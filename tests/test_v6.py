"""Stage 2 tests: forecast bias tracking + bias-corrected model variants."""
import asyncio, os, sys, tempfile
import os as _osp; sys.path.insert(0, _osp.path.dirname(_osp.path.dirname(_osp.path.abspath(__file__))))
from weather_arb.config import Settings, StationConfig
from weather_arb.db import Database
from weather_arb.forecast_ingest import ForecastClient
from weather_arb.models import Venue, WeatherMarket, STRIKE_GREATER_EQ
from weather_arb import timeutils

failures=[]
def check(n,c):
    print(("PASS" if c else "FAIL"),"-",n)
    if not c: failures.append(n)

STA=StationConfig("KMDW","Chicago Midway",41.786,-87.752,-6)
MKT=WeatherMarket("KXHIGHCHI-D-B85.5",Venue.KALSHI,"KMDW",floor_strike_f=85.0,strike_type=STRIKE_GREATER_EQ)

async def test_recent_bias():
    with tempfile.TemporaryDirectory() as d:
        async with Database(os.path.join(d,"b.sqlite3")) as db:
            # 6 past days: forecast 85, realized 88 -> bias +3
            for day in range(1,7):
                cd=f"2026-07-0{day}"
                await db.record_daily_forecast(icao="KMDW",model="current",climate_date=cd,forecast_high_f=85.0)
                await db.update_tmax(icao="KMDW",climate_date=cd,temp_f=88.0,observed_at_utc=timeutils.utc_now())
            b=await db.recent_bias("KMDW","current","2026-07-20")
            check("bias = +3.0 (model runs cold)", b is not None and abs(b-3.0)<1e-9)
            # below min_samples -> None
            b2=await db.recent_bias("KMDW","current","2026-07-03")  # only days 1,2 < date
            check("None below min_samples", b2 is None)
            # excludes the current/future day (climate_date < before_date)
            b3=await db.recent_bias("KMDW","current","2026-07-04")  # days 1,2,3
            check("still None at 3 samples", b3 is None)

async def test_bc_variants():
    fc=ForecastClient(session=None, settings=Settings(), mos=None)
    async def fake_grid(s,c): return (88.0,3.0)
    fc._grid_forecast_high=fake_grid  # type: ignore
    ests=await fc.model_probabilities(station=STA,market=MKT,climate_date="2026-07-22",observed_tmax_f=None,bias={"current":2.0})
    check("has current and current_bc", "current" in ests and "current_bc" in ests)
    check("bc shifts forecast_high by bias", abs(ests["current_bc"].forecast_high_f-90.0)<1e-9)
    check("uncorrected unchanged", abs(ests["current"].forecast_high_f-88.0)<1e-9)
    # no bias -> no _bc
    ests2=await fc.model_probabilities(station=STA,market=MKT,climate_date="2026-07-22",observed_tmax_f=None,bias=None)
    check("no bc without bias", "current_bc" not in ests2)

async def main():
    await test_recent_bias(); await test_bc_variants()
    print("\n"+("ALL PASS" if not failures else f"FAILURES: {failures}"))
    sys.exit(1 if failures else 0)
asyncio.run(main())
