"""Offline smoke test: exercises core logic without any network I/O."""
import asyncio
import os
import sys
import tempfile

import os as _osp; sys.path.insert(0, _osp.path.dirname(_osp.path.dirname(_osp.path.abspath(__file__))))

from weather_arb.config import Settings, RiskConfig, KalshiConfig
from weather_arb.db import Database
from weather_arb.models import (
    Action, LimitOrder, LimitOrderRequest, OrderBook, OrderStatus, PriceLevel,
    Side, Strategy, Venue, WeatherMarket, MetarObservation,
)
from weather_arb.risk_manager import RiskManager
from weather_arb.forecast_ingest import _truncated_normal_exceed, _iso_duration_hours, _climate_day_bounds_utc
from weather_arb.market_client import KalshiClient, _orderbook_from_kalshi, TokenBucketRateLimiter
from weather_arb.arb_engine import ArbitrageEngine
from weather_arb import timeutils

failures = []
def check(name, cond):
    print(("PASS" if cond else "FAIL"), "-", name)
    if not cond:
        failures.append(name)


def test_orderbook_math():
    # NO bids 40@30, 38@10  => YES asks 60@30, 62@10 ; YES bids 55@20
    ob = OrderBook(
        ticker="T", timestamp=timeutils.utc_now(),
        yes_bids=(PriceLevel(55, 20), PriceLevel(50, 5)),
        no_bids=(PriceLevel(40, 30), PriceLevel(38, 10)),
    )
    check("best_yes_ask=60", ob.best_yes_ask.price_cents == 60 and ob.best_yes_ask.size == 30)
    check("best_yes_bid=55", ob.best_yes_bid.price_cents == 55)
    check("buyable up to 62 = 40", ob.buyable_yes_size(62) == 40)
    check("buyable up to 60 = 30", ob.buyable_yes_size(60) == 30)
    check("sellable at >=50 = 25", ob.sellable_yes_size(50) == 25)
    check("sellable at >=55 = 20", ob.sellable_yes_size(55) == 20)


def test_kalshi_orderbook_parse():
    # Takes the WHOLE payload now, not the unwrapped inner dict: Kalshi serves
    # two encodings (`orderbook` in cents, `orderbook_fp` in dollar strings) and
    # the parser has to choose between them. Reading the old key against the new
    # payload silently produced an empty book -- see tests/test_v36.py.
    ob = _orderbook_from_kalshi(
        "T", {"orderbook": {"yes": [[55, 20], [50, 5]], "no": [[40, 30]]}})
    check("parsed best_yes_ask=60", ob.best_yes_ask.price_cents == 60)
    check("parsed best_yes_bid sorted", ob.best_yes_bid.price_cents == 55)
    fp = _orderbook_from_kalshi(
        "T", {"orderbook_fp": {"yes_dollars": [["0.5500", "20.00"]],
                               "no_dollars": [["0.4000", "30.00"]]}})
    check("parsed the dollar encoding too", fp.best_yes_bid.price_cents == 55)


def test_truncated_normal():
    # strike below observed -> certain
    check("strike<=obs -> 1.0", _truncated_normal_exceed(88, 3, 85, 90) == 1.0)
    # symmetric: P(X>=mu)=0.5 with no truncation
    p = _truncated_normal_exceed(90, 3, 90, -1e9)
    check("P(X>=mu)~0.5", abs(p - 0.5) < 1e-6)
    # truncation raises conditional probability
    p_plain = _truncated_normal_exceed(90, 3, 93, -1e9)
    p_trunc = _truncated_normal_exceed(90, 3, 93, 88)
    check("truncation raises prob", p_trunc > p_plain)
    check("prob in [0,1]", 0.0 <= p_trunc <= 1.0)
    # zero sigma degenerates to indicator
    check("sigma=0 exceed", _truncated_normal_exceed(95, 0, 90, -1e9) == 1.0)
    check("sigma=0 miss", _truncated_normal_exceed(85, 0, 90, -1e9) == 0.0)


def test_iso_duration():
    check("PT1H=1", _iso_duration_hours("PT1H") == 1)
    check("PT6H=6", _iso_duration_hours("PT6H") == 6)
    check("P1DT6H=30", _iso_duration_hours("P1DT6H") == 30)
    check("PT30M=0", _iso_duration_hours("PT30M") == 0)


def test_climate_day_bounds():
    # CST offset -6: local midnight 2025-07-21 == 06:00Z
    start, end = _climate_day_bounds_utc("2025-07-21", -6)
    check("CST day start 06Z", start.hour == 6 and start.day == 21)
    check("CST day end +24h", (end - start).total_seconds() == 86400)


def test_climate_date_lst_no_dst():
    # 2025-07-21 04:30Z at CST(-6) is still 2025-07-20 22:30 LST -> climate day 20th
    dt = timeutils.parse_isoformat_utc("2025-07-21T04:30:00+00:00")
    check("LST rollover uses standard offset", timeutils.climate_date_str(dt, -6) == "2025-07-20")


def test_metar_temp_conversion():
    obs = MetarObservation("KORD", timeutils.utc_now(), 30.0, 20.0, "raw")
    check("30C -> 86F", abs(obs.temp_f - 86.0) < 1e-9)
    check("20C -> 68F", abs(obs.dewpoint_f - 68.0) < 1e-9)


async def test_rate_limiter():
    rl = TokenBucketRateLimiter(rate=1000, burst=2)
    import time
    t0 = time.monotonic()
    for _ in range(4):
        await rl.acquire()
    dt = time.monotonic() - t0
    check("rate limiter throttles", dt >= 0.0015)  # 2 burst free, 2 more at 1000/s


async def test_db_and_reconcile():
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "t.sqlite3")
        async with Database(path) as db:
            # Atomic Tmax: lower reading must not lower the max
            m1 = await db.update_tmax(icao="KORD", climate_date="2025-07-21", temp_f=88.0, observed_at_utc=timeutils.utc_now())
            m2 = await db.update_tmax(icao="KORD", climate_date="2025-07-21", temp_f=91.0, observed_at_utc=timeutils.utc_now())
            m3 = await db.update_tmax(icao="KORD", climate_date="2025-07-21", temp_f=85.0, observed_at_utc=timeutils.utc_now())
            check("tmax rises to 91", m2 == 91.0)
            check("tmax not lowered by 85", m3 == 91.0)
            check("get_tmax=91", await db.get_tmax("KORD", "2025-07-21") == 91.0)

            # Order lifecycle + exposure
            now = timeutils.utc_now()
            order = LimitOrder("s1-X-d", "TICK", Venue.KALSHI, Side.YES, Action.BUY,
                               price_cents=90, count=10, strategy=Strategy.LOCK_IN,
                               status=OrderStatus.RESTING, created_at_utc=now, updated_at_utc=now,
                               exchange_order_id="ex1", filled_count=0)
            await db.upsert_open_order(order)
            opens = await db.get_open_orders(ticker="TICK")
            check("one open order", len(opens) == 1 and opens[0].exchange_order_id == "ex1")
            # exposure = working order remaining: buy 10 @ 0.90 = $9.00
            exp = await db.ticker_exposure_usd("TICK")
            check("exposure=9.0", abs(exp - 9.0) < 1e-9)
            await db.set_order_status("s1-X-d", OrderStatus.FILLED, filled_count=10)
            opens2 = await db.get_open_orders(ticker="TICK")
            check("no open after fill", len(opens2) == 0)


async def test_risk_sizing():
    settings = Settings(risk=RiskConfig(account_equity_usd=1000.0,
                                        max_bankroll_fraction_per_market=0.05,
                                        max_contracts_per_order=10_000))
    with tempfile.TemporaryDirectory() as d:
        async with Database(os.path.join(d, "r.sqlite3")) as db:
            rm = RiskManager(settings=settings, db=db)
            # cap = 5% * 1000 = $50 ; buy @ 50c -> $0.50/contract -> 100 contracts
            dec = await rm.size_order(ticker="TICK", action=Action.BUY, price_cents=50,
                                      strategy=Strategy.LOCK_IN, desired_count=10_000)
            check("risk sizes to 100", dec.approved and dec.count == 100)
            # invalid price rejected
            dec2 = await rm.size_order(ticker="TICK", action=Action.BUY, price_cents=0,
                                       strategy=Strategy.LOCK_IN, desired_count=10)
            check("invalid price rejected", not dec2.approved)


async def test_engine_dry_run_lock_in():
    """Full Strategy-1 path in dry-run with a fake order book (no network)."""
    market = WeatherMarket(ticker="KXHIGHCHI-D-B90", venue=Venue.KALSHI, icao="KORD",
                           strike_temp_f=90.0, comparison=">=")
    settings = Settings(dry_run=True, markets=(market,),
                        risk=RiskConfig(account_equity_usd=1000.0))
    with tempfile.TemporaryDirectory() as d:
        async with Database(os.path.join(d, "e.sqlite3")) as db:
            client = KalshiClient(settings)

            # Stub market data so no HTTP happens.
            fake_book = OrderBook(ticker=market.ticker, timestamp=timeutils.utc_now(),
                                  yes_bids=(PriceLevel(70, 100),),
                                  no_bids=(PriceLevel(20, 500),))  # yes_ask = 80 (<98)
            async def fake_get_orderbook(ticker, depth=32):
                return fake_book
            async def fake_get_market(ticker):
                from weather_arb.market_client import MarketInfo
                return MarketInfo(ticker=ticker, status="active", title="", close_time_utc=None)
            client.get_orderbook = fake_get_orderbook  # type: ignore
            client.get_market = fake_get_market        # type: ignore

            from weather_arb.forecast_ingest import ForecastClient
            from weather_arb.market_discovery import MarketRegistry
            forecast = ForecastClient(session=None, settings=settings)  # unused in lock-in
            rm = RiskManager(settings=settings, db=db)
            registry = MarketRegistry((market,))
            engine = ArbitrageEngine(settings=settings, db=db, client=client, risk=rm,
                                     forecast=forecast, registry=registry)

            # Observed max 92 >= strike 90 -> deterministic lock-in.
            cdate = timeutils.climate_date_str(timeutils.utc_now(), -6)
            await db.update_tmax(icao="KORD", climate_date=cdate, temp_f=92.0, observed_at_utc=timeutils.utc_now())
            await engine.on_new_high("KORD", cdate, 92.0)

            order = await db.get_order(f"s1-{market.ticker}-{cdate}")
            check("lock-in order journaled", order is not None)
            check("lock-in is a BUY YES limit at ceiling 98",
                  order is not None and order.action == Action.BUY and order.side == Side.YES and order.price_cents == 98)
            check("lock-in resting in dry-run", order is not None and order.status == OrderStatus.RESTING)
            check("lock-in sized within 5% cap (<= ~62 @98c on $50)",
                  order is not None and 0 < order.count <= 62)

            # Idempotency: second call should not create a duplicate/extra order.
            before = len(await db.get_open_orders(ticker=market.ticker))
            await engine.on_new_high("KORD", cdate, 92.0)
            after = len(await db.get_open_orders(ticker=market.ticker))
            check("lock-in idempotent", before == after == 1)


async def main():
    test_orderbook_math()
    test_kalshi_orderbook_parse()
    test_truncated_normal()
    test_iso_duration()
    test_climate_day_bounds()
    test_climate_date_lst_no_dst()
    test_metar_temp_conversion()
    await test_rate_limiter()
    await test_db_and_reconcile()
    await test_risk_sizing()
    await test_engine_dry_run_lock_in()
    print("\n" + ("ALL PASS" if not failures else f"FAILURES: {failures}"))
    sys.exit(1 if failures else 0)


asyncio.run(main())
