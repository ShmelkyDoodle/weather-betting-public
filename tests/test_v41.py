"""Favorite-bias calibration: the backward-looking price rule (offline).

WHAT THIS LOCKS DOWN.  `scripts/favorite_bias.py` asks whether Kalshi's
favourite-longshot bias beats its quadratic fee.  The answer is decided entirely
by HOW the price is sampled, and there are two ways to get a spectacular fake
result:

1. **Conditioning on a price the market REACHED.**  Every contract that settles
   YES passes through 96c on its way to 100c; only some eventual-NO contracts
   ever get there.  Sampling "markets that traded at 96c" therefore conditions on
   the outcome and reports a wildly inflated win rate.  The script samples at a
   FIXED horizon before close instead, which is outcome-independent.

2. **Taking a price from after the decision point.**  `_candle_index_at` returns
   the first candle at or AFTER a timestamp -- up to a full bar in the future.
   That is the same class of bug as the `period_interval` look-ahead that made
   every Strategy-2 backtest ~$0.81/trade optimistic.  `price_as_of` takes the
   last candle at or BEFORE the horizon, and refuses a stale one.
"""
import os as _o, sys as _s
_ROOT = _o.path.dirname(_o.path.dirname(_o.path.abspath(__file__)))
_s.path.insert(0, _ROOT); _s.path.insert(0, _o.path.join(_ROOT, "scripts"))

from favorite_bias import (BASE_FEE_COEFFICIENT, BUCKETS, FAVORITE_BAND, LONGSHOT_BAND,
                           MIN_LOSSES, MarketRow, SeriesMeta, bar_as_of, band_test,
                           cluster_stats, event_of, holm, net_edge, price_as_of,
                           select_markets, t_sf, _wilson)
from weather_arb.backtest import Candle
from weather_arb.fees import fee_cents

failures = []
def check(n, c):
    print(("PASS" if c else "FAIL"), "-", n)
    if not c: failures.append(n)


def _c(ts, yes_ask, no_ask):
    return Candle(ts=ts, yes_ask=yes_ask, no_ask=no_ask, yes_bid=None,
                  ask_low=None, bid_high=None, trade_low=None, trade_high=None,
                  volume=0.0)


CANDLES = [_c(1000, 40, 61), _c(2000, 55, 46), _c(3000, 96, 5), _c(4000, 99, 2)]


def test_price_is_strictly_backward_looking():
    # Exactly on a candle -> that candle.
    check("exact hit", price_as_of(CANDLES, 2000, 3600) == (55, 46))
    # Between candles -> the EARLIER one, never the later.
    ya, na = price_as_of(CANDLES, 2999, 3600)
    check("between candles takes the earlier", (ya, na) == (55, 46))
    check("it does NOT take the later candle", (ya, na) != (96, 5))
    # Past the end -> the last candle, but only if it is not stale.
    check("just past the end is allowed", price_as_of(CANDLES, 4500, 3600) == (99, 2))
    # Before the first candle -> nothing, not the first candle.
    check("before the series -> None", price_as_of(CANDLES, 500, 3600) == (None, None))
    check("empty series -> None", price_as_of([], 2000, 3600) == (None, None))


def test_stale_quote_is_refused():
    """A long trading gap must read as unknown, not as a live quote."""
    check("within tolerance", price_as_of(CANDLES, 4000 + 3600, 3600) == (99, 2))
    check("beyond tolerance -> None", price_as_of(CANDLES, 4000 + 3601, 3600) == (None, None))


def test_the_fee_really_is_quadratic():
    """The whole premise: the fee collapses at the extremes, so the same bias is
    harvestable at 98c and not at 50c."""
    per = {p: fee_cents(p, 100) / 100.0 for p in (50, 70, 90, 95, 98)}
    check("50c costs ~1.75c/contract", abs(per[50] - 1.75) < 0.02)
    check("98c costs <= 0.15c/contract", per[98] <= 0.15)
    check("fee falls monotonically from 50c to 98c",
          per[50] > per[70] > per[90] > per[95] > per[98])
    check("98c is at least 10x cheaper than 50c", per[50] / per[98] >= 10)


def test_buckets_are_declared_and_disjoint():
    """Pre-registered, and they must tile without overlap or a fishing gap."""
    check("buckets ascend and do not overlap",
          all(BUCKETS[i][1] + 1 == BUCKETS[i + 1][0] for i in range(len(BUCKETS) - 1)))
    check("they span the tradeable range 1..99",
          BUCKETS[0][0] == 1 and BUCKETS[-1][1] == 99)


def _obs(event, ask, won, coeff=BASE_FEE_COEFFICIENT, ticker="KXX-A-B", volume=1.0):
    return {"ticker": ticker, "event": event, "ask": ask, "won": won, "coeff": coeff,
            "series": "KXX", "category": "Test", "close_ts": 0, "bar_volume": volume}


def test_the_cluster_unit_is_kalshis_event_not_a_parsed_ticker():
    """⚠ The trap: `event_of` assumes the weather shape SERIES-DATE-STRIKE.

    A sports ticker like `KXNBASERIES-26-BOSLAL-BOS` collapses to
    `KXNBASERIES-26` under that rule, so an ENTIRE SEASON becomes one cluster --
    or, the other way, unrelated markets get merged and the standard error is
    computed over the wrong thing. The observation's own `event_ticker` is used.
    """
    check("event_of is right for the weather shape",
          event_of("KXHIGHMIA-26SEP08-B90.5") == "KXHIGHMIA-26SEP08")
    check("event_of MERGES a whole season on a 4-segment ticker",
          event_of("KXNBASERIES-26-BOSLAL-BOS") == "KXNBASERIES-26"
          and event_of("KXNBASERIES-26-NYKMIA-NYK") == "KXNBASERIES-26")
    # Same numbers, grouped two ways: 40 distinct events vs 1 merged blob.
    xs = [_obs(f"E{i}", 97, i % 2 == 0) for i in range(40)]
    n_ok, _, lo_ok, hi_ok = cluster_stats(xs)
    merged = [dict(o, event="KXNBASERIES-26") for o in xs]
    n_bad, _, lo_bad, hi_bad = cluster_stats(merged)
    check("distinct events give 40 clusters", n_ok == 40)
    check("mis-grouping collapses them to 1", n_bad == 1)
    check("and destroys the interval entirely",
          (hi_ok - lo_ok) < 1e9 and hi_bad == float("inf"))


def test_the_fee_coefficient_is_per_series():
    """⚠ 19 MLB series charge HALF fee and 14 charge NONE. Assuming 0.07 there
    understates the net edge -- in the harvestable direction, on heavily-traded
    series. Each observation carries its own coefficient."""
    full = net_edge(_obs("E", 98, True, coeff=0.07))
    half = net_edge(_obs("E", 98, True, coeff=0.035))
    free = net_edge(_obs("E", 98, True, coeff=0.0))
    check("half fee keeps strictly more than full fee", half > full)
    check("no fee keeps strictly more than half fee", free > half)
    check("a zero-coefficient series pays literally nothing",
          abs(free - (100 - 98)) < 1e-9)
    check("the full-fee 98c number is the documented ~0.14c",
          abs((free - full) - 0.14) < 0.01)
    check("SeriesMeta carries the coefficient, not a global constant",
          SeriesMeta("KXMLBF5", "Sports", "t", 0.035, "quadratic", "daily").coeff == 0.035)


def test_holm_controls_the_declared_family():
    """Thousands of cells guarantee a beautiful one by chance. Only the declared
    family is tested, and it is step-down adjusted."""
    adj = holm({"a": 0.001, "b": 0.02, "c": 0.30, "d": 0.9})
    check("smallest p is multiplied by the family size", abs(adj["a"] - 0.004) < 1e-12)
    check("second is multiplied by m-1", abs(adj["b"] - 0.06) < 1e-12)
    check("adjustment is monotone (step-down enforced)",
          adj["a"] <= adj["b"] <= adj["c"] <= adj["d"])
    check("nothing is adjusted below its raw p-value",
          all(adj[k] >= raw for k, raw in
              {"a": 0.001, "b": 0.02, "c": 0.30, "d": 0.9}.items()))
    check("a lone hypothesis is unchanged", holm({"a": 0.04}) == {"a": 0.04})
    # A 'significant' cell found by shopping 40 of them is not significant.
    shopped = {f"c{i}": (0.001 if i == 0 else 0.5) for i in range(40)}
    check("p=0.001 out of 40 shopped cells adjusts to non-significant",
          holm(shopped)["c0"] > 0.03)


def test_t_not_normal_for_thin_categories():
    """A normal tail is optimistic exactly where a false positive is tempting:
    a category with only 30-40 event clusters."""
    for df, t, want in ((10, 1.812, 0.05), (30, 1.697, 0.05), (100, 1.984, 0.025)):
        check(f"t_sf({t}, df={df}) matches the published table",
              abs(t_sf(t, df) - want) < 5e-4)
    check("the t tail is FATTER than the normal at small df", t_sf(1.96, 10) > 0.025)
    check("and converges to it at large df", abs(t_sf(1.96, 100000) - 0.025) < 1e-3)
    check("symmetric about zero", abs(t_sf(2.0, 20) + t_sf(-2.0, 20) - 1.0) < 1e-12)


def test_a_thin_band_is_reported_but_not_tested():
    """Pre-registered: fewer than 30 event clusters is not a test."""
    thin = band_test([_obs(f"E{i}", 97, True) for i in range(10)], +1)
    check("thin band is flagged untestable", thin["testable"] is False)
    # 200 events at 50c winning 60% of the time: +8.25c/contract before the
    # per-observation variance, which at these prices is enormous (~49c sd) --
    # which is exactly why the study needs clusters in the hundreds.
    fat = [_obs(f"E{i}", 50, i % 5 < 3) for i in range(200)]
    up = band_test(fat, +1)
    check("200 clusters is testable", up["testable"] is True)
    check("a one-sided p in the declared direction is small when the mean is +",
          up["mean"] > 0 and up["p"] < 0.05)
    down = band_test(fat, -1)
    check("the SAME data tested in the wrong direction is not significant",
          down["p"] > 0.95)
    check("the two one-sided p-values are complements",
          abs(up["p"] + down["p"] - 1.0) < 1e-9)


def test_selection_never_looks_at_the_outcome():
    """The selection rule must be outcome-independent or the whole study is
    conditioned on the result. Flipping every settlement must not move it."""
    rows = {}
    for si in range(3):
        rows[f"KXS{si}"] = [
            MarketRow(f"KXS{si}-{d:02d}-T", f"KXS{si}", f"KXS{si}-{d:02d}",
                      "yes" if d % 2 else "no", 1_000_000 + d * 3600, None,
                      float(100 * (si + 1)))
            for d in range(30)]
    cat = {f"KXS{si}": SeriesMeta(f"KXS{si}", "Test", "t", 0.07, "quadratic", "daily")
           for si in range(3)}
    picked, per_cat = select_markets(rows, cat, 2, 20, 10)
    flipped = {k: [r._replace(result=("no" if r.result == "yes" else "yes")) for r in v]
               for k, v in rows.items()}
    picked2, _ = select_markets(flipped, cat, 2, 20, 10)
    check("selection is identical when every outcome is flipped",
          [p.ticker for p in picked] == [p.ticker for p in picked2])
    check("it takes the top 2 series per category by volume", per_cat == {"Test": 2})
    check("it picks the HIGHEST-volume series, not the first",
          {p.series for p in picked} == {"KXS1", "KXS2"})
    check("the per-series cap is honoured", len(picked) == 20)
    closes = sorted(p.close_ts for p in picked if p.series == "KXS2")
    mid = 1_000_000 + 15 * 3600
    check("the sample spans both halves of the chronological split",
          any(c <= mid for c in closes) and any(c > mid for c in closes))
    check("a series under --min-markets is dropped entirely",
          select_markets(rows, cat, 2, 100, 10)[0] == [])


def test_bar_as_of_carries_volume_for_the_depth_caveat():
    """The ask is not a fill. The bar's traded volume is the only depth signal
    candles carry, so it has to survive to the observation."""
    bar = bar_as_of(CANDLES, 2999, 3600)
    check("bar_as_of returns the same bar price_as_of prices",
          (bar.yes_ask, bar.no_ask) == price_as_of(CANDLES, 2999, 3600))
    check("it exposes the bar's volume", bar.volume == 0.0)
    check("stale reads as no bar at all", bar_as_of(CANDLES, 4000 + 3601, 3600) is None)
    check("before the series -> no bar (a horizon longer than the market's life)",
          bar_as_of(CANDLES, 500, 3600) is None)


def test_the_tested_bands_are_the_extremes_of_the_declared_buckets():
    check("favorite band is the top declared bucket", FAVORITE_BAND == BUCKETS[-1])
    check("longshot band is the bottom declared bucket", LONGSHOT_BAND == BUCKETS[0])


def test_a_band_with_no_observed_losses_is_disowned():
    """⚠⚠ THE FALSE POSITIVE THAT EVERY OTHER GUARD WAVED THROUGH.

    The first exchange-wide run scored Entertainment's 96-99c band at +1.56c,
    95% CI [+1.32, +1.80], Holm-adjusted p = 0.0000 -- and it had ONE event out
    of 36 in which anything lost. At a 98c ask the payoff is +2c or -98c, so
    with no losses the sample variance is the variance of the WINS and the
    interval describes nothing. Holm cannot catch this: the problem is not the
    number of comparisons, it is that the statistic is degenerate.
    """
    clean = [_obs(f"E{i}", 98, True) for i in range(36)]
    r = band_test(clean, +1)
    check("a spotless band still reports a positive mean", r["mean"] > 0)
    check("but it is flagged fragile", r["fragile"] is True)
    check("its losing-event count is zero", r["losses"] == 0)
    check("ONE more loss flips it deeply negative", r["plus_one"] < 0)
    check("the Wilson interval admits the downside the t-interval hides",
          r["wlo"] < 0 < r["hi"])
    # Add real losses and the fragility flag clears.
    mixed = clean + [_obs(f"L{i}", 98, False) for i in range(MIN_LOSSES)]
    check("observed losses clear the flag", band_test(mixed, +1)["fragile"] is False)
    # A mixed event -- several near-certain NOs, one of which broke -- counts as
    # downside observed, not as a clean win.
    ev = [_obs("E", 98, True), _obs("E", 98, True), _obs("E", 98, False)]
    check("a mixed event counts as having seen a loss",
          band_test(ev, +1)["losses"] == 1)


def test_wilson_stays_sane_where_the_t_interval_collapses():
    lo, hi = _wilson(36, 36)
    check("zero failures does not give a zero-width rate interval", lo < 1.0)
    check("the upper bound is capped at 1", hi <= 1.0)
    check("more clean observations tighten it", _wilson(360, 360)[0] > lo)
    check("it is inside [0,1] at the other extreme",
          0.0 <= _wilson(0, 40)[0] and _wilson(0, 40)[1] <= 1.0)
    check("an empty band is fully uninformative", _wilson(0, 0) == (0.0, 1.0))


def main():
    test_price_is_strictly_backward_looking()
    test_stale_quote_is_refused()
    test_the_fee_really_is_quadratic()
    test_buckets_are_declared_and_disjoint()
    test_the_cluster_unit_is_kalshis_event_not_a_parsed_ticker()
    test_the_fee_coefficient_is_per_series()
    test_holm_controls_the_declared_family()
    test_t_not_normal_for_thin_categories()
    test_a_thin_band_is_reported_but_not_tested()
    test_selection_never_looks_at_the_outcome()
    test_bar_as_of_carries_volume_for_the_depth_caveat()
    test_the_tested_bands_are_the_extremes_of_the_declared_buckets()
    test_a_band_with_no_observed_losses_is_disowned()
    test_wilson_stays_sane_where_the_t_interval_collapses()
    print("\n" + ("ALL PASS" if not failures else f"{len(failures)} FAILED: {failures}"))
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
