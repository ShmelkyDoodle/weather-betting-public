"""#B Settlement-basis measurement + sigma-floor derivation (offline, pure).

Locks down the facts backlog B turned up, so a future session cannot quietly
re-derive them wrong:
  * Kalshi's six buckets tile the INTEGERS, and both tails are STRICT.
  * The settled integer is recovered from the single YES market.
  * The half-normal MLE (= RMS) is what the floor's derivation rests on.
  * The current bucket grid loses ~30% of the probability mass; the half-degree
    grid conserves it exactly.
"""
import math
import os as _osp, sys
sys.path.insert(0, _osp.path.dirname(_osp.path.dirname(_osp.path.abspath(__file__))))
from weather_arb.settlement_basis import (
    HALF_NORMAL_MEAN, HALF_NORMAL_SD, bucket_bounds, bucket_prob, event_date,
    half_normal_nll, half_normal_scale, settled_range, tiles_integers,
)

failures = []
def check(n, c):
    print(("PASS" if c else "FAIL"), "-", n)
    if not c: failures.append(n)


def event(floors, yes_index):
    """A real Kalshi event shape: <lo tail, 4 two-integer buckets, >hi tail."""
    ms = [{"strike_type": "less", "floor": None, "cap": floors[0], "result": "no"}]
    for f in floors:
        ms.append({"strike_type": "between", "floor": f, "cap": f + 1, "result": "no"})
    ms.append({"strike_type": "greater", "floor": floors[-1] + 1, "cap": None,
               "result": "no"})
    ms[yes_index]["result"] = "yes"
    return ms


def test_ticker_dates():
    check("ticker date parses to the climate day",
          event_date("KXHIGHNY-26AUG22-B77.5") == "2026-08-22")
    check("single-digit day parses", event_date("KXHIGHDEN-26JUL05-T96") == "2026-07-05")
    check("garbage returns None", event_date("NOTATICKER") is None)
    check("bad month returns None", event_date("KXHIGHNY-26XXX22-B1") is None)


def test_integer_tiling():
    # KXHIGHDEN-26AUG18: <=88, {89,90}, {91,92}, {93,94}, {95,96}, >=97
    ms = event([89, 91, 93, 95], 0)
    check("a real event tiles the integers", tiles_integers(ms))
    check("6 markets per event", len(ms) == 6)

    gap = event([89, 91, 93, 95], 0)
    gap[3]["floor"], gap[3]["cap"] = 92, 93      # overlaps the bucket below
    check("an overlapping grid is rejected", not tiles_integers(gap))

    wide = event([89, 91, 93, 95], 0)
    wide[2]["cap"] = 91                          # a 3-integer bucket
    check("a mis-sized bucket is rejected", not tiles_integers(wide))


def test_settled_range_is_strict_at_the_tails():
    check("less cap=89 means T <= 88",
          settled_range(event([89, 91, 93, 95], 0)) == (None, 88))
    check("between [89,90] means T in {89,90}",
          settled_range(event([89, 91, 93, 95], 1)) == (89, 90))
    check("greater floor=96 means T >= 97",
          settled_range(event([89, 91, 93, 95], 5)) == (97, None))
    ms = event([89, 91, 93, 95], 0)
    for m in ms:
        m["result"] = "no"
    check("no YES market yields None", settled_range(ms) is None)


def test_bucket_grid_conserves_mass():
    """The six probabilities must sum to 1 -- the buckets tile and one settles YES."""
    ms = event([89, 91, 93, 95], 1)
    # Sweep the running max across a bucket: the leak is position-dependent, so a
    # single point would understate it. Measured on real events the mean sum is
    # 0.61; here it ranges 0.27-0.71 depending on where the observation lands.
    runs = [89.96, 90.0, 90.5, 91.04, 91.98]
    cur = [sum(bucket_prob(r, 1.0, *bucket_bounds(m, False), r) for m in ms) for r in runs]
    half = [sum(bucket_prob(r, 1.0, *bucket_bounds(m, True), r) for m in ms) for r in runs]
    check("half-degree grid conserves mass exactly, everywhere",
          all(abs(t - 1.0) < 1e-9 for t in half))
    check("current grid leaks mass at every position",
          all(t < 0.80 for t in cur))
    check("current grid's leak is severe near a bucket edge", min(cur) < 0.35)

    # The leak is structural: a 'between' bucket covers two integers but only
    # 1.0 degF of continuous space under today's convention.
    m = {"strike_type": "between", "floor": 89, "cap": 90}
    lo0, hi0 = bucket_bounds(m, False)
    lo1, hi1 = bucket_bounds(m, True)
    check("today's between-bucket is 1 degF wide", abs((hi0 - lo0) - 1.0) < 1e-12)
    check("the integers it covers are 2 degF wide", abs((hi1 - lo1) - 2.0) < 1e-12)

    # Tails must not overlap the neighbouring bucket under the corrected grid.
    less_hi = bucket_bounds({"strike_type": "less", "floor": None, "cap": 89}, True)[1]
    btw_lo = bucket_bounds({"strike_type": "between", "floor": 89, "cap": 90}, True)[0]
    check("corrected tail abuts the first bucket exactly", abs(less_hi - btw_lo) < 1e-12)


def test_half_normal_estimator():
    check("MLE scale of a half-normal is the RMS",
          abs(half_normal_scale([0.0, 1.0, 1.0, 2.0]) - math.sqrt(6 / 4)) < 1e-12)
    check("empty sample yields 0", half_normal_scale([]) == 0.0)

    # The moment identities the derivation quotes.
    check("half-normal mean factor", abs(HALF_NORMAL_MEAN - 0.79788) < 1e-4)
    check("half-normal sd factor", abs(HALF_NORMAL_SD - 0.60280) < 1e-4)

    # NLL is minimised at the MLE -- this is what makes RMS the right estimator.
    sample = [0.0, 1.0, 1.0, 2.0, 1.0, 0.0, 2.0, 1.0]
    s_hat = half_normal_scale(sample)
    best = min((half_normal_nll(sample, s), s)
               for s in (0.05 * k for k in range(1, 80)))[1]
    check("NLL is minimised at the RMS", abs(best - s_hat) < 0.06)
    check("degenerate scale is rejected", half_normal_nll(sample, 0.0) == math.inf)


def test_truncation_supplies_the_mean_shift():
    """Why an explicit bias on top of the floor over-corrects.

    Truncating at the running max turns N(run, s) into a half-normal, whose mean
    is already run + 0.798*s. At s=1.0 that is +0.80 -- essentially the measured
    +0.81 residual. The floor is doing the recentring, not just the widening.
    """
    run, s = 90.0, 1.0
    p_here = bucket_prob(run, s, *bucket_bounds(
        {"strike_type": "between", "floor": 89, "cap": 90}, True), run)
    p_next = bucket_prob(run, s, *bucket_bounds(
        {"strike_type": "between", "floor": 91, "cap": 92}, True), run)
    check("implied mean shift is ~0.8 degF", abs(HALF_NORMAL_MEAN * s - 0.798) < 1e-3)
    # Measured residual: P(R=0)=0.35, P(R>=1)=0.65. So with the running max on a
    # bucket's lower edge the bucket ABOVE should lead -- and it does. This is
    # why adding an explicit bias on top of the floor over-corrects.
    check("the bucket above the running max leads", p_next > p_here)
    check("both stay in the plausible range", 0.3 < p_here < 0.5 and 0.5 < p_next < 0.7)
    check("no mass below the running max",
          bucket_prob(run, s, *bucket_bounds(
              {"strike_type": "less", "floor": None, "cap": 89}, True), run) == 0.0)


for fn in (test_ticker_dates, test_integer_tiling,
           test_settled_range_is_strict_at_the_tails, test_bucket_grid_conserves_mass,
           test_half_normal_estimator, test_truncation_supplies_the_mean_shift):
    print(f"\n-- {fn.__name__}")
    fn()

print("\n" + ("ALL PASS" if not failures else f"FAILURES: {failures}"))
sys.exit(1 if failures else 0)
