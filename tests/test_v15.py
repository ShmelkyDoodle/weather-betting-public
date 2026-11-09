"""Strategy-2 (model edge) backtest: fee-adjusted edge trade + summary (offline)."""
import asyncio, sys
import os as _osp; sys.path.insert(0, _osp.path.dirname(_osp.path.dirname(_osp.path.abspath(__file__))))
from weather_arb.backtest import _edge_trade, _summarize_s2

failures = []
def check(n, c):
    print(("PASS" if c else "FAIL"), "-", n)
    if not c: failures.append(n)

THR, COEFF = 0.15, 0.07


def test_edge_trade():
    # Strong YES edge (prob 0.9 vs 60c ask) clears threshold+fee -> buy YES.
    # Returns (side, entry_cents, side_prob); side_prob is the raw prob (no calibration given).
    check("buys YES on strong yes edge", _edge_trade(0.90, 60, 42, THR, COEFF) == ("yes", 60, 0.90))
    # Strong NO edge (prob 0.2 -> NO prob 0.8 vs 42c) -> buy NO.
    check("buys NO on strong no edge", _edge_trade(0.20, 60, 42, THR, COEFF) == ("no", 42, 0.80))
    # Thin edge below the fee-adjusted threshold -> no trade.
    check("no trade on thin edge", _edge_trade(0.55, 52, 50, THR, COEFF) is None)
    # Missing prices -> no trade.
    check("no trade without prices", _edge_trade(0.90, None, None, THR, COEFF) is None)
    # Picks the larger of two qualifying edges.
    t = _edge_trade(0.99, 5, 1, THR, COEFF)   # yes edge .94 vs no edge -.98 -> yes
    check("picks the larger edge (YES @5c)", t == ("yes", 5, 0.99))
    # A price floor refuses to trade below it even with a clearing edge.
    check("min_price_cents floor blocks a cheap edge",
          _edge_trade(0.99, 5, 1, THR, COEFF, min_price_cents=10) is None)
    check("min_price_cents floor allows a price at/above it",
          _edge_trade(0.99, 15, 1, THR, COEFF, min_price_cents=10) == ("yes", 15, 0.99))


def test_summarize_ranking():
    per_model = {
        "nbm": [{"won": True, "net_usd": 40.0, "fee_usd": 1.0},
                {"won": False, "net_usd": -60.0, "fee_usd": 1.0}],
        "current": [{"won": True, "net_usd": 55.0, "fee_usd": 1.0},
                    {"won": True, "net_usd": 30.0, "fee_usd": 1.0}],
    }
    s = _summarize_s2(per_model, size=100, n_preds=10, min_trades=2)
    check("best model = current (higher net)", s["best_model"] == "current")
    check("current net = 85", abs(s["models"]["current"]["net_usd"] - 85.0) < 1e-9)
    check("current hit rate = 1.0", s["models"]["current"]["hit_rate"] == 1.0)
    check("nbm hit rate = 0.5", s["models"]["nbm"]["hit_rate"] == 0.5)
    check("models ranked by net (current first)", list(s["models"]) == ["current", "nbm"])


def test_best_model_needs_a_sample_floor():
    """A model that traded twice must not out-rank one that traded 100 times.

    `_id` variants only exist once the climate day is under way, so they always
    carry far fewer trades than their base model -- without a floor the headline
    crowns whichever model happened to trade least."""
    per_model = {
        "current": [{"won": False, "net_usd": -5.0, "fee_usd": 1.0}] * 100,
        "current_id": [{"won": True, "net_usd": 1.0, "fee_usd": 0.1}] * 2,
    }
    s = _summarize_s2(per_model, size=100, n_preds=10, min_trades=20)
    check("thin 2-trade model does not win the headline", s["best_model"] == "current")
    check("thin model still ranked/reported in the table", "current_id" in s["models"])
    check("floor surfaced for the renderer", s["best_model_min_trades"] == 20)
    # Nothing clears the floor -> no winner is claimed at all.
    thin_only = _summarize_s2({"current_id": per_model["current_id"]},
                              size=100, n_preds=10, min_trades=20)
    check("no model clears the floor -> best_model is None", thin_only["best_model"] is None)


async def main():
    test_edge_trade(); test_summarize_ranking(); test_best_model_needs_a_sample_floor()
    print("\n" + ("ALL PASS" if not failures else f"FAILURES: {failures}"))
    sys.exit(1 if failures else 0)

asyncio.run(main())
