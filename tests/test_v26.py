"""#A kinematic METAR spike guard: rate-limited warming, not a flat step (offline).

The bug being fixed: a flat >5F guard plus hourly METARs plus a 5-minute
confirmation window meant a fast-warming station could never validate its rise,
and validated_max_f froze near the morning value (mean -11.5F at DEN, -7.2F at
AUS). These lock down the replacement AND the properties that keep it safe.
"""
import asyncio, os, sys, tempfile
from datetime import timedelta
import os as _osp; sys.path.insert(0, _osp.path.dirname(_osp.path.dirname(_osp.path.abspath(__file__))))
from weather_arb import timeutils
from weather_arb.config import Settings
from weather_arb.db import Database
from weather_arb.metar_ingest import MetarPoller, max_plausible_rise_f, plausible_rise
from weather_arb.models import MetarObservation

failures = []
def check(n, c):
    print(("PASS" if c else "FAIL"), "-", n)
    if not c: failures.append(n)

BASE = timeutils.parse_isoformat_utc("2026-07-21T13:00:00+00:00")   # ~06:00 MST
def at(minutes): return BASE + timedelta(minutes=minutes)
def F(f): return (f - 32.0) * 5.0 / 9.0        # MetarObservation carries Celsius


def test_allowance_regimes():
    check("sub-minute updates get the fixed jitter allowance",
          max_plausible_rise_f(30.0) == 2.0)
    check("one hour allows rate + buffer", max_plausible_rise_f(3600.0) == 9.0)
    check("half an hour allows half the rate + buffer",
          max_plausible_rise_f(1800.0) == 5.0)
    check("long gaps are capped, not extrapolated",
          max_plausible_rise_f(6 * 3600.0) == 15.0)
    check("the cap binds from ~1.75h onward", max_plausible_rise_f(2 * 3600.0) == 15.0)
    check("limits are overridable", max_plausible_rise_f(3600.0, rate_f_per_hour=4.0) == 5.0)


def test_plausible_rise():
    check("routine hourly warm-up is plausible",
          plausible_rise(62.0, at(0), 69.0, at(60)) is True)
    check("the same step in five minutes is not",
          plausible_rise(62.0, at(0), 69.0, at(5)) is False)
    check("a big spike over an hour is still rejected",
          plausible_rise(62.0, at(0), 82.0, at(60)) is False)
    # Cooling is never rate-limited -- it cannot inflate the daily max.
    check("a large fast DROP is always accepted",
          plausible_rise(95.0, at(0), 60.0, at(1)) is True)
    check("an equal temperature is accepted",
          plausible_rise(80.0, at(0), 80.0, at(1)) is True)
    # Out-of-order timestamps must not be read as a long, permissive gap.
    check("negative elapsed falls to the strictest regime",
          plausible_rise(60.0, at(60), 70.0, at(0)) is False)
    check("negative elapsed still allows a small jitter step",
          plausible_rise(60.0, at(60), 61.5, at(0)) is True)


async def _poller(db, settings=None):
    return MetarPoller(session=None, db=db,
                       settings=settings or Settings(dry_run=True), on_new_high=None)


async def test_den_morning_no_longer_freezes():
    """The regression: 8F/hour warm-up on an hourly feed must track all day."""
    with tempfile.TemporaryDirectory() as d:
        async with Database(os.path.join(d, "t.sqlite3")) as db:
            p = await _poller(db)
            station = Settings(dry_run=True).station_by_icao("KDEN")
            cdate = timeutils.climate_date_str(BASE, station.standard_utc_offset_hours)
            temps = [45.0, 53.0, 61.0, 69.0, 77.0, 85.0, 92.0]   # +8F/h, hourly
            for i, t in enumerate(temps):
                await p._process(MetarObservation("KDEN", at(i * 60), F(t), F(t - 20), "raw"))
            got = await db.get_tmax("KDEN", cdate)
            check(f"8F/hour hourly warm-up tracks to the true max (got {got})", got == 92.0)


async def test_spikes_are_still_caught():
    with tempfile.TemporaryDirectory() as d:
        async with Database(os.path.join(d, "t.sqlite3")) as db:
            p = await _poller(db)
            station = Settings(dry_run=True).station_by_icao("KDEN")
            cdate = timeutils.climate_date_str(BASE, station.standard_utc_offset_hours)
            await p._process(MetarObservation("KDEN", at(0), F(70.0), F(50.0), "raw"))
            await p._process(MetarObservation("KDEN", at(60), F(78.0), F(50.0), "raw"))
            # +25F in one hour is far past both the rate and the absolute cap.
            await p._process(MetarObservation("KDEN", at(120), F(103.0), F(50.0), "raw"))
            check("a 25F/hour spike is held, not validated",
                  await db.get_tmax("KDEN", cdate) == 78.0)
            # An isolated spike must not become the baseline for the NEXT reading:
            # measuring from 103 would make 99 look like a harmless drop.
            await p._process(MetarObservation("KDEN", at(180), F(99.0), F(50.0), "raw"))
            check("a held spike does not legitimise the next one",
                  await db.get_tmax("KDEN", cdate) == 78.0)


async def test_restart_without_velocity_reference():
    """No trusted reading yet (process restarted mid-day) -> flat-threshold fallback."""
    with tempfile.TemporaryDirectory() as d:
        async with Database(os.path.join(d, "t.sqlite3")) as db:
            station = Settings(dry_run=True).station_by_icao("KDEN")
            cdate = timeutils.climate_date_str(BASE, station.standard_utc_offset_hours)
            await db.update_tmax(icao="KDEN", climate_date=cdate, temp_f=80.0,
                                 observed_at_utc=at(0))
            p = await _poller(db)          # fresh poller: no in-memory trusted reading
            await p._process(MetarObservation("KDEN", at(60), F(83.0), F(50.0), "raw"))
            check("small step accepted against the persisted max",
                  await db.get_tmax("KDEN", cdate) == 83.0)

            p2 = await _poller(db)
            await p2._process(MetarObservation("KDEN", at(120), F(95.0), F(50.0), "raw"))
            check("large step held pending on the fallback path",
                  await db.get_tmax("KDEN", cdate) == 83.0)


async def main():
    test_allowance_regimes(); test_plausible_rise()
    await test_den_morning_no_longer_freezes()
    await test_spikes_are_still_caught()
    await test_restart_without_velocity_reference()
    print("\n" + ("ALL PASS" if not failures else f"{len(failures)} FAILED: {failures}"))
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
