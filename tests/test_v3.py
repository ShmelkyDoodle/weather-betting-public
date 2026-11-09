"""Tests for the upgrades: fees, model_prob persistence, migration, calibration."""
import asyncio, math, os, sqlite3, sys, tempfile
import os as _osp; sys.path.insert(0, _osp.path.dirname(_osp.path.dirname(_osp.path.abspath(__file__))))

from weather_arb.config import Settings, RiskConfig
from weather_arb.db import Database
from weather_arb.fees import fee_cents, fee_fraction_per_contract
from weather_arb.models import (Action, OrderBook, PriceLevel, Side, Strategy, Trade, Venue,
                                WeatherMarket, STRIKE_GREATER_EQ)
from weather_arb.report import build_summary
from weather_arb import timeutils

failures = []
def check(name, cond):
    print(("PASS" if cond else "FAIL"), "-", name)
    if not cond: failures.append(name)

def test_fees():
    # C=100 @ P=0.5 -> 0.07*100*0.25 = $1.75 -> 175 cents
    check("fee 100@50c = 175c", fee_cents(50, 100, 0.07) == 175)
    # Near extremes fees ~ 0
    check("fee 100@98c tiny", fee_cents(98, 100, 0.07) <= 14)  # 0.07*100*.98*.02=0.137->$0.14
    check("fee zero count", fee_cents(50, 0, 0.07) == 0)
    check("fee_fraction @50c = 0.0175", abs(fee_fraction_per_contract(50, 0.07) - 0.0175) < 1e-9)

async def test_model_prob_persistence():
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "m.sqlite3")
        async with Database(path) as db:
            now = timeutils.utc_now()
            await db.record_trade(Trade("c1","e1",Venue.KALSHI,"TICK",Side.YES,Action.BUY,
                                        40,10,Strategy.MODEL_EDGE,now,model_prob=0.72))
            trades = await db.trades_for_ticker("TICK")
            check("model_prob round-trips", trades and abs(trades[0].model_prob - 0.72) < 1e-9)

async def test_migration_from_old_schema():
    """Simulate an old ledger lacking model_prob; Database must add the column."""
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "old.sqlite3")
        # Build a minimal OLD trades table without model_prob.
        conn = sqlite3.connect(path)
        conn.execute("""CREATE TABLE trades (id INTEGER PRIMARY KEY AUTOINCREMENT,
            client_order_id TEXT, exchange_order_id TEXT, venue TEXT NOT NULL, ticker TEXT NOT NULL,
            side TEXT NOT NULL, action TEXT NOT NULL, price_cents INTEGER NOT NULL, count INTEGER NOT NULL,
            notional_usd REAL NOT NULL, strategy TEXT NOT NULL, executed_at_utc TEXT NOT NULL)""")
        conn.execute("""CREATE TABLE open_limit_orders (client_order_id TEXT PRIMARY KEY,
            exchange_order_id TEXT, venue TEXT NOT NULL, ticker TEXT NOT NULL, side TEXT NOT NULL,
            action TEXT NOT NULL, price_cents INTEGER NOT NULL, count INTEGER NOT NULL,
            filled_count INTEGER NOT NULL DEFAULT 0, status TEXT NOT NULL, strategy TEXT NOT NULL,
            created_at_utc TEXT NOT NULL, updated_at_utc TEXT NOT NULL)""")
        conn.commit(); conn.close()
        # Opening via Database should migrate (add model_prob) without error.
        async with Database(path) as db:
            cols = await db._column_names("trades")
            check("migration added trades.model_prob", "model_prob" in cols)
            ocols = await db._column_names("open_limit_orders")
            check("migration added orders.model_prob", "model_prob" in ocols)
            # And it should be usable.
            await db.record_trade(Trade("c","e",Venue.KALSHI,"T",Side.YES,Action.BUY,50,1,
                                        Strategy.MODEL_EDGE,timeutils.utc_now(),model_prob=0.6))
            check("post-migration insert works", len(await db.trades_for_ticker("T")) == 1)

async def test_report_net_and_calibration():
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "r.sqlite3")
        async with Database(path) as db:
            now = timeutils.utc_now()
            # Model-edge fills with predicted probs; make realized match roughly.
            # 5 trades predicted 0.8 that win, 5 predicted 0.8 that lose -> realized 50% (miscalib)
            for i in range(10):
                tk = f"M{i}"
                res = "yes" if i < 5 else "no"
                await db.record_trade(Trade(f"c{i}","e",Venue.KALSHI,tk,Side.YES,Action.BUY,
                                            50,10,Strategy.MODEL_EDGE,now,model_prob=0.8))
                await db.record_market_result(tk, res)
        summary = build_summary(path, fee_coefficient=0.07)
        t = summary["totals"]
        # Gross: 5 wins (+$5 each: buy@50 settle100 -> +50c*10=$5) = +$25; 5 losses -$25 -> gross 0
        check("gross ~ 0", abs(t["gross_usd"]) < 1e-9)
        # Fees: 10 fills * fee(50c,10)=ceil(0.07*10*0.25*100)=18c -> $0.18*10=$1.80
        check("fees = $1.80", abs(t["fees_usd"] - 1.80) < 1e-9)
        check("net = -$1.80 (fees drag)", abs(t["net_usd"] + 1.80) < 1e-9)
        cal = summary["calibration"]
        check("calibration n=10", cal["n"] == 10)
        # predicted 0.8 vs realized 0.5 -> Brier = mean((0.8-o)^2): wins (0.8-1)^2=.04, losses (0.8-0)^2=.64 -> avg .34
        check("brier ~ 0.34", abs(cal["brier"] - 0.34) < 1e-9)
        bins = {b["range"]: b for b in cal["bins"]}
        check("80-90% bin realized 50%", "80-90%" in bins and abs(bins["80-90%"]["realized"]-0.5) < 1e-9)
        check("80-90% bin predicted 80%", abs(bins["80-90%"]["mean_predicted"]-0.8) < 1e-9)

async def test_fee_aware_edge_and_skip_close():
    """Fee-adjusted Strategy-2 price is stricter than the fee-free price."""
    from weather_arb.arb_engine import ArbitrageEngine
    from weather_arb.risk_manager import RiskManager
    from weather_arb.forecast_ingest import ForecastClient
    from weather_arb.market_client import KalshiClient
    from weather_arb.market_discovery import MarketRegistry
    settings = Settings(dry_run=True)
    with tempfile.TemporaryDirectory() as d:
        async with Database(os.path.join(d, "f.sqlite3")) as db:
            eng = ArbitrageEngine(settings=settings, db=db, client=KalshiClient(settings),
                                  risk=RiskManager(settings=settings, db=db),
                                  forecast=ForecastClient(session=None, settings=settings),
                                  registry=MarketRegistry())
            mkt = WeatherMarket("T", Venue.KALSHI, "KNYC", floor_strike_f=80.0, strike_type=STRIKE_GREATER_EQ)
            # model_prob 0.90, threshold 0.15 -> fee-free buy_limit=75, fee-adjusted=73.
            # ask=74 would pass the fee-FREE test but must be REJECTED once fees apply.
            book74 = OrderBook("T", timeutils.utc_now(), yes_bids=(PriceLevel(60, 100),),
                               no_bids=(PriceLevel(26, 100),))  # yes_ask=74
            check("fee rejects marginal 74c edge", eng._model_edge_order(mkt, book74, 0.90, 0.15, "2026-07-21") is None)
            # ask=73 clears the fee-adjusted limit -> order created at 73, model_prob recorded.
            book73 = OrderBook("T", timeutils.utc_now(), yes_bids=(PriceLevel(60, 100),),
                               no_bids=(PriceLevel(27, 100),))  # yes_ask=73
            req = eng._model_edge_order(mkt, book73, 0.90, 0.15, "2026-07-21")
            check("fee-aware order created at 73", req is not None and req.price_cents == 73)
            check("model_prob recorded on order", req is not None and abs(req.model_prob-0.90) < 1e-9)


async def test_calibration_and_floor_in_edge_order():
    """Live edge builder honors the opt-in calibration map and price floor."""
    from weather_arb.arb_engine import ArbitrageEngine
    from weather_arb.risk_manager import RiskManager
    from weather_arb.forecast_ingest import ForecastClient
    from weather_arb.market_client import KalshiClient
    from weather_arb.market_discovery import MarketRegistry
    from weather_arb.calibration import CalibrationMap
    settings = Settings(dry_run=True)
    with tempfile.TemporaryDirectory() as d:
        async with Database(os.path.join(d, "c.sqlite3")) as db:
            eng = ArbitrageEngine(settings=settings, db=db, client=KalshiClient(settings),
                                  risk=RiskManager(settings=settings, db=db),
                                  forecast=ForecastClient(session=None, settings=settings),
                                  registry=MarketRegistry())
            mkt = WeatherMarket("T", Venue.KALSHI, "KNYC", floor_strike_f=80.0, strike_type=STRIKE_GREATER_EQ)
            # A cheap YES market: yes_ask = 10c (no_bid 90). Raw prob 0.30 clears the
            # fee-adjusted buy limit -> order without calibration.
            cheap = OrderBook("T", timeutils.utc_now(), yes_bids=(PriceLevel(8, 100),),
                              no_bids=(PriceLevel(90, 100),))  # yes_ask=10
            base = eng._model_edge_order(mkt, cheap, 0.30, 0.15, "2026-07-21")
            check("raw 0.30 buys the cheap YES", base is not None and base.action.value == "buy")
            # Calibration that learned raw-0.30 really wins ~5% suppresses that trade.
            cm = CalibrationMap(breakpoints=((0.0, 0.0), (0.30, 0.05), (1.0, 1.0)), n_samples=100)
            calibrated = eng._model_edge_order(mkt, cheap, 0.30, 0.15, "2026-07-21", calibration=cm)
            check("calibrated-down prob suppresses the longshot buy", calibrated is None)
            # Price floor alone also blocks the sub-floor entry, no calibration needed.
            floored = eng._model_edge_order(mkt, cheap, 0.30, 0.15, "2026-07-21", min_price_cents=20)
            check("price floor blocks the <20c entry", floored is None)
            # Above the floor with genuine edge still trades.
            ok = eng._model_edge_order(mkt, cheap, 0.90, 0.15, "2026-07-21", min_price_cents=5)
            check("above-floor genuine edge still trades", ok is not None)


async def main():
    test_fees()
    await test_model_prob_persistence()
    await test_migration_from_old_schema()
    await test_report_net_and_calibration()
    await test_fee_aware_edge_and_skip_close()
    await test_calibration_and_floor_in_edge_order()
    print("\n" + ("ALL PASS" if not failures else f"FAILURES: {failures}"))
    sys.exit(1 if failures else 0)

asyncio.run(main())
