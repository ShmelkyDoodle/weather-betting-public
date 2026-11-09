"""Lock-in telemetry: detection + L2 depth must accrue on PRICED-OUT lock-ins.

The reason this exists. The L2 probe added in `a446dd2` sat *below* the price
ceiling gate (`best.price_cents >= ceiling -> return`). But `backtest --strategy
1` measures 420 of 421 lock-ins already at exactly 100c against a 98c ceiling, so
the probe fired on roughly 1 event a month and could never resolve the depth
question it was built for. It is now above the gate.

Also locks the log-volume guard: `_evaluate_lock_in` runs every eval cycle
(~120s) and a market stays certain for the rest of the climate day, so telemetry
must latch once per market per day or it drowns the study in its own noise.

⚠ SCOPE, asserted in prose because no test can check it: this samples at t2
(METAR publication). The ~17c discount lives at t1, ~48 min earlier, and is not
observable without a real-time sub-hourly feed. See HANDOFF.md section 3.
"""
import asyncio, logging, os, sys, tempfile
import os as _osp
sys.path.insert(0, _osp.path.dirname(_osp.path.dirname(_osp.path.abspath(__file__))))

from weather_arb import timeutils
from weather_arb.arb_engine import ArbitrageEngine, L2_PROBE_SIZE
from weather_arb.config import Settings
from weather_arb.db import Database
from weather_arb.forecast_ingest import ForecastClient
from weather_arb.market_client import KalshiClient
from weather_arb.market_discovery import MarketRegistry
from weather_arb.models import (
    OrderBook, PriceLevel, Side, Venue, WeatherMarket, STRIKE_BETWEEN,
)
from weather_arb.risk_manager import RiskManager

failures = []
def check(n, c):
    print(("PASS" if c else "FAIL"), "-", n)
    if not c: failures.append(n)


class _Cap(logging.Handler):
    def __init__(self): super().__init__(); self.records = []
    def emit(self, r): self.records.append(r)

    def msgs(self, name): return [r for r in self.records if r.getMessage() == name]


def _market():
    # certain-NO once the max climbs past cap 91: bucket {90, 91}, observed 93.
    return WeatherMarket("KXHIGHDEN-26AUG18-B90.5", Venue.KALSHI, "KDEN",
                         floor_strike_f=90.0, cap_strike_f=91.0,
                         strike_type=STRIKE_BETWEEN)


def _priced_out_book():
    """The 420/421 case: certainty already fully priced.

    We buy NO, and the NO ladder is derived from the YES bids (no_ask =
    100 - yes_bid), so a 1c YES bid is a 99c NO offer -- above the 98c ceiling.
    """
    return OrderBook("T", timeutils.utc_now(),
                     yes_bids=(PriceLevel(1, 500),),
                     no_bids=(PriceLevel(30, 300),))


def _tradeable_book():
    """The 1/421 case: the certain side is still buyable under the ceiling."""
    return OrderBook("T", timeutils.utc_now(),
                     yes_bids=(PriceLevel(58, 1), PriceLevel(20, 300)),
                     no_bids=(PriceLevel(30, 300),))


async def _engine(db, alerts=None):
    settings = Settings(dry_run=True)
    return ArbitrageEngine(
        settings=settings, db=db, client=KalshiClient(settings),
        risk=RiskManager(settings=settings, db=db),
        forecast=ForecastClient(session=None, settings=settings),
        registry=MarketRegistry(), alerts=alerts)


async def test_priced_out_still_logs_depth():
    """THE REGRESSION TEST: a 99c ask must still yield a depth sample."""
    cap = _Cap()
    lg = logging.getLogger("weather_arb.arb_engine")
    lg.addHandler(cap); lg.setLevel(logging.INFO)
    try:
        with tempfile.TemporaryDirectory() as d:
            async with Database(os.path.join(d, "l.sqlite3")) as db:
                eng = await _engine(db)
                book = _priced_out_book()
                eng._get_book = lambda t: asyncio.sleep(0, result=book)
                eng._is_tradeable = lambda t: asyncio.sleep(0, result=True)
                await eng._evaluate_lock_in(_market(), climate_date="2026-08-18",
                                            tmax_f=93.0)
                orders = await db.get_open_orders()
    finally:
        lg.removeHandler(cap)

    check("detection logged even though priced out", len(cap.msgs("lock-in detected")) == 1)
    depth = cap.msgs("lock-in L2 depth")
    check("DEPTH logged even though priced out", len(depth) == 1)
    if depth:
        ctx = depth[0].context
        check("priced_out flag records why no order followed", ctx["priced_out"] is True)
        check("depth sample carries the real touch", ctx["top_ask_cents"] == 99)
        check("depth sample carries a VWAP", ctx["vwap_cents"] == 99.0)
        check("depth sample records the ceiling it was judged against",
              ctx["ceiling_cents"] == 98)
    check("no order was submitted on a priced-out lock-in", len(orders) == 0)
    check("no lock-in signal on a priced-out lock-in",
          len(cap.msgs("lock-in signal")) == 0)


async def test_latches_once_per_day_then_rearms():
    cap = _Cap()
    lg = logging.getLogger("weather_arb.arb_engine")
    lg.addHandler(cap); lg.setLevel(logging.INFO)
    try:
        with tempfile.TemporaryDirectory() as d:
            async with Database(os.path.join(d, "l.sqlite3")) as db:
                eng = await _engine(db)
                book = _priced_out_book()
                eng._get_book = lambda t: asyncio.sleep(0, result=book)
                eng._is_tradeable = lambda t: asyncio.sleep(0, result=True)
                mkt = _market()
                for _ in range(30):     # an hour of 120s eval cycles
                    await eng._evaluate_lock_in(mkt, climate_date="2026-08-18",
                                                tmax_f=93.0)
                same_day = len(cap.msgs("lock-in detected"))
                depth_same_day = len(cap.msgs("lock-in L2 depth"))
                await eng._evaluate_lock_in(mkt, climate_date="2026-08-19",
                                            tmax_f=93.0)
                after_roll = len(cap.msgs("lock-in detected"))
    finally:
        lg.removeHandler(cap)

    check("30 cycles produce ONE detection line", same_day == 1)
    check("30 cycles produce ONE depth line", depth_same_day == 1)
    check("climate-date rollover re-arms detection", after_roll == 2)


async def test_depth_latch_is_independent_of_detection():
    """A missing book must not burn the depth sample for the whole day."""
    cap = _Cap()
    lg = logging.getLogger("weather_arb.arb_engine")
    lg.addHandler(cap); lg.setLevel(logging.INFO)
    try:
        with tempfile.TemporaryDirectory() as d:
            async with Database(os.path.join(d, "l.sqlite3")) as db:
                eng = await _engine(db)
                eng._is_tradeable = lambda t: asyncio.sleep(0, result=True)
                mkt = _market()
                # cycle 1: certain, but no book yet
                eng._get_book = lambda t: asyncio.sleep(0, result=None)
                await eng._evaluate_lock_in(mkt, climate_date="2026-08-18", tmax_f=93.0)
                first_detect = len(cap.msgs("lock-in detected"))
                first_depth = len(cap.msgs("lock-in L2 depth"))
                # cycle 2: the book arrives
                book = _priced_out_book()
                eng._get_book = lambda t: asyncio.sleep(0, result=book)
                await eng._evaluate_lock_in(mkt, climate_date="2026-08-18", tmax_f=93.0)
    finally:
        lg.removeHandler(cap)

    check("detection fires on the bookless cycle", first_detect == 1)
    check("no depth sample without a book", first_depth == 0)
    check("depth still captured when the book arrives later",
          len(cap.msgs("lock-in L2 depth")) == 1)
    check("detection did NOT re-fire", len(cap.msgs("lock-in detected")) == 1)


async def test_tradeable_path_still_works():
    """The 1/421 case must be unchanged: order submitted, push carries L2."""
    cap = _Cap()
    lg = logging.getLogger("weather_arb.arb_engine")
    lg.addHandler(cap); lg.setLevel(logging.INFO)
    sent = []
    class Alerts:
        def send(self, title, message, *, priority="high", tags="warning"):
            sent.append(message)
    try:
        with tempfile.TemporaryDirectory() as d:
            async with Database(os.path.join(d, "l.sqlite3")) as db:
                eng = await _engine(db, alerts=Alerts())
                book = _tradeable_book()
                eng._get_book = lambda t: asyncio.sleep(0, result=book)
                eng._is_tradeable = lambda t: asyncio.sleep(0, result=True)
                await eng._evaluate_lock_in(_market(), climate_date="2026-08-18",
                                            tmax_f=93.0)
                orders = await db.get_open_orders()
    finally:
        lg.removeHandler(cap)

    check("tradeable lock-in still submits", len(orders) == 1)
    check("tradeable lock-in still logs its signal",
          len(cap.msgs("lock-in signal")) == 1)
    depth = cap.msgs("lock-in L2 depth")
    check("tradeable lock-in still logs depth", len(depth) == 1)
    check("priced_out is False on the tradeable path",
          depth and depth[0].context["priced_out"] is False)
    check("push still carries the L2 line", sent and "L2 Depth:" in sent[0])
    check("push still carries the VWAP label",
          sent and f"VWAP({L2_PROBE_SIZE})" in sent[0])


async def test_no_telemetry_before_certainty():
    """Nothing may log until deterministic_lock_in actually returns non-None."""
    cap = _Cap()
    lg = logging.getLogger("weather_arb.arb_engine")
    lg.addHandler(cap); lg.setLevel(logging.INFO)
    try:
        with tempfile.TemporaryDirectory() as d:
            async with Database(os.path.join(d, "l.sqlite3")) as db:
                eng = await _engine(db)
                eng._get_book = lambda t: asyncio.sleep(0, result=_priced_out_book())
                eng._is_tradeable = lambda t: asyncio.sleep(0, result=True)
                # 90.5 sits INSIDE bucket {90,91} -- outcome not yet certain
                await eng._evaluate_lock_in(_market(), climate_date="2026-08-18",
                                            tmax_f=90.5)
    finally:
        lg.removeHandler(cap)
    check("no detection line while the outcome is still open",
          len(cap.msgs("lock-in detected")) == 0)
    check("no depth line while the outcome is still open",
          len(cap.msgs("lock-in L2 depth")) == 0)


for fn in (test_priced_out_still_logs_depth, test_latches_once_per_day_then_rearms,
           test_depth_latch_is_independent_of_detection,
           test_tradeable_path_still_works, test_no_telemetry_before_certainty):
    print(f"\n-- {fn.__name__}")
    asyncio.run(fn())

print("\n" + ("ALL PASS" if not failures else f"FAILURES: {failures}"))
sys.exit(1 if failures else 0)
