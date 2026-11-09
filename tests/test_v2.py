"""Tests for the paper-trading additions: buckets, discovery, settlement, report."""
import asyncio, os, sys, tempfile
import os as _osp; sys.path.insert(0, _osp.path.dirname(_osp.path.dirname(_osp.path.abspath(__file__))))

from weather_arb.config import Settings, RiskConfig
from weather_arb.db import Database
from weather_arb.models import (
    Action, LimitOrder, OrderBook, OrderStatus, PriceLevel, Side, Strategy, Venue,
    WeatherMarket, STRIKE_BETWEEN, STRIKE_GREATER_EQ, STRIKE_LESS_EQ,
)
from weather_arb.forecast_ingest import _truncated_normal_between
from weather_arb.market_discovery import _parse_market, MarketRegistry
from weather_arb.pnl import trade_pnl_cents, trade_won
from weather_arb.report import build_summary
from weather_arb.arb_engine import ArbitrageEngine
from weather_arb.risk_manager import RiskManager
from weather_arb.forecast_ingest import ForecastClient
from weather_arb import timeutils

failures = []
def check(name, cond):
    print(("PASS" if cond else "FAIL"), "-", name)
    if not cond: failures.append(name)

import math

def test_bucket_deterministic():
    tail_hi = WeatherMarket("A", Venue.KALSHI, "KNYC", floor_strike_f=90.0, strike_type=STRIKE_GREATER_EQ)
    check("tail-high floor/cap", tail_hi.floor_f == 90.0 and tail_hi.cap_f is None)
    check("tail-high not certain at 88", tail_hi.deterministic_lock_in(88.0) is None)
    check("tail-high certain YES at 91", tail_hi.deterministic_lock_in(91.0) == (Side.YES, Action.BUY))

    interior = WeatherMarket("B", Venue.KALSHI, "KNYC", floor_strike_f=84.0, cap_strike_f=85.0, strike_type=STRIKE_BETWEEN)
    check("interior never certain-YES intraday at 84.5", interior.deterministic_lock_in(84.5) is None)
    check("interior certain-NO once above cap (86>85)", interior.deterministic_lock_in(86.0) == (Side.NO, Action.BUY))
    check("interior settles_yes(84.5)=True", interior.settles_yes(84.5) is True)
    check("interior settles_yes(86)=False", interior.settles_yes(86.0) is False)

    tail_lo = WeatherMarket("C", Venue.KALSHI, "KNYC", cap_strike_f=70.0, strike_type=STRIKE_LESS_EQ)
    check("tail-low certain-NO once above cap (72>70)", tail_lo.deterministic_lock_in(72.0) == (Side.NO, Action.BUY))
    check("tail-low not certain at 68", tail_lo.deterministic_lock_in(68.0) is None)

def test_bucket_probability():
    # Interior [84,85], mu=84.5, small sigma -> high prob; observed below.
    p_in = _truncated_normal_between(84.5, 1.0, 84.0, 85.0, None)
    p_wide = _truncated_normal_between(84.5, 10.0, 84.0, 85.0, None)
    check("tight sigma bucket prob > wide", p_in > p_wide)
    check("bucket prob in [0,1]", 0 <= p_in <= 1)
    # Observed already above cap -> 0
    check("observed above cap -> 0", _truncated_normal_between(90, 3, 84, 85, 88.0) == 0.0)
    # Tail-high with +inf: P(X>=90) at mu=90 ~ 0.5
    p_tail = _truncated_normal_between(90, 3, 90.0, math.inf, None)
    check("tail-high P~0.5", abs(p_tail - 0.5) < 1e-6)

def test_no_side_orderbook():
    # yes_bids: 95@50 -> NO ask = 5@50 (cheap NO). buyable_no up to 98 = 50
    ob = OrderBook("T", timeutils.utc_now(), yes_bids=(PriceLevel(95, 50),), no_bids=(PriceLevel(30, 10),))
    check("best_no_ask=5", ob.best_no_ask.price_cents == 5 and ob.best_no_ask.size == 50)
    check("buyable_no up to 98 = 50", ob.buyable_no_size(98) == 50)

def test_discovery_parse():
    raw = {"ticker":"KXHIGHNY-25JUL21-B90.5","status":"active","floor_strike":90.5,
           "strike_type":"greater_or_equal","title":"NY high >= 90.5","close_time":"2026-07-21T23:00:00Z"}
    m = _parse_market(raw, "KNYC")
    check("discovery parses tail-high", m is not None and m.floor_f == 90.5 and m.cap_f is None)
    raw2 = {"ticker":"KXHIGHNY-25JUL21-T84","status":"active","floor_strike":84,"cap_strike":85,
            "strike_type":"between","title":"84-85"}
    m2 = _parse_market(raw2, "KNYC")
    check("discovery parses interior", m2 is not None and m2.floor_f == 84 and m2.cap_f == 85)
    raw3 = {"ticker":"X","status":"closed","floor_strike":90}
    check("discovery skips closed", _parse_market(raw3, "KNYC") is None)
    raw4 = {"ticker":"Y","status":"active"}  # no strikes
    check("discovery skips no-strike", _parse_market(raw4, "KNYC") is None)

def test_pnl_math():
    # Buy YES @ 40, settles YES(100): +60/contract
    check("buy yes win", trade_pnl_cents(side="yes",action="buy",price_cents=40,count=10,result="yes")==600)
    # Buy YES @ 40, settles NO: -40
    check("buy yes lose", trade_pnl_cents(side="yes",action="buy",price_cents=40,count=10,result="no")==-400)
    # Buy NO @ 98 (lock-in certain NO), settles NO: +2/contract
    check("buy no win", trade_pnl_cents(side="no",action="buy",price_cents=98,count=100,result="no")==200)
    # Sell YES @ 30, settles NO: +30
    check("sell yes win", trade_pnl_cents(side="yes",action="sell",price_cents=30,count=10,result="no")==300)
    check("trade_won buy-yes-yes", trade_won(side="yes",action="buy",result="yes") is True)
    check("trade_won sell-yes-yes", trade_won(side="yes",action="sell",result="yes") is False)

async def test_settlement_and_report():
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "s.sqlite3")
        async with Database(path) as db:
            from weather_arb.models import Trade
            now = timeutils.utc_now()
            # Lock-in NO buy @98 x100 on interior bucket that will settle NO -> +$2
            await db.record_trade(Trade("c1","e1",Venue.KALSHI,"TICK1",Side.NO,Action.BUY,98,100,Strategy.LOCK_IN,now))
            # Model-edge YES buy @40 x10 on TICK2 that settles YES -> +$6
            await db.record_trade(Trade("c2","e2",Venue.KALSHI,"TICK2",Side.YES,Action.BUY,40,10,Strategy.MODEL_EDGE,now))
            # Unsettled trade
            await db.record_trade(Trade("c3","e3",Venue.KALSHI,"TICK3",Side.YES,Action.BUY,50,10,Strategy.MODEL_EDGE,now))

            check("2 tickers unsettled before results", set(await db.traded_tickers_without_result())=={"TICK1","TICK2","TICK3"})
            await db.record_market_result("TICK1","no")
            await db.record_market_result("TICK2","yes")
            check("1 ticker unsettled after 2 results", await db.traded_tickers_without_result()==["TICK3"])

        summary = build_summary(path)
        # TICK1: +$2.00, TICK2: +$6.00 => gross $8.00; TICK3 unsettled
        check("gross pnl = $8.00", abs(summary["totals"]["gross_usd"] - 8.0) < 1e-9)
        check("net < gross (fees)", summary["totals"]["net_usd"] < summary["totals"]["gross_usd"])
        check("settled fills = 2", summary["totals"]["settled_fills"] == 2)
        check("unsettled fills = 1", summary["totals"]["unsettled_fills"] == 1)
        check("hit rate = 100%", abs(summary["totals"]["hit_rate"] - 1.0) < 1e-9)
        check("lock_in gross $2", abs(summary["by_strategy"]["lock_in"]["gross_usd"] - 2.0) < 1e-9)

async def test_engine_certain_no_lockin():
    """Interior bucket, temp climbs above cap -> engine buys NO to lock in."""
    market = WeatherMarket("KXHIGHNY-D-T84", Venue.KALSHI, "KNYC",
                           floor_strike_f=84.0, cap_strike_f=85.0, strike_type=STRIKE_BETWEEN)
    settings = Settings(dry_run=True, risk=RiskConfig(account_equity_usd=1000.0))
    with tempfile.TemporaryDirectory() as d:
        async with Database(os.path.join(d, "e.sqlite3")) as db:
            from weather_arb.market_client import KalshiClient, MarketInfo
            client = KalshiClient(settings)
            # yes_bids 90@200 -> NO ask = 10 (<98). Certain NO -> buy NO.
            book = OrderBook(market.ticker, timeutils.utc_now(),
                             yes_bids=(PriceLevel(90, 200),), no_bids=(PriceLevel(8, 200),))
            async def fake_ob(t, depth=32): return book
            async def fake_mkt(t): return MarketInfo(t, "active", "", None)
            client.get_orderbook = fake_ob  # type: ignore
            client.get_market = fake_mkt    # type: ignore
            registry = MarketRegistry((market,))
            rm = RiskManager(settings=settings, db=db)
            fc = ForecastClient(session=None, settings=settings)
            engine = ArbitrageEngine(settings=settings, db=db, client=client, risk=rm, forecast=fc, registry=registry)
            cdate = timeutils.climate_date_str(timeutils.utc_now(), -5)
            await db.update_tmax(icao="KNYC", climate_date=cdate, temp_f=86.0, observed_at_utc=timeutils.utc_now())
            await engine.on_new_high("KNYC", cdate, 86.0)  # 86 > cap 85 -> certain NO
            order = await db.get_order(f"s1-{market.ticker}-{cdate}")
            check("certain-NO lock-in placed", order is not None)
            check("lock-in is BUY NO @98", order is not None and order.side==Side.NO and order.action==Action.BUY and order.price_cents==98)

async def main():
    test_bucket_deterministic()
    test_bucket_probability()
    test_no_side_orderbook()
    test_discovery_parse()
    test_pnl_math()
    await test_settlement_and_report()
    await test_engine_certain_no_lockin()
    print("\n" + ("ALL PASS" if not failures else f"FAILURES: {failures}"))
    sys.exit(1 if failures else 0)

asyncio.run(main())
