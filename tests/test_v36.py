"""#A Order-book encoding: Kalshi migrated to `orderbook_fp`, and we read `{}` (offline).

THE DEFECT. The REST endpoint used to reply

    {"orderbook": {"yes": [[price_cents, size], ...], "no": [...]}}

and now replies

    {"orderbook_fp": {"yes_dollars": [["0.4000", "947.00"], ...],
                      "no_dollars":  [...]}}

serving NOTHING under the old key -- verified live 2026-08-26, and no request
parameter brings it back. `_orderbook_from_kalshi` read `payload["orderbook"]`,
got `{}`, and produced a book with zero levels.

WHY IT WENT UNNOTICED FOR WEEKS. It never raised. An empty book means
`best_yes_ask`/`best_no_ask` are None, so Strategy 1 always took the priced-out
branch and Strategy 2 never found a qualifying price. Both logged zero signals
for the entire paper run, and that was read as "the market is always fully
priced" -- when in fact we could not see the market at all.

This is the SECOND time this migration has bitten the project: the candlestick
fields moved to `volume_fp`/`open_interest_fp` and were read as
`volume`/`open_interest`, reporting zero volume everywhere. Both encodings are
accepted now so a rollback either way is a non-event.
"""
import os, sys
import os as _osp; sys.path.insert(0, _osp.path.dirname(_osp.path.dirname(_osp.path.abspath(__file__))))

from weather_arb.market_client import _level_from_pair, _orderbook_from_kalshi
from weather_arb.models import Side

failures = []
def check(n, c):
    print(("PASS" if c else "FAIL"), "-", n)
    if not c: failures.append(n)


# Trimmed from a real response for KXHIGHAUS-26AUG26-B101.5 (2026-08-26).
LIVE_FP = {"orderbook_fp": {
    "yes_dollars": [["0.0100", "1876.00"], ["0.0200", "424.00"], ["0.0800", "9.00"]],
    "no_dollars": [["0.4000", "947.00"], ["0.4400", "55.00"], ["0.9200", "9.00"],
                   ["0.6800", "20.29"]],
}}
LEGACY = {"orderbook": {"yes": [[58, 10], [20, 300]], "no": [[30, 300]]}}


def test_the_regression():
    b = _orderbook_from_kalshi("T", LIVE_FP)
    check("the new encoding is parsed at all (this returned an empty book)",
          len(b.yes_bids) == 3 and len(b.no_bids) == 4)
    check("dollar strings become integer cents",
          b.best_yes_bid.price_cents == 8 and b.best_no_bid.price_cents == 92)
    check("and therefore an ask exists, which is what the strategies gate on",
          b.best_yes_ask is not None and b.best_no_ask is not None)
    check("YES ask derives from the best NO bid (100 - 92 = 8)",
          b.best_yes_ask.price_cents == 8)
    check("NO ask derives from the best YES bid (100 - 8 = 92)",
          b.best_no_ask.price_cents == 92)


def test_fractional_sizes_are_floored():
    """Sizes are decimal strings and are sometimes fractional ("20.29")."""
    lvl = _level_from_pair(["0.6800", "20.29"], dollars=True)
    check("a fractional size floors to whole contracts", lvl.size == 20)
    check("a level that floors to zero is dropped, not recorded as free size",
          _level_from_pair(["0.6800", "0.42"], dollars=True) is None)
    b = _orderbook_from_kalshi("T", LIVE_FP)
    check("the floored size reaches the ladder",
          any(l.price_cents == 68 and l.size == 20 for l in b.no_bids))


def test_legacy_encoding_still_works():
    b = _orderbook_from_kalshi("T", LEGACY)
    check("integer-cent payloads still parse",
          b.best_yes_bid.price_cents == 58 and b.best_no_bid.price_cents == 30)
    check("legacy sizes are preserved", b.best_yes_bid.size == 10)


def test_levels_are_sorted_best_first():
    b = _orderbook_from_kalshi("T", LIVE_FP)
    check("yes bids descend (best bid first)",
          [l.price_cents for l in b.yes_bids] == [8, 2, 1])
    check("no bids descend (best bid first)",
          [l.price_cents for l in b.no_bids] == [92, 68, 44, 40])
    check("derived asks ascend (cheapest first)",
          [l.price_cents for l in b.no_asks] == [92, 98, 99])


def test_garbage_is_dropped_not_crashed():
    payload = {"orderbook_fp": {"no_dollars": [
        ["0.5000", "10"], ["oops", "10"], ["0.5000"], None,
        ["1.5000", "10"], ["-0.1", "10"], ["0.5000", "-3"],
    ]}}
    b = _orderbook_from_kalshi("T", payload)
    check("only the well-formed level survives", len(b.no_bids) == 1)
    check("out-of-range prices are rejected",
          all(0 <= l.price_cents <= 100 for l in b.no_bids))


def test_missing_and_empty_payloads():
    for payload in ({}, {"orderbook_fp": {}}, {"orderbook": {}},
                    {"orderbook_fp": {"yes_dollars": [], "no_dollars": []}}):
        b = _orderbook_from_kalshi("T", payload)
        check(f"an empty payload yields an empty book, not a crash ({list(payload)})",
              b.yes_bids == () and b.no_bids == ())


def test_depth_is_now_measurable():
    """The whole experiment depends on this: depth must be non-zero."""
    b = _orderbook_from_kalshi("T", LIVE_FP)
    # NO asks derive from YES BIDS (no_ask = 100 - yes_bid): yes bids 8/2/1
    # become NO offers at 92/98/99 with sizes 9/424/1876. Only 92 and 98 sit at
    # or below the ceiling -- the 99c level is exactly the "already fully priced"
    # case Strategy 1 refuses.
    check("sweepable NO size under the ceiling is real",
          b.buyable_no_size(98) == 9 + 424)
    check("and the 99c level is correctly excluded by the ceiling",
          b.buyable_no_size(100) == 9 + 424 + 1876)
    m = b.get_l2_sweep_metrics(Side.NO, 100, max_price_cents=98)
    check("a 100-contract sweep prices above the touch",
          m["vwap_cents"] > m["top_ask_cents"])
    check("and reports what it could actually fill", m["fillable_size"] == 100)


def main():
    test_the_regression()
    test_fractional_sizes_are_floored()
    test_legacy_encoding_still_works()
    test_levels_are_sorted_best_first()
    test_garbage_is_dropped_not_crashed()
    test_missing_and_empty_payloads()
    test_depth_is_now_measurable()
    print("\n" + ("ALL PASS" if not failures else f"{len(failures)} FAILED: {failures}"))
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
