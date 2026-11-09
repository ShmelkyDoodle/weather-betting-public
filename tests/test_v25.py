"""#5 passive (maker) placement: honest resting-fill simulation (offline).

The whole credibility of a passive backtest is the fill model -- assume fills and
it manufactures a win. These lock down that an order only fills when the market
actually came to it, never from a print that preceded the order.
"""
import sys
from datetime import datetime, timezone
import os as _osp; sys.path.insert(0, _osp.path.dirname(_osp.path.dirname(_osp.path.abspath(__file__))))
from weather_arb.arb_engine import edge_limit_prices
from weather_arb.backtest import (
    Candle, _matched_pairs, _passive_fill_index, _passive_quote, _simulate_passive, _spread_at,
)

failures = []
def check(n, c):
    print(("PASS" if c else "FAIL"), "-", n)
    if not c: failures.append(n)


def _ts(m): return int(datetime(2026, 8, 1, 12, m, tzinfo=timezone.utc).timestamp())
def _iso(m): return datetime(2026, 8, 1, 12, m, tzinfo=timezone.utc).isoformat()


def _bar(m, *, bid=85, ask=90, ask_low=89, bid_high=86, lo=88, hi=91, vol=200.0):
    return Candle(_ts(m), ask, 100 - bid, bid, ask_low, bid_high, lo, hi, vol)


def test_edge_limit_prices():
    # Shared with the live engine: 90% model prob, 15% threshold, 7c fee coefficient.
    buy, sell, yp, np_ = edge_limit_prices(0.90, 0.15, 0.07)
    check("buy limit is fee-adjusted (73c)", buy == 73)
    check("sell limit clamps to 99c", sell == 99)
    check("probabilities pass through", abs(yp - 0.90) < 1e-9 and abs(np_ - 0.10) < 1e-9)


def test_passive_fill_index():
    # Resting BUY YES at 73c. Offer never reaches it, but a trade prints at 70c.
    bars = [_bar(1, ask_low=80, lo=70, hi=88, vol=200.0)]
    check("trade print at/below the bid fills it",
          _passive_fill_index(bars, 0, 0, "yes", 73, "trade", 100) == 0)
    check("quote that never reached us does not fill 'touch'",
          _passive_fill_index(bars, 0, 0, "yes", 73, "touch", 100) is None)
    check("strictly-through print with depth fills 'through'",
          _passive_fill_index(bars, 0, 0, "yes", 73, "through", 100) == 0)
    thin = [_bar(1, ask_low=80, lo=70, hi=88, vol=40.0)]
    check("'through' refuses a period thinner than our own size",
          _passive_fill_index(thin, 0, 0, "yes", 73, "through", 100) is None)
    check("'trade' accepts the thin period", 
          _passive_fill_index(thin, 0, 0, "yes", 73, "trade", 100) == 0)
    at_price = [_bar(1, ask_low=80, lo=73, hi=88, vol=200.0)]
    check("'trade' fills at exactly our price",
          _passive_fill_index(at_price, 0, 0, "yes", 73, "trade", 100) == 0)
    check("'through' needs strictly past our price",
          _passive_fill_index(at_price, 0, 0, "yes", 73, "through", 100) is None)
    # Resting SELL YES at 73c (= buying NO at 27c) fills on a print at/above 73.
    up = [_bar(1, lo=60, hi=75, vol=200.0)]
    check("sell side fills on a print above it",
          _passive_fill_index(up, 0, 0, "no", 73, "trade", 100) == 0)
    check("sell side unfilled when prints stay below",
          _passive_fill_index([_bar(1, lo=60, hi=70)], 0, 0, "no", 73, "trade", 100) is None)
    check("empty scan range yields no fill",
          _passive_fill_index(bars, 1, 0, "yes", 73, "trade", 100) is None)


def test_passive_quote():
    q = _passive_quote(0.90, 0.15, 0.07, yes_bid=85, yes_ask=90, improve=1)
    check("rests a YES bid at the fee-adjusted limit", q == ("yes", 73, 0.90))
    q0 = _passive_quote(0.90, 0.15, 0.07, yes_bid=60, yes_ask=65, improve=1)
    check("improves the bid when the book is below our limit", q0 == ("yes", 61, 0.90))
    check("never crosses the offer",
          _passive_quote(0.90, 0.15, 0.07, yes_bid=70, yes_ask=72, improve=5)[1] == 71)
    check("price floor rejects both legs",
          _passive_quote(0.90, 0.15, 0.07, yes_bid=85, yes_ask=90, improve=1,
                         min_price_cents=80) is None)


def _fixture(bars, prob=0.90, result="yes"):
    snaps = {("T1", "m"): [{"created_at": _iso(0), "prob": prob, "date": "2026-08-01"}]}
    return snaps, {"T1": result}, {"T1": bars}


def test_unfilled_resting_order_is_not_a_trade():
    # The load-bearing property: the market never trades down to our 73c bid.
    bars = [_bar(0), _bar(1), _bar(2)]
    snaps, res, cache = _fixture(bars)
    per, _n, stats = _simulate_passive(snaps, res, cache, 0.15, 0.07, 100,
                                       fill_model="trade", improve=1, cross=False)
    check("no fill -> no trade row", per == {})
    check("but the order is counted as rested", stats["rested"] == 1)
    check("and counted as unfilled", stats["unfilled"] == 1)


def test_fill_is_never_credited_to_the_pricing_bar():
    # A deep print in the SAME bar the order was priced off would predate the order.
    early = [_bar(0, lo=10), _bar(1), _bar(2)]
    snaps, res, cache = _fixture(early)
    per, _n, _s = _simulate_passive(snaps, res, cache, 0.15, 0.07, 100,
                                    fill_model="trade", improve=1, cross=False)
    check("print in the pricing bar does not fill", per == {})
    later = [_bar(0), _bar(1, lo=10), _bar(2)]
    snaps, res, cache = _fixture(later)
    per, _n, _s = _simulate_passive(snaps, res, cache, 0.15, 0.07, 100,
                                    fill_model="trade", improve=1, cross=False)
    row = per["m"][0]
    check("print in a later bar does fill", row["entry_cents"] == 73)
    check("fill is marked passive", row["execution"] == "passive")
    check("settles against the official result", row["won"] is True)


def test_cross_mode_still_takes_what_it_can():
    # Ask inside our 73c limit -> the live engine's passive branch is an elif.
    bars = [_bar(0, bid=55, ask=60, ask_low=59, lo=58, hi=61), _bar(1)]
    snaps, res, cache = _fixture(bars)
    per, _n, stats = _simulate_passive(snaps, res, cache, 0.15, 0.07, 100,
                                       fill_model="trade", improve=1, cross=True)
    row = per["m"][0]
    check("crossable edge is taken, not rested", row["execution"] == "taker")
    check("taker pays the offer", row["entry_cents"] == 60)
    check("nothing was rested", stats["rested"] == 0)
    per2, _n2, _s2 = _simulate_passive(snaps, res, cache, 0.15, 0.07, 100,
                                       fill_model="trade", improve=1, cross=False)
    check("passive-only refuses to cross the same edge",
          per2 == {} or per2["m"][0]["execution"] == "passive")


def test_spread_at():
    bars = [Candle(_ts(0), 48, 55, 45)]
    check("spread is ask minus bid", _spread_at(bars, _ts(0)) == 3)
    check("an empty bid side is excluded, not counted as a huge spread",
          _spread_at([Candle(_ts(0), 48, 100, 0)], _ts(0)) is None)
    check("a missing ask is excluded", _spread_at([Candle(_ts(0), None, 55, 45)], _ts(0)) is None)


def test_matched_pairs_separates_side_flips():
    def row(t, side, net):
        return {"ticker": t, "side": side, "entry_cents": 50, "won": net > 0,
                "net_usd": net, "fee_usd": 1.0}
    taker = {"m": [row("A", "yes", -5.0), row("B", "yes", -5.0), row("C", "yes", -5.0)]}
    passive = {"m": [row("A", "yes", -6.0), row("B", "no", 40.0)]}
    m = _matched_pairs(taker, passive)
    check("matched on both", m["n"] == 2)
    check("same-side pairs isolated", m["same_side_n"] == 1)
    check("side flips isolated", m["flip_n"] == 1)
    check("same-side delta excludes the flip", abs(m["same_side_delta_per_trade"] + 1.0) < 1e-9)
    check("flip delta reported separately", abs(m["flip_delta_per_trade"] - 45.0) < 1e-9)
    check("capture rate is share of the taker book", abs(m["capture_rate"] - 2 / 3) < 1e-9)
    check("taker-only bucket is what passive never filled", m["taker_only"]["trades"] == 1)


def main():
    test_edge_limit_prices(); test_passive_fill_index(); test_passive_quote()
    test_unfilled_resting_order_is_not_a_trade()
    test_fill_is_never_credited_to_the_pricing_bar()
    test_cross_mode_still_takes_what_it_can(); test_spread_at()
    test_matched_pairs_separates_side_flips()
    print("\n" + ("ALL PASS" if not failures else f"{len(failures)} FAILED: {failures}"))
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
