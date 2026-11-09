"""#A The lock-in margin: certainty is measured against the SETTLEMENT interval (offline).

THE DEFECT THIS LOCKS OUT. `deterministic_lock_in` used to compare a float
observation against the raw INTEGER strike:

    if hi is not None and observed_tmax_f > hi:  return (Side.NO, Action.BUY)

Kalshi settles on the NWS CLI daily high -- a whole-degF value, i.e. the
continuous record ROUNDED half-up. `observed_tmax_f` is a float from a
tenths-of-degC METAR, so values like 84.02 are routine (93.2% of 9,056 stored
observations sit within +-0.08 degF of an integer). `84.02 > 84` is true, so the
old rule called the {83, 84} bucket a certain NO -- but a true max anywhere in
[84.0, 84.5) rounds to a CLI of 84, and that bucket settles YES.

Replaying the old rule over every settled market with local observation history:
564 detections, 18 WRONG, 16 of them with a margin under 0.1 degF, and 11
reproducing on the exact `daily_tmax` value the live engine consumes.

The fix is not a special case for the observed failures. It compares against
`settlement_interval_f()` -- the continuous region of true temperatures that
round into a YES settlement, which already mirrors `settles_yes` -- so the
lock-in rule, the pricing grid and the settlement rule cannot drift apart.

`margin_f` is calibrated so the default 0.5 IS that exact bound (0.5 is the
rounding half-width); larger values are pure conservatism.
"""
import asyncio, os, sys, tempfile
from datetime import timedelta
import os as _osp; sys.path.insert(0, _osp.path.dirname(_osp.path.dirname(_osp.path.abspath(__file__))))

from weather_arb import timeutils
from weather_arb.config import Settings
from weather_arb.db import Database
from weather_arb.metar_ingest import MetarPoller
from weather_arb.models import (
    LOCKIN_MARGIN_F, Action, MetarObservation, Side, Venue, WeatherMarket,
    STRIKE_BETWEEN, STRIKE_GREATER, STRIKE_GREATER_EQ, STRIKE_LESS, STRIKE_LESS_EQ,
)

failures = []
def check(n, c):
    print(("PASS" if c else "FAIL"), "-", n)
    if not c: failures.append(n)

NO, YES = (Side.NO, Action.BUY), (Side.YES, Action.BUY)


def mkt(**kw):
    return WeatherMarket(ticker="T", venue=Venue.KALSHI, icao="KNYC", **kw)

# The exact shape that produced the real wrong calls: KXHIGHNY-26AUG04-B83.5,
# cap 84, observed running max 84.02 -- settled YES.
INTERIOR = mkt(floor_strike_f=83.0, cap_strike_f=84.0, strike_type=STRIKE_BETWEEN)
TAIL_HI  = mkt(floor_strike_f=87.0, strike_type=STRIKE_GREATER)
TAIL_LO  = mkt(cap_strike_f=78.0, strike_type=STRIKE_LESS)


# --------------------------------------------------------------------------- #
# 1. The regression itself
# --------------------------------------------------------------------------- #
def test_sub_degree_does_not_lock():
    check("84.02 does NOT lock out a cap of 84 (the real KNYC 2026-08-04 case)",
          INTERIOR.deterministic_lock_in(84.02) is None)
    # Every distinct observed value that produced a wrong call in the replay.
    for observed, cap in ((84.02, 84.0), (91.04, 91.0), (93.02, 93.0),
                          (96.08, 96.0), (100.04, 100.0), (102.02, 102.0),
                          (78.08, 78.0), (80.06, 80.0)):
        m = mkt(floor_strike_f=cap - 1.0, cap_strike_f=cap, strike_type=STRIKE_BETWEEN)
        check(f"{observed} does NOT lock out a cap of {cap:.0f}",
              m.deterministic_lock_in(observed) is None)


def test_rounding_boundary_semantics():
    """84.5 is the exact point certainty begins: it rounds half-up to 85."""
    check("84.49 does NOT lock out a cap of 84 (rounds to 84 -> could settle YES)",
          INTERIOR.deterministic_lock_in(84.49) is None)
    check("84.50 DOES lock out a cap of 84 (rounds half-up to 85 -> certain NO)",
          INTERIOR.deterministic_lock_in(84.5) == NO)
    check("the threshold is inclusive at exactly cap + LOCKIN_MARGIN_F",
          INTERIOR.deterministic_lock_in(84.0 + LOCKIN_MARGIN_F) == NO)


def test_clear_margin_still_locks():
    check("85.0 DOES lock out a cap of 84", INTERIOR.deterministic_lock_in(85.0) == NO)
    check("86.0 DOES lock out a cap of 84", INTERIOR.deterministic_lock_in(86.0) == NO)


# --------------------------------------------------------------------------- #
# 4. The tail-high branch respects the same margin
# --------------------------------------------------------------------------- #
def test_tail_high_respects_margin():
    # `greater` floor 87 settles YES only at T >= 88, i.e. true max >= 87.5.
    check("tail-high 87.0 is NOT certain YES (strict tail needs T >= 88)",
          TAIL_HI.deterministic_lock_in(87.0) is None)
    check("tail-high 87.08 is NOT certain YES (the LAX/MIA wrong-call shape)",
          TAIL_HI.deterministic_lock_in(87.08) is None)
    check("tail-high 87.49 is NOT certain YES",
          TAIL_HI.deterministic_lock_in(87.49) is None)
    check("tail-high 87.5 IS certain YES (rounds to 88)",
          TAIL_HI.deterministic_lock_in(87.5) == YES)
    check("tail-high 88.0 IS certain YES", TAIL_HI.deterministic_lock_in(88.0) == YES)
    check("a bounded-above bucket is never certain YES intraday",
          INTERIOR.deterministic_lock_in(83.5) is None)


def test_strict_and_non_strict_tails_preserved():
    """The strict/non-strict distinction survives, one whole integer apart."""
    ge = mkt(floor_strike_f=87.0, strike_type=STRIKE_GREATER_EQ)   # YES at T >= 87
    check("greater_or_equal floor 87 is certain YES at 86.5 (rounds to 87)",
          ge.deterministic_lock_in(86.5) == YES)
    check("greater_or_equal floor 87 is NOT certain at 86.49",
          ge.deterministic_lock_in(86.49) is None)
    check("strict greater floor 87 is NOT certain at 86.5 (needs T >= 88)",
          TAIL_HI.deterministic_lock_in(86.5) is None)
    check("the two tails differ by exactly one integer",
          TAIL_HI.deterministic_lock_in(87.5) == YES and ge.deterministic_lock_in(86.5) == YES)

    le = mkt(cap_strike_f=78.0, strike_type=STRIKE_LESS_EQ)        # YES at T <= 78
    check("strict less cap 78 (YES at T <= 77) is certain NO at 77.5",
          TAIL_LO.deterministic_lock_in(77.5) == NO)
    check("strict less cap 78 is NOT certain NO at 77.49",
          TAIL_LO.deterministic_lock_in(77.49) is None)
    check("less_or_equal cap 78 (YES at T <= 78) needs a full degree more",
          le.deterministic_lock_in(77.5) is None and le.deterministic_lock_in(78.5) == NO)


def test_agrees_with_settles_yes_on_every_integer():
    """The property that makes this safe, asserted rather than argued.

    If the rule says NO, then NO true max at or above the observation may settle
    YES. Swept over every shape and every integer settlement value in range.
    """
    shapes = [
        mkt(floor_strike_f=f, cap_strike_f=f + 1.0, strike_type=STRIKE_BETWEEN)
        for f in range(60, 105)
    ] + [
        mkt(floor_strike_f=float(f), strike_type=STRIKE_GREATER) for f in range(60, 105)
    ] + [
        mkt(cap_strike_f=float(c), strike_type=STRIKE_LESS) for c in range(60, 105)
    ]
    bad_no = bad_yes = 0
    for m in shapes:
        for tenth in range(600, 1100):          # observed max, 0.1 degF steps
            observed = tenth / 10.0
            det = m.deterministic_lock_in(observed)
            if det is None:
                continue
            # Every integer settlement reachable from here: S = round(true_max)
            # for some true_max >= observed, i.e. every S >= round-half-up(observed).
            reachable = range(int(observed + 0.5), 121)
            if det == NO and any(m.settles_yes(float(s)) for s in reachable):
                bad_no += 1
            if det == YES and any(not m.settles_yes(float(s)) for s in reachable):
                bad_yes += 1
    check(f"no certain-NO is contradicted by a reachable settlement ({bad_no} violations)",
          bad_no == 0)
    check(f"no certain-YES is contradicted by a reachable settlement ({bad_yes} violations)",
          bad_yes == 0)


# --------------------------------------------------------------------------- #
# 5. Real detections with genuine margin must survive
# --------------------------------------------------------------------------- #
def test_genuine_detections_retained():
    """Margins at or above 0.5 degF -- 395 of the 564 replayed detections."""
    kept = [
        ("interior cap 84, running 85.0 (margin 1.0)", INTERIOR, 85.0, NO),
        ("interior cap 84, running 87.98 (margin 3.98)", INTERIOR, 87.98, NO),
        ("tail-low cap 78, running 80.06 (margin 2.06)", TAIL_LO, 80.06, NO),
        ("tail-low cap 78, running 96.08 (margin 18.08)", TAIL_LO, 96.08, NO),
        ("tail-high floor 87, running 89.0 (margin 2.0)", TAIL_HI, 89.0, YES),
    ]
    for name, m, observed, want in kept:
        check(f"retained: {name}", m.deterministic_lock_in(observed) == want)


def test_margin_is_monotone_and_configurable():
    check("margin 0.0 reproduces the OLD permissive behaviour at 84.02",
          INTERIOR.deterministic_lock_in(84.02, margin_f=0.0) == NO)
    check("a larger margin is strictly more conservative",
          INTERIOR.deterministic_lock_in(85.0, margin_f=2.0) is None
          and INTERIOR.deterministic_lock_in(86.5, margin_f=2.0) == NO)
    check("the default constant is 0.5", LOCKIN_MARGIN_F == 0.5)
    check("config exposes it and defaults to the constant",
          Settings(dry_run=True).risk.lockin_margin_f == LOCKIN_MARGIN_F)


def test_engine_uses_the_configured_margin():
    """The engine must read the knob, not the import-time default."""
    import inspect
    from weather_arb import arb_engine
    src = inspect.getsource(arb_engine)
    n_calls = src.count("deterministic_lock_in(")
    n_wired = src.count("margin_f=self._settings.risk.lockin_margin_f")
    check(f"every engine call site passes the configured margin ({n_wired}/{n_calls})",
          n_calls > 0 and n_wired == n_calls)


# --------------------------------------------------------------------------- #
# 6. Sensor-spike rejection is unchanged
# --------------------------------------------------------------------------- #
BASE = timeutils.parse_isoformat_utc("2026-08-16T13:00:00+00:00")
def at(minutes): return BASE + timedelta(minutes=minutes)
def C(f): return (f - 32.0) * 5.0 / 9.0


async def test_spike_guard_still_rejects():
    """The real KDEN 2026-08-16 138.2F reading must never reach daily_tmax.

    It matters here because the margin fix does NOT protect against this: a
    138.2F spike clears any margin. The glitch guard is the only defence, and
    replaying raw station_logs (which bypasses it) produces 2 wrong lock-ins.
    """
    with tempfile.TemporaryDirectory() as d:
        async with Database(os.path.join(d, "t.sqlite3")) as db:
            settings = Settings(dry_run=True)
            p = MetarPoller(session=None, db=db, settings=settings, on_new_high=None)
            station = settings.station_by_icao("KDEN")
            cdate = timeutils.climate_date_str(BASE, station.standard_utc_offset_hours)
            for i, t in enumerate((70.0, 76.0, 80.96)):
                await p._process(MetarObservation("KDEN", at(i * 60), C(t), C(50.0), "raw"))
            check("normal warming still validates", await db.get_tmax("KDEN", cdate) == 80.96)
            await p._process(MetarObservation("KDEN", at(180), C(138.2), C(50.0), "raw"))
            got = await db.get_tmax("KDEN", cdate)
            check(f"the 138.2F sensor spike is still rejected (max stayed {got})", got == 80.96)
            m = mkt(floor_strike_f=82.0, cap_strike_f=83.0, strike_type=STRIKE_BETWEEN)
            check("and so the spike cannot fire a lock-in",
                  m.deterministic_lock_in(got) is None)


async def main():
    test_sub_degree_does_not_lock()
    test_rounding_boundary_semantics()
    test_clear_margin_still_locks()
    test_tail_high_respects_margin()
    test_strict_and_non_strict_tails_preserved()
    test_agrees_with_settles_yes_on_every_integer()
    test_genuine_detections_retained()
    test_margin_is_monotone_and_configurable()
    test_engine_uses_the_configured_margin()
    await test_spike_guard_still_rejects()
    print("\n" + ("ALL PASS" if not failures else f"{len(failures)} FAILED: {failures}"))
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
