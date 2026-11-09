"""Climatology base-rate model: aggregation + climo/blend pillars."""
import asyncio, os, sys
import os as _osp; sys.path.insert(0, _osp.path.dirname(_osp.path.dirname(_osp.path.abspath(__file__))))
from weather_arb.config import Settings, ForecastConfig, StationConfig
from weather_arb.forecast_ingest import ForecastClient
from weather_arb.climatology import ClimatologyClient
from weather_arb.models import Venue, WeatherMarket, STRIKE_GREATER_EQ

failures=[]
def check(n,c):
    print(("PASS" if c else "FAIL"),"-",n)
    if not c: failures.append(n)

STA=StationConfig("KMDW","Chicago Midway",41.786,-87.752,-6)
MKT=WeatherMarket("KXHIGHCHI-D-B85.5",Venue.KALSHI,"KMDW",floor_strike_f=85.0,strike_type=STRIKE_GREATER_EQ)

class StubClimo:
    async def distribution(self, icao, cd, off): return (85.0, 4.0, 200)

async def test_climo_blend_pillars():
    fc=ForecastClient(session=None, settings=Settings(), mos=None, climo=StubClimo())
    async def fake_grid(s,c): return (88.0, 3.0)
    fc._grid_forecast_high=fake_grid  # type: ignore
    e=await fc.model_probabilities(station=STA, market=MKT, climate_date="2026-07-22", observed_tmax_f=None)
    check("emits climo + blend", "climo" in e and "blend" in e)
    check("blend = avg(climo, current)",
          abs(e["blend"].probability - 0.5*(e["climo"].probability+e["current"].probability))<1e-9)
    check("climo uses climatological mean 85", abs(e["climo"].forecast_high_f-85.0)<1e-9)
    # no climo client -> no climo/blend
    fc2=ForecastClient(session=None, settings=Settings(), mos=None, climo=None)
    fc2._grid_forecast_high=fake_grid  # type: ignore
    e2=await fc2.model_probabilities(station=STA, market=MKT, climate_date="2026-07-22", observed_tmax_f=None)
    check("no climo pillar without client", "climo" not in e2 and "blend" not in e2)

async def test_aggregation_offline():
    cfg=ForecastConfig(climo_years=1, climo_window_days=1, climo_min_samples=1)
    c=ClimatologyClient(session=None, settings=Settings(forecast=cfg))
    async def fake_get(params):
        # two CST climate days: 07-21 max 92, 07-22 max 85
        return ("station,valid,tmpf\n"
                "MDW,2015-07-21 18:00,90\nMDW,2015-07-21 20:00,92\n"
                "MDW,2015-07-22 18:00,85\n")
    c._get=fake_get  # type: ignore
    dist=await c.distribution("KMDW","2026-07-22",-6)
    check("distribution parsed", dist is not None)
    check("climatological mean = 88.5 (avg of 92,85)", dist is not None and abs(dist[0]-88.5)<1e-9)
    check("sample count = 2", dist is not None and dist[2]==2)
    # below min_samples -> None
    cfg2=ForecastConfig(climo_years=1, climo_window_days=1, climo_min_samples=99)
    c2=ClimatologyClient(session=None, settings=Settings(forecast=cfg2)); c2._get=fake_get  # type: ignore
    check("None below min_samples", await c2.distribution("KMDW","2026-07-22",-6) is None)

async def main():
    await test_climo_blend_pillars(); await test_aggregation_offline()
    print("\n"+("ALL PASS" if not failures else f"FAILURES: {failures}"))
    sys.exit(1 if failures else 0)
asyncio.run(main())
