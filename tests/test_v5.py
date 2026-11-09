"""Tests for multi-model forecasting + per-model Brier comparison."""
import asyncio, os, sys, tempfile
import os as _osp; sys.path.insert(0, _osp.path.dirname(_osp.path.dirname(_osp.path.abspath(__file__))))

from weather_arb.config import Settings, StationConfig
from weather_arb.db import Database
from weather_arb.forecast_ingest import ForecastClient
from weather_arb.models import Venue, WeatherMarket, STRIKE_GREATER_EQ
from weather_arb.report import build_summary
from weather_arb import timeutils

failures = []
def check(name, cond):
    print(("PASS" if cond else "FAIL"), "-", name)
    if not cond: failures.append(name)

STA = StationConfig("KMDW", "Chicago Midway", 41.786, -87.752, -6)
MKT = WeatherMarket("KXHIGHCHI-D-B85.5", Venue.KALSHI, "KMDW", floor_strike_f=85.0, strike_type=STRIKE_GREATER_EQ)

class StubMos:
    def __init__(self, vals): self.vals=vals  # {code: max_f}
    async def daily_max_f(self, icao, code, climate_date, off): return self.vals.get(code)

async def test_model_probabilities():
    fc = ForecastClient(session=None, settings=Settings(), mos=StubMos({"NBS":90.0,"GFS":86.0}))
    # stub the gridpoint high (mu, sigma)
    async def fake_grid(station, cdate): return (88.0, 3.0)
    fc._grid_forecast_high = fake_grid  # type: ignore
    ests = await fc.model_probabilities(station=STA, market=MKT, climate_date="2026-07-22", observed_tmax_f=None)
    check("has current/nbm/gfs_mos/ensemble", set(ests) == {"current","nbm","gfs_mos","ensemble"})
    # nbm mean 90 > current 88 > gfs 86, all >= strike 85 -> all high prob; nbm highest
    check("nbm prob >= current prob", ests["nbm"].probability >= ests["current"].probability)
    # ensemble mean = (88+90+86)/3 = 88; sigma widened by disagreement
    check("ensemble mu ~88", abs(ests["ensemble"].forecast_high_f-88.0) < 1e-6)
    check("ensemble sigma > base 3.0 (disagreement widens it)", ests["ensemble"].sigma_f > 3.0)
    check("probs in [0,1]", all(0<=e.probability<=1 for e in ests.values()))
    # MOS unavailable -> only current + no ensemble
    fc2 = ForecastClient(session=None, settings=Settings(), mos=StubMos({}))
    fc2._grid_forecast_high = fake_grid  # type: ignore
    e2 = await fc2.model_probabilities(station=STA, market=MKT, climate_date="2026-07-22", observed_tmax_f=None)
    check("only current when no MOS", set(e2) == {"current"})

async def test_db_and_report_comparison():
    with tempfile.TemporaryDirectory() as d:
        path=os.path.join(d,"m.sqlite3")
        async with Database(path) as db:
            # 3 markets; log 2 models each; settle them
            data = [("T1","yes"),("T2","no"),("T3","yes")]
            # 'good' model predicts correctly (0.9 for yes, 0.1 for no); 'bad' inverted
            for tk,res in data:
                pyes = 0.9 if res=="yes" else 0.1
                await db.record_model_prediction(ticker=tk, model="good", climate_date="2026-07-22", prob=pyes, forecast_high_f=90, sigma_f=3)
                await db.record_model_prediction(ticker=tk, model="bad", climate_date="2026-07-22", prob=1-pyes, forecast_high_f=80, sigma_f=3)
                await db.record_market_result(tk, res)
            # unsettled prediction shouldn't count
            await db.record_model_prediction(ticker="T4", model="good", climate_date="2026-07-22", prob=0.5, forecast_high_f=85, sigma_f=3)
        summary = build_summary(path, fee_coefficient=0.07)
        mc = summary["model_comparison"]
        check("both models compared", set(mc) == {"good","bad"})
        check("good n=3 (unsettled excluded)", mc["good"]["n"] == 3)
        # good: brier = mean((0.9-1)^2,(0.1-0)^2,(0.9-1)^2) = (.01+.01+.01)/3 = .01
        check("good brier ~0.01", abs(mc["good"]["brier"]-0.01) < 1e-9)
        check("good accuracy 100%", abs(mc["good"]["accuracy"]-1.0) < 1e-9)
        check("bad worse than good", mc["bad"]["brier"] > mc["good"]["brier"])
        # render text should include the comparison
        from weather_arb.report import render_text
        txt = render_text(summary)
        check("render shows best model", "best so far: good" in txt)

async def main():
    await test_model_probabilities()
    await test_db_and_report_comparison()
    print("\n"+("ALL PASS" if not failures else f"FAILURES: {failures}"))
    sys.exit(1 if failures else 0)

asyncio.run(main())
