"""#0b Time-decay edge threshold + signal persistence (offline, stubbed).

Locks down the three things most likely to be got wrong:
  * the threshold uses each STATION's LST offset (cities span UTC-5..-8), so the
    same instant is a different threshold in NY and LA;
  * persistence gates BOTH entry actions and restarts on a side flip or a gap;
  * with both flags off, nothing changes at all;
  * the two flags are INDEPENDENT -- measured 2026-08-23 the decay is harmful and
    the persistence gate is mildly useful, so one must be usable without the other.
"""
import asyncio, math, os, sys, tempfile
from datetime import datetime, timedelta, timezone
import os as _osp
sys.path.insert(0, _osp.path.dirname(_osp.path.dirname(_osp.path.abspath(__file__))))
from weather_arb import timeutils
from weather_arb.arb_engine import (
    ArbitrageEngine, get_dynamic_edge_threshold, gross_edges,
)
from weather_arb.config import Settings
from weather_arb.models import Action

failures = []
def check(n, c):
    print(("PASS" if c else "FAIL"), "-", n)
    if not c: failures.append(n)

KW = dict(max_threshold=0.25, min_threshold=0.05, start_hour=8.0, end_hour=16.0)


def test_decay_shape():
    check("flat at max before the start hour",
          get_dynamic_edge_threshold(0.0, **KW) == 0.25
          and get_dynamic_edge_threshold(8.0, **KW) == 0.25)
    check("flat at min after the end hour",
          get_dynamic_edge_threshold(16.0, **KW) == 0.05
          and get_dynamic_edge_threshold(23.9, **KW) == 0.05)
    check("midpoint is the mean", abs(get_dynamic_edge_threshold(12.0, **KW) - 0.15) < 1e-12)
    check("monotonically decreasing across the window",
          all(get_dynamic_edge_threshold(h, **KW) >= get_dynamic_edge_threshold(h + 0.25, **KW)
              for h in [8 + 0.25 * i for i in range(32)]))
    check("fractional hours interpolate (not stepped hourly)",
          get_dynamic_edge_threshold(10.5, **KW) < get_dynamic_edge_threshold(10.0, **KW))
    # Degenerate windows must not divide by zero; they collapse to the strict end.
    for bad in ({"start_hour": 16.0, "end_hour": 8.0}, {"start_hour": 9.0, "end_hour": 9.0}):
        kw = dict(KW); kw.update(bad)
        check(f"degenerate window {bad} -> max_threshold",
              get_dynamic_edge_threshold(12.0, **kw) == 0.25)


def test_lst_offset_is_per_station():
    """The whole point of item 2: one instant, seven different local hours."""
    # 18:00 UTC. NY (-5) = 13:00 LST -> mid-decay. LA (-8) = 10:00 LST -> stricter.
    at = datetime(2026, 8, 23, 18, 0, tzinfo=timezone.utc)
    ny = get_dynamic_edge_threshold(timeutils.local_hour_lst(at, -5), **KW)
    la = get_dynamic_edge_threshold(timeutils.local_hour_lst(at, -8), **KW)
    check("same instant, NY is later in its day than LA", ny < la)
    check("NY 13:00 LST -> 0.125", abs(ny - 0.125) < 1e-12)
    check("LA 10:00 LST -> 0.200", abs(la - 0.200) < 1e-12)
    check("local_hour_lst is fractional",
          abs(timeutils.local_hour_lst(
              datetime(2026, 8, 23, 18, 30, tzinfo=timezone.utc), -5) - 13.5) < 1e-9)
    check("no DST applied (fixed offset)",
          timeutils.local_hour_lst(datetime(2026, 1, 15, 18, 0, tzinfo=timezone.utc), -5)
          == timeutils.local_hour_lst(at, -5))


def _engine(**env):
    for k, v in env.items():
        os.environ[k] = v
    settings = Settings.from_env()
    eng = ArbitrageEngine.__new__(ArbitrageEngine)   # pure methods only; no I/O
    eng._settings = settings
    eng._edge_seen = {}
    eng._edge_seen_day = None
    return eng, settings


def _clear(*keys):
    for k in keys:
        os.environ.pop(k, None)


def test_disabled_by_default():
    _clear("WA_ENABLE_DYNAMIC_EDGE", "WA_ENABLE_EDGE_PERSISTENCE")
    eng, settings = _engine()
    station = settings.station_by_icao("KNYC")
    check("both flags default off",
          settings.enable_dynamic_edge is False
          and settings.enable_edge_persistence is False)
    check("threshold is the flat live one when off",
          eng._edge_threshold_for(station) == settings.risk.model_edge_threshold)
    check("persistence is a no-op when off",
          eng._edge_persisted("T", Action.BUY, "2026-08-23") is True)
    check("even a None action passes when off",
          eng._edge_persisted("T", None, "2026-08-23") is True)
    check("no state accumulated when off", eng._edge_seen == {})


def test_flags_are_independent():
    """The decay is harmful, the gate is useful -- neither may drag the other in."""
    _clear("WA_ENABLE_DYNAMIC_EDGE", "WA_ENABLE_EDGE_PERSISTENCE")
    eng, settings = _engine(WA_ENABLE_EDGE_PERSISTENCE="1")
    station = settings.station_by_icao("KNYC")
    check("persistence on alone leaves the threshold flat",
          eng._edge_threshold_for(station) == settings.risk.model_edge_threshold)
    check("persistence on alone still gates",
          eng._edge_persisted("X", Action.BUY, "2026-08-23") is False)

    _clear("WA_ENABLE_EDGE_PERSISTENCE")
    eng2, settings2 = _engine(WA_ENABLE_DYNAMIC_EDGE="1")
    check("decay on alone does NOT gate on persistence",
          eng2._edge_persisted("X", Action.BUY, "2026-08-23") is True)
    # Freeze the clock so the threshold assertion is deterministic: 12:00 UTC is
    # 07:00 LST at KNYC (-5), i.e. before start_hour -> the strict end.
    real_now = timeutils.utc_now
    timeutils.utc_now = lambda: datetime(2026, 8, 23, 12, 0, tzinfo=timezone.utc)
    try:
        thr = eng2._edge_threshold_for(settings2.station_by_icao("KNYC"))
    finally:
        timeutils.utc_now = real_now
    check("decay on alone moves the threshold off the flat value",
          thr == settings2.risk.dynamic_edge_max_threshold
          and thr != settings2.risk.model_edge_threshold)
    _clear("WA_ENABLE_DYNAMIC_EDGE")


def test_persistence_gate():
    eng, settings = _engine(WA_ENABLE_EDGE_PERSISTENCE="1", WA_EDGE_PERSIST_SECONDS="900")
    day = "2026-08-23"
    base = datetime(2026, 8, 23, 12, 0, tzinfo=timezone.utc)
    now = {"t": base}
    timeutils.utc_now = lambda: now["t"]              # freeze the clock

    check("first sighting never fires", eng._edge_persisted("A", Action.BUY, day) is False)
    now["t"] = base + timedelta(seconds=899)
    check("one second short still does not fire",
          eng._edge_persisted("A", Action.BUY, day) is False)
    now["t"] = base + timedelta(seconds=900)
    check("fires exactly at the persistence window",
          eng._edge_persisted("A", Action.BUY, day) is True)

    # A side flip must restart the clock, not inherit the elapsed time.
    now["t"] = base + timedelta(seconds=1000)
    check("side flip restarts the clock",
          eng._edge_persisted("A", Action.SELL, day) is False)
    now["t"] = base + timedelta(seconds=1500)
    check("still short after the flip (only 500s on the new side)",
          eng._edge_persisted("A", Action.SELL, day) is False)
    now["t"] = base + timedelta(seconds=1900)
    check("fires 900s after the flip", eng._edge_persisted("A", Action.SELL, day) is True)

    # SELL is an entry too (buying NO) -- it must be gated, not waved through.
    eng._edge_seen.clear()
    now["t"] = base
    check("SELL is gated on first sighting too",
          eng._edge_persisted("B", Action.SELL, day) is False)

    # A vanished edge clears the timer, so a flicker never accumulates.
    eng._edge_seen.clear()
    now["t"] = base
    eng._edge_persisted("C", Action.BUY, day)
    now["t"] = base + timedelta(seconds=600)
    check("edge gone -> not persisted", eng._edge_persisted("C", None, day) is False)
    check("edge gone -> state evicted", "C" not in eng._edge_seen)
    now["t"] = base + timedelta(seconds=610)
    check("re-appearance starts over, does not inherit 600s",
          eng._edge_persisted("C", Action.BUY, day) is False)


def test_climate_date_eviction():
    eng, _ = _engine(WA_ENABLE_EDGE_PERSISTENCE="1", WA_EDGE_PERSIST_SECONDS="900")
    base = datetime(2026, 8, 23, 12, 0, tzinfo=timezone.utc)
    now = {"t": base}
    timeutils.utc_now = lambda: now["t"]
    for t in ("D1", "D2", "D3"):
        eng._edge_persisted(t, Action.BUY, "2026-08-23")
    check("state accumulates within a day", len(eng._edge_seen) == 3)
    eng._edge_persisted("E1", Action.BUY, "2026-08-24")
    check("rollover evicts the previous day's tickers",
          set(eng._edge_seen) == {"E1"})
    check("rollover recorded", eng._edge_seen_day == "2026-08-24")


def test_gross_edges():
    yes, no = gross_edges(0.60, 0.40, 45, 70)
    check("buy-YES edge = yes_prob - ask/100", abs(yes - 0.15) < 1e-12)
    check("buy-NO edge = no_prob - no_ask/100", abs(no - (-0.30)) < 1e-12)
    check("missing book side -> None", gross_edges(0.6, 0.4, None, None) == (None, None))
    check("one-sided book handled", gross_edges(0.6, 0.4, 45, None)[1] is None)


_real_now = timeutils.utc_now
for fn in (test_decay_shape, test_lst_offset_is_per_station, test_disabled_by_default,
           test_flags_are_independent, test_persistence_gate,
           test_climate_date_eviction, test_gross_edges):
    print(f"\n-- {fn.__name__}")
    try:
        fn()
    finally:
        timeutils.utc_now = _real_now
        _clear("WA_ENABLE_DYNAMIC_EDGE", "WA_ENABLE_EDGE_PERSISTENCE",
               "WA_EDGE_PERSIST_SECONDS")

print("\n" + ("ALL PASS" if not failures else f"FAILURES: {failures}"))
sys.exit(1 if failures else 0)
