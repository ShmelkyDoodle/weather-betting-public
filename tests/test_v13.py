"""1-minute lock-in augmenter: corroboration + _apply safety gates (offline)."""
import asyncio, sys
from datetime import datetime, timezone
import os as _osp; sys.path.insert(0, _osp.path.dirname(_osp.path.dirname(_osp.path.abspath(__file__))))
from weather_arb.config import Settings
from weather_arb.onemin_ingest import corroborated_day_max, OneMinutePoller

failures = []
def check(n, c):
    print(("PASS" if c else "FAIL"), "-", n)
    if not c: failures.append(n)

DATE = "2026-07-20"
OFF = -7  # KDEN


def _ts(h, m=0):
    return datetime(2026, 7, 20, h, m, tzinfo=timezone.utc)


class StubDB:
    """Monotonic T_max store mirroring Database.update_tmax semantics."""
    def __init__(self, initial=None):
        self.tmax = dict(initial or {})
        self.writes = 0

    async def get_tmax(self, icao, date):
        return self.tmax.get((icao, date))

    async def update_tmax(self, *, icao, climate_date, temp_f, observed_at_utc):
        self.writes += 1
        cur = self.tmax.get((icao, climate_date))
        self.tmax[(icao, climate_date)] = max(cur, temp_f) if cur is not None else temp_f
        return self.tmax[(icao, climate_date)]


def _poller(db):
    p = OneMinutePoller(session=None, db=db, settings=Settings(), on_new_high=None)
    return p


def _capturing_poller(db):
    calls = []
    async def cb(icao, date, tmax):
        calls.append((icao, date, tmax))
    p = OneMinutePoller(session=None, db=db, settings=Settings(), on_new_high=cb)
    return p, calls


def test_corroboration():
    # Sustained peak of 102 (3 minutes) -> corroborated.
    obs = [(_ts(21, i), 102.0) for i in range(3)] + [(_ts(20, i), 100.0) for i in range(5)]
    cm = corroborated_day_max(obs, DATE, OFF, 2, 1.5)
    check("sustained max 102 corroborated", cm is not None and cm[0] == 102.0)
    # A lone spike of 108 ABOVE the real peak is skipped; 102 still found.
    obs2 = [(_ts(21, 30), 108.0)] + obs
    cm2 = corroborated_day_max(obs2, DATE, OFF, 2, 1.5)
    check("lone spike above peak skipped, falls back to 102", cm2 is not None and cm2[0] == 102.0)
    # Everything a lone minute -> nothing corroborated.
    check("all-singletons -> None", corroborated_day_max(
        [(_ts(1), 70.0), (_ts(2), 80.0), (_ts(3), 90.0)], DATE, OFF, 2, 1.5) is None)
    # LST-day filter: an ob before the climate day starts is excluded.
    early = [(_ts(6, 0), 120.0), (_ts(6, 1), 120.0)]  # 06:00Z, offset -7 -> prev day 23:00
    check("prior-LST-day obs excluded", corroborated_day_max(early, DATE, OFF, 2, 1.5) is None)


async def test_apply_new_high_fires():
    db = StubDB({("KDEN", DATE): 100.0})
    p, calls = _capturing_poller(db)
    obs = [(_ts(21, i), 102.0) for i in range(3)]
    result = await p._apply("KDEN", DATE, OFF, obs)
    check("raises T_max to 102", result == 102.0 and db.tmax[("KDEN", DATE)] == 102.0)
    check("fires on_new_high once with 102", calls == [("KDEN", DATE, 102.0)])


async def test_apply_glitch_does_not_lock():
    # Lone 108 spike over a sustained 100 == prev T_max: corroborated level is 100,
    # which is not a new high -> no write, no fire.
    db = StubDB({("KDEN", DATE): 100.0})
    p, calls = _capturing_poller(db)
    obs = [(_ts(21, 30), 108.0)] + [(_ts(20, i), 100.0) for i in range(5)]
    result = await p._apply("KDEN", DATE, OFF, obs)
    check("lone glitch does not raise T_max", result is None and db.tmax[("KDEN", DATE)] == 100.0)
    check("no lock fired on glitch", calls == [] and db.writes == 0)


async def test_apply_sanity_cap():
    # Corroborated 107 but that is > prev(100) + spike_threshold(5) -> rejected.
    db = StubDB({("KDEN", DATE): 100.0})
    p, calls = _capturing_poller(db)
    obs = [(_ts(21, i), 107.0) for i in range(3)]
    result = await p._apply("KDEN", DATE, OFF, obs)
    check("implausible jump rejected by sanity cap", result is None)
    check("T_max unchanged, no fire", db.tmax[("KDEN", DATE)] == 100.0 and calls == [])


async def test_apply_no_new_high_is_quiet():
    db = StubDB({("KDEN", DATE): 105.0})
    p, calls = _capturing_poller(db)
    obs = [(_ts(21, i), 102.0) for i in range(3)]
    result = await p._apply("KDEN", DATE, OFF, obs)
    check("below existing T_max -> no-op", result is None and calls == [])


async def main():
    test_corroboration()
    await test_apply_new_high_fires()
    await test_apply_glitch_does_not_lock()
    await test_apply_sanity_cap()
    await test_apply_no_new_high_is_quiet()
    print("\n" + ("ALL PASS" if not failures else f"FAILURES: {failures}"))
    sys.exit(1 if failures else 0)

asyncio.run(main())
