"""#0 The bucket grid: pricing integrates over the region that ROUNDS into a bucket.

Kalshi lists INTEGER strikes and settles on a whole-degF value, so an interior
bucket [F, F+1] covers two integers = [F-0.5, F+1.5] of real temperature. Pricing
it as a 1F window lost ~30% of the probability mass (the six buckets of an event
summed to 0.70, not 1.0).

The old tests only ever called `_truncated_normal_between` with hand-written
bounds, so nothing exercised the geometry `_estimate` actually uses. These close
that gap: they drive the REAL production path and assert the invariant that
matters -- the six mutually-exclusive, collectively-exhaustive buckets of an
event must sum to exactly 1.
"""
import math
import os as _osp, sys
sys.path.insert(0, _osp.path.dirname(_osp.path.dirname(_osp.path.abspath(__file__))))
from weather_arb.config import Settings
from weather_arb.forecast_ingest import ForecastClient
from weather_arb.models import (
    STRIKE_BETWEEN, STRIKE_GREATER, STRIKE_GREATER_EQ, STRIKE_LESS, STRIKE_LESS_EQ,
    Venue, WeatherMarket,
)

failures = []
def check(n, c):
    print(("PASS" if c else "FAIL"), "-", n)
    if not c: failures.append(n)


def mk(floor=None, cap=None, kind="", ticker="T"):
    return WeatherMarket(ticker=ticker, venue=Venue.KALSHI, icao="KNYC",
                         floor_strike_f=floor, cap_strike_f=cap, strike_type=kind)


def real_event(floors):
    """A real Kalshi event: <lo tail, len(floors) two-integer buckets, >hi tail."""
    ms = [mk(cap=floors[0], kind=STRIKE_LESS, ticker="tail-lo")]
    ms += [mk(floor=f, cap=f + 1, kind=STRIKE_BETWEEN, ticker=f"B{f}") for f in floors]
    ms.append(mk(floor=floors[-1] + 1, kind=STRIKE_GREATER, ticker="tail-hi"))
    return ms


def test_interval_geometry():
    lo, hi = mk(floor=89, cap=90, kind=STRIKE_BETWEEN).settlement_interval_f()
    check("interior [89,90] -> [88.5, 90.5]", (lo, hi) == (88.5, 90.5))
    check("interior spans 2 degF, not 1", abs((hi - lo) - 2.0) < 1e-12)

    lo, hi = mk(cap=89, kind=STRIKE_LESS).settlement_interval_f()
    check("strict less cap=89 (T<=88) -> [-inf, 88.5]", lo == -math.inf and hi == 88.5)
    lo, hi = mk(floor=96, kind=STRIKE_GREATER).settlement_interval_f()
    check("strict greater floor=96 (T>=97) -> [96.5, inf]", lo == 96.5 and hi == math.inf)

    # Non-strict variants exist for back-compat and must move the OTHER way.
    lo, _ = mk(floor=90, kind=STRIKE_GREATER_EQ).settlement_interval_f()
    check("greater_or_equal floor=90 (T>=90) -> lo 89.5", lo == 89.5)
    _, hi = mk(cap=90, kind=STRIKE_LESS_EQ).settlement_interval_f()
    check("less_or_equal cap=90 (T<=90) -> hi 90.5", hi == 90.5)

    # A bare mk() is NOT strike-less: floor_f falls back to strike_temp_f (0.0),
    # so it is a tail-high at 0F -- and settles_yes agrees, which is the property
    # that matters. The genuinely reachable empty case is 'between' with no strikes.
    bare = mk()
    check("bare market matches settles_yes (tail-high at 0F)",
          bare.settlement_interval_f()[0] == -0.5 and bare.settles_yes(50.0))
    empty = mk(kind=STRIKE_BETWEEN)
    lo, hi = empty.settlement_interval_f()
    check("strike-less 'between' -> empty interval (never YES)",
          hi < lo and not empty.settles_yes(50.0))


def test_interval_agrees_with_settles_yes():
    """The pricing grid and the settlement rule must not disagree on any integer."""
    bad = []
    for ms in (real_event([89, 91, 93, 95]), real_event([70, 72, 74, 76])):
        for m in ms:
            lo, hi = m.settlement_interval_f()
            for s in range(60, 110):                 # every plausible settled integer
                priced = lo <= s <= hi
                settles = m.settles_yes(float(s))
                if priced != settles:
                    bad.append((m.ticker, s, priced, settles))
    check("interval matches settles_yes on every integer", not bad)
    if bad:
        print("   disagreements:", bad[:6])


def test_event_probabilities_sum_to_one():
    """The invariant the old grid violated. Drives the REAL `_estimate` path."""
    settings = Settings.from_env()
    fc = ForecastClient.__new__(ForecastClient)      # pure method; no I/O needed
    fc._settings = settings
    station = settings.station_by_icao("KNYC")

    sums = []
    for floors in ([89, 91, 93, 95], [70, 72, 74, 76], [100, 102, 104, 106]):
        ms = real_event(floors)
        for mu, sigma, obs in ((90.0, 1.0, 90.0), (84.3, 2.6, 80.0), (95.0, 5.5, None),
                               (72.0, 0.5, 71.0), (105.0, 3.0, 103.0)):
            total = sum(fc._estimate(station, m, mu, sigma, obs).probability for m in ms)
            sums.append(total)
    check(f"every event sums to 1.0 (n={len(sums)})",
          all(abs(t - 1.0) < 1e-9 for t in sums))
    print(f"   sums: min {min(sums):.9f}  max {max(sums):.9f}")

    # The old grid is what these numbers must NOT look like.
    ms = real_event([89, 91, 93, 95])
    old = sum(
        __import__("weather_arb.forecast_ingest", fromlist=["x"])._truncated_normal_between(
            90.0, 1.0,
            m.floor_f if m.floor_f is not None else -math.inf,
            m.cap_f if m.cap_f is not None else math.inf, 90.0)
        for m in ms)
    check("the pre-fix grid really did leak mass", old < 0.80)
    print(f"   pre-fix sum at the same inputs: {old:.3f}")


def test_truncation_still_bounds_below():
    """R = settlement - observed max is never negative, so no mass below the max."""
    settings = Settings.from_env()
    fc = ForecastClient.__new__(ForecastClient)
    fc._settings = settings
    station = settings.station_by_icao("KNYC")
    ms = real_event([89, 91, 93, 95])
    obs = 93.0
    est = {m.ticker: fc._estimate(station, m, 92.0, 1.0, obs).probability for m in ms}
    check("buckets entirely below the observed max get 0",
          est["tail-lo"] == 0.0 and est["B89"] == 0.0 and est["B91"] == 0.0)
    check("still sums to 1 under truncation",
          abs(sum(est.values()) - 1.0) < 1e-9)


for fn in (test_interval_geometry, test_interval_agrees_with_settles_yes,
           test_event_probabilities_sum_to_one, test_truncation_still_bounds_below):
    print(f"\n-- {fn.__name__}")
    fn()

print("\n" + ("ALL PASS" if not failures else f"FAILURES: {failures}"))
sys.exit(1 if failures else 0)
