"""Settlement-basis re-audit: the certainty proof's real failure mode (offline).

WHAT THIS LOCKS DOWN.  ``CLAUDE.md`` has twice recorded a safety claim for
Strategy 1 that did not survive contact with more data -- first "421 detections,
0 wrong calls" (which had graded 1 of 421), then "R = CLI - METAR max is never
negative, n=245" (which was an artifact of rounding before differencing).  The
2026-10-30 re-audit over 693 settled events found the claim standing, but
standing on a completely different support than the one written down.  These
tests pin that support, because it is load-bearing and easy to refactor away.

1. **THE ASYMMETRY IS THE WHOLE SAFETY ARGUMENT.**  Every branch of
   ``deterministic_lock_in`` proves its outcome by showing the settlement must be
   at or ABOVE a boundary.  So a settlement that comes in higher than the truth
   is harmless and one that comes in lower is fatal -- in all three branches, with
   no exceptions.  ``test_only_a_downward_settlement_error_can_break_a_lockin``
   asserts both halves against the production rule, which is why "how often does
   Kalshi disagree with the CLI" is the wrong question and "how often does it
   settle BELOW what we observed" is the right one.

2. **THE GUARD, NOT THE BASIS, IS WHAT MAKES IT SAFE.**  The single wrong call in
   1,468 replayed detections is KNYC 2026-08-27: a 10.08 degF spike in 9 minutes
   during half-mile heavy rain, with the METAR's own ``$`` maintenance flag set,
   which fell back 9 degF eight minutes later.  NWS QC removed it (CLI 77) and
   Kalshi settled 77, so a certain-NO on ``T80`` at the spiked max would have
   lost **-$185** on 189 contracts.  It never fired, because the kinematic guard
   rejected the reading at 67.2 degF/hour.  ``test_the_knyc_spike_that_would_have
   _cost_185_dollars`` replays that exact sequence through production's
   ``plausible_rise`` and asserts both halves: the guard rejects it, and the
   lock-in rule WOULD have fired on it. Weaken the guard and this loss comes back.

3. The audit reads ``market_results`` to reach past Kalshi's ~70-day retention
   cliff, reconstructing strike geometry from tickers because that table stores
   only yes/no.  ``T<n>`` is the ``less`` tail at an event's bottom and the
   ``greater`` tail at its top, so getting it backwards would silently invert
   every tail call.  Validated against Kalshi's own API rows on 469/469
   overlapping events; this test pins the property offline.

4. A zero-failure sample must not be reported as certainty.  This repo already
   published one degenerate interval (Entertainment's 96-99c band, CI [+1.32,
   +1.80] from a sample with ONE loss).  0/940 is the same shape.
"""
import math
import os as _o, sqlite3, sys as _s, tempfile
from datetime import datetime, timedelta, timezone

_ROOT = _o.path.dirname(_o.path.dirname(_o.path.abspath(__file__)))
_s.path.insert(0, _ROOT); _s.path.insert(0, _o.path.join(_ROOT, "scripts"))

from settlement_audit import _db_events, lockin_pnl_cents, market_of, wilson

from weather_arb.metar_ingest import max_plausible_rise_f, plausible_rise
from weather_arb.models import LOCKIN_MARGIN_F, Side, Venue, WeatherMarket
from weather_arb.settlement_basis import settled_range, tiles_integers

failures = []
def check(n, c):
    print(("PASS" if c else "FAIL"), "-", n)
    if not c: failures.append(n)


def _mk(strike_type, floor=None, cap=None, ticker="T"):
    return WeatherMarket(ticker=ticker, venue=Venue.KALSHI, icao="KNYC",
                         floor_strike_f=floor, cap_strike_f=cap,
                         strike_type=strike_type)


# --------------------------------------------------------------------------- #
# 1. The asymmetry that the whole safety argument rests on
# --------------------------------------------------------------------------- #
def test_only_a_downward_settlement_error_can_break_a_lockin():
    """Upward settlement errors preserve every branch; downward break every one.

    The three shapes Kalshi lists, each at its exact certainty threshold.  The
    "truth" is round(observed); then settlement is perturbed up and down and the
    call is re-graded with production's own ``settles_yes``.
    """
    cases = [
        # (market, observed max at the certainty threshold, expected side)
        (_mk("between", floor=83.0, cap=84.0), 84.5, Side.NO),
        (_mk("less", cap=80.0), 79.5, Side.NO),
        (_mk("greater", floor=84.0), 84.5, Side.YES),
    ]
    up_ok = down_breaks = 0
    for market, observed, want_side in cases:
        call = market.deterministic_lock_in(observed, margin_f=LOCKIN_MARGIN_F)
        check(f"{market.kind}: fires at the threshold {observed}",
              call is not None and call[0] is want_side)
        if call is None:
            continue
        # Settlement rounds HALF-UP, so the truth at an exact .5 threshold is the
        # integer above. Python's round() is banker's rounding and returns the
        # even neighbour -- round(84.5) == 84 -- which this repo has recorded as a
        # trap and which silently inverted this very assertion on first write.
        truth = math.floor(observed + 0.5)

        def correct_at(settlement):
            yes = market.settles_yes(float(settlement))
            return yes if want_side is Side.YES else not yes

        check(f"{market.kind}: correct when settlement == the truth ({truth})",
              correct_at(truth))
        # Upward: every error above the truth must leave the conclusion intact.
        if all(correct_at(truth + d) for d in range(1, 11)):
            up_ok += 1
        # Downward: some error below the truth must break it.
        if any(not correct_at(truth - d) for d in range(1, 11)):
            down_breaks += 1

    check("upward settlement error is harmless in ALL three branches", up_ok == 3)
    check("downward settlement error is fatal in ALL three branches", down_breaks == 3)


def test_the_margin_is_what_absorbs_a_coarse_observation():
    """A whole-degC reading is 1.8 degF wide; the 0.5 margin must cover the half-step.

    KMDW 2026-09-20 is the real case: the max was 69.8 degF (= 21.0 degC exactly,
    a body-group reading with no T-group), Kalshi settled 69, so R = -0.8.  The
    certain-NO on ``T69`` still graded CORRECT -- but by exactly one integer, with
    the proof's own margin fully consumed.  Pinning it documents the headroom.
    """
    t69 = _mk("less", cap=69.0, ticker="KXHIGHCHI-26SEP20-T69")
    call = t69.deterministic_lock_in(69.8, margin_f=LOCKIN_MARGIN_F)
    check("KMDW 09-20: certain-NO on T69 fires at the observed 69.8",
          call is not None and call[0] is Side.NO)
    check("KMDW 09-20: it was right -- 69 is NOT <= 68", not t69.settles_yes(69.0))
    check("KMDW 09-20: and it had ZERO slack -- 68 would have settled it YES",
          t69.settles_yes(68.0))

    # The margin is exactly the rounding half-width, so lowering it re-admits the
    # sub-degree band that produced 18 wrong calls before 2026-08-26.
    tight = _mk("between", floor=83.0, cap=84.0)
    check("margin 0.5 refuses the ambiguous 84.02 that settles YES at 84",
          tight.deterministic_lock_in(84.02, margin_f=0.5) is None)
    check("margin 0.0 (the retired rule) wrongly calls 84.02 a certain NO",
          tight.deterministic_lock_in(84.02, margin_f=0.0) is not None)


# --------------------------------------------------------------------------- #
# 2. The spike that the guard, and only the guard, stopped
# --------------------------------------------------------------------------- #
def test_the_knyc_spike_that_would_have_cost_185_dollars():
    """KNYC 2026-08-27 19:51Z, replayed through the production guard.

    METAR KNYC 271951Z AUTO 1/2SM +RA ... 27/21 ... T02670211 $
    preceded by 69.98 degF at 19:42Z and followed by 71.06 degF at 19:59Z.
    Kalshi settled 77 (= the CLI, which QC'd the spike out) and ``T80`` paid YES.
    """
    at = lambda hh, mm: datetime(2026, 8, 27, hh, mm, tzinfo=timezone.utc)
    trusted_f, trusted_at = 69.98, at(19, 42)
    spike_f, spike_at = 80.06, at(19, 51)

    # Rule 1 -- kinematic plausibility. 10.08 degF in 9 minutes = 67.2 degF/hour.
    elapsed = (spike_at - trusted_at).total_seconds()
    allowance = max_plausible_rise_f(elapsed)
    check("the guard REJECTS the spike (rule 1: implausible warming rate)",
          not plausible_rise(trusted_f, trusted_at, spike_f, spike_at))
    check(f"rise {spike_f - trusted_f:.2f} degF far exceeds the {allowance:.2f} degF allowance",
          (spike_f - trusted_f) > allowance * 4)

    # Rule 2 -- a held spike needs re-confirmation within the window; the next
    # reading is 8 minutes later AND 9 degF lower, so it never confirms.
    next_f, next_at = 71.06, at(19, 59)
    check("no confirmation arrives: the next reading is 9 degF BELOW the spike",
          next_f < spike_f - 8.0)
    check("and it lands outside the 5-minute confirmation window",
          (next_at - spike_at).total_seconds() > 300.0)

    # The counterfactual: had the spike been trusted, the rule WOULD have fired.
    t80 = _mk("less", cap=80.0, ticker="KXHIGHNY-26AUG27-T80")
    call = t80.deterministic_lock_in(spike_f, margin_f=LOCKIN_MARGIN_F)
    check("on the spiked max, certain-NO on T80 WOULD have fired",
          call is not None and call[0] is Side.NO)
    check("and it would have been WRONG -- Kalshi settled 77, so T80 paid YES",
          t80.settles_yes(77.0))
    loss = lockin_pnl_cents(98, False, 189) / 100.0
    check(f"the avoided loss is ~$185 on the real 189-contract size (got ${-loss:.2f})",
          -186.0 < loss < -184.0)

    # The guard must stay strict at this timescale. A 9-minute gap is inside the
    # rate-limited regime, not the sub-minute SPECI exemption.
    check("a 9-minute gap is rate-limited, not treated as a SPECI burst",
          allowance < 3.0)


# --------------------------------------------------------------------------- #
# 3. Reaching past Kalshi's retention cliff without inverting the tails
# --------------------------------------------------------------------------- #
def test_ledger_geometry_reconstruction_gets_the_tails_right():
    """``market_results`` stores only yes/no; geometry comes from the ticker."""
    rows = [
        ("KXHIGHAUS-26JUL21-T95", "no"),       # bottom tail -> less, cap 95
        ("KXHIGHAUS-26JUL21-B95.5", "no"),     # -> between [95, 96]
        ("KXHIGHAUS-26JUL21-B97.5", "no"),
        ("KXHIGHAUS-26JUL21-B99.5", "no"),
        ("KXHIGHAUS-26JUL21-B101.5", "yes"),   # -> between [101, 102]
        ("KXHIGHAUS-26JUL21-T102", "no"),      # top tail -> greater, floor 102
    ]
    with tempfile.TemporaryDirectory() as d:
        path = _o.path.join(d, "t.sqlite3")
        conn = sqlite3.connect(path)
        conn.execute("CREATE TABLE market_results (ticker TEXT PRIMARY KEY, "
                     "result TEXT NOT NULL)")
        conn.executemany("INSERT INTO market_results VALUES (?, ?)", rows)
        conn.commit(); conn.close()
        events = _db_events(path)

    markets = events.get("KXHIGHAUS-26JUL21", [])
    by_ticker = {m["ticker"]: m for m in markets}
    check("all six markets reconstructed", len(markets) == 6)
    check("the LOW T is the `less` tail, cap 95",
          by_ticker["KXHIGHAUS-26JUL21-T95"]["strike_type"] == "less"
          and by_ticker["KXHIGHAUS-26JUL21-T95"]["cap"] == 95)
    check("the HIGH T is the `greater` tail, floor 102",
          by_ticker["KXHIGHAUS-26JUL21-T102"]["strike_type"] == "greater"
          and by_ticker["KXHIGHAUS-26JUL21-T102"]["floor"] == 102)
    check("B101.5 is the two integers {101, 102}, not a 1-degF window",
          by_ticker["KXHIGHAUS-26JUL21-B101.5"]["floor"] == 101
          and by_ticker["KXHIGHAUS-26JUL21-B101.5"]["cap"] == 102)
    check("the reconstruction tiles the integers exactly once",
          tiles_integers(markets))
    check("the YES market pins the settled range to {101, 102}",
          settled_range(markets) == (101, 102))

    # Round-trip through the production market type: the tails must stay strict.
    greater = market_of(by_ticker["KXHIGHAUS-26JUL21-T102"])
    check("the `greater` tail stays STRICT: 102 is NO, 103 is YES",
          not greater.settles_yes(102.0) and greater.settles_yes(103.0))
    less = market_of(by_ticker["KXHIGHAUS-26JUL21-T95"])
    check("the `less` tail stays STRICT: 95 is NO, 94 is YES",
          not less.settles_yes(95.0) and less.settles_yes(94.0))


# --------------------------------------------------------------------------- #
# 4. A clean sample is not a proof
# --------------------------------------------------------------------------- #
def test_zero_wrong_calls_is_not_reported_as_certainty():
    lo, hi = wilson(0, 940)
    check("0/940 wrong gives a lower bound of 0", lo == 0.0)
    check("but the upper bound stays positive -- no certainty claimed",
          0.0 < hi < 0.01)
    check("it is not mistaken for a tight interval around zero", hi > 0.003)

    # The economics: one bad settlement undoes ~49 good lock-ins.
    win = lockin_pnl_cents(98, True, 189)
    loss = lockin_pnl_cents(98, False, 189)
    breakeven = win / (win - loss)
    check("breakeven wrong-rate is ~2%", 0.015 < breakeven < 0.025)
    check("one wrong call costs ~49x what a right one earns",
          48.0 < (-loss / win) < 54.0)
    check("the Wilson upper bound on 0/940 sits BELOW breakeven, so the record "
          "is consistent with a positive edge", hi < breakeven)


def main():
    test_only_a_downward_settlement_error_can_break_a_lockin()
    test_the_margin_is_what_absorbs_a_coarse_observation()
    test_the_knyc_spike_that_would_have_cost_185_dollars()
    test_ledger_geometry_reconstruction_gets_the_tails_right()
    test_zero_wrong_calls_is_not_reported_as_certainty()
    print("\n" + ("ALL PASS" if not failures else f"{len(failures)} FAILED: {failures}"))
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
