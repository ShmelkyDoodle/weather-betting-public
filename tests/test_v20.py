"""#4 prediction-history: gated recording + lead-time backtest entry (offline)."""
import asyncio, sys, tempfile
from datetime import datetime, timezone
import os as _osp; sys.path.insert(0, _osp.path.dirname(_osp.path.dirname(_osp.path.abspath(__file__))))
from weather_arb.db import Database
from weather_arb.backtest import Candle, _price_at, first_qualifying_edge

failures = []
def check(n, c):
    print(("PASS" if c else "FAIL"), "-", n)
    if not c: failures.append(n)


def _iso(h):
    return datetime(2026, 7, 20, h, tzinfo=timezone.utc).isoformat()


def _ts(h):
    return int(datetime(2026, 7, 20, h, tzinfo=timezone.utc).timestamp())


def test_price_at():
    candles = [Candle(_ts(10), 40, 62), Candle(_ts(12), 55, 47), Candle(_ts(14), 95, 6)]
    check("first candle at/after ts", _price_at(candles, _ts(11)) == (55, 47))
    check("exact-ts candle", _price_at(candles, _ts(12)) == (55, 47))
    check("before first -> first", _price_at(candles, _ts(9)) == (40, 62))
    check("beyond last -> last", _price_at(candles, _ts(20)) == (95, 6))
    check("empty -> None", _price_at([], _ts(11)) == (None, None))


def test_first_qualifying_edge_enters_early():
    # prob 0.8; YES-ask 40c at 10:00 -> edge 0.4 clears immediately -> enter early.
    candles = [Candle(_ts(10), 40, 62), Candle(_ts(12), 55, 47), Candle(_ts(14), 95, 6)]
    snaps = [{"created_at": _iso(10), "prob": 0.8},
             {"created_at": _iso(12), "prob": 0.8},
             {"created_at": _iso(14), "prob": 0.8}]
    t = first_qualifying_edge(snaps, candles, threshold=0.15, coeff=0.07)
    check("enters YES at the earliest qualifying snapshot", t == ("yes", 40, _iso(10), 0.8))


def test_first_qualifying_edge_skips_then_enters():
    # No edge at 10:00 (YES 90 / NO 15), edge appears at 12:00 (YES 55).
    candles = [Candle(_ts(10), 90, 15), Candle(_ts(12), 55, 47)]
    snaps = [{"created_at": _iso(10), "prob": 0.8}, {"created_at": _iso(12), "prob": 0.8}]
    t = first_qualifying_edge(snaps, candles, threshold=0.15, coeff=0.07)
    check("skips no-edge snapshot, enters when edge appears", t == ("yes", 55, _iso(12), 0.8))


def test_first_qualifying_edge_none():
    # prob 0.55 near a 50/52 market -> neither side clears the threshold.
    candles = [Candle(_ts(10), 50, 52), Candle(_ts(12), 50, 52)]
    snaps = [{"created_at": _iso(10), "prob": 0.55}, {"created_at": _iso(12), "prob": 0.55}]
    check("no qualifying edge -> None",
          first_qualifying_edge(snaps, candles, threshold=0.15, coeff=0.07) is None)


async def _count_history(db, ticker):
    async with db._c.execute(
        "SELECT COUNT(*) AS n FROM model_prediction_history WHERE ticker=?", (ticker,)
    ) as cur:
        return (await cur.fetchone())["n"]


async def test_history_gating():
    with tempfile.TemporaryDirectory() as d:
        db = Database(f"{d}/t.sqlite3")
        await db.connect()
        try:
            # interval 0 -> every call appends a history row.
            for _ in range(3):
                await db.record_model_prediction(
                    ticker="KX-A", model="current", climate_date="2026-07-20",
                    prob=0.6, forecast_high_f=90.0, sigma_f=3.0, history_min_interval_s=0)
            check("interval 0 -> 3 history rows", await _count_history(db, "KX-A") == 3)
            # huge interval -> only the first of several close calls is kept.
            for _ in range(3):
                await db.record_model_prediction(
                    ticker="KX-B", model="current", climate_date="2026-07-20",
                    prob=0.6, forecast_high_f=90.0, sigma_f=3.0, history_min_interval_s=1e9)
            check("large interval -> 1 history row (throttled)", await _count_history(db, "KX-B") == 1)
            # latest-view table always has exactly one row per (ticker, model).
            async with db._c.execute("SELECT COUNT(*) AS n FROM model_predictions WHERE ticker='KX-B'") as c:
                check("model_predictions upsert keeps 1 latest row", (await c.fetchone())["n"] == 1)
        finally:
            await db.close()


async def test_model_pnl_selection():
    with tempfile.TemporaryDirectory() as d:
        db = Database(f"{d}/t.sqlite3")
        await db.connect()
        try:
            await db.record_model_pnl({
                "climo": {"trades": 40, "net_usd": 74.6, "hit_rate": 0.52},
                "current": {"trades": 49, "net_usd": -87.7, "hit_rate": 0.17},
                "nbm": {"trades": 5, "net_usd": 200.0, "hit_rate": 0.9},   # < min_trades
            })
            best = await db.best_model_by_pnl(min_trades=20)
            check("picks highest net-P&L above min_trades (climo)", best[0] == "climo")
            check("returns (model, trades, net)", best[1] == 40 and abs(best[2] - 74.6) < 1e-9)
            check("high-net but few-trades model excluded (nbm)", best[0] != "nbm")
            # Upsert replaces the cached value.
            await db.record_model_pnl({"climo": {"trades": 50, "net_usd": -10.0, "hit_rate": 0.4}})
            b2 = await db.best_model_by_pnl(min_trades=20)
            check("upsert updates net (climo -10 still beats current -87.7)",
                  b2[0] == "climo" and abs(b2[2] + 10.0) < 1e-9)
            check("none qualify when min_trades too high", await db.best_model_by_pnl(min_trades=100) is None)
        finally:
            await db.close()


async def main():
    test_price_at(); test_first_qualifying_edge_enters_early()
    test_first_qualifying_edge_skips_then_enters(); test_first_qualifying_edge_none()
    await test_history_gating(); await test_model_pnl_selection()
    print("\n" + ("ALL PASS" if not failures else f"FAILURES: {failures}"))
    sys.exit(1 if failures else 0)

asyncio.run(main())
