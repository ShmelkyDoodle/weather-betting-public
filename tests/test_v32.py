"""L2 depth probe on lock-ins — resolving the "depth illusion" (offline, stubbed).

The 17.4c average discount measured on lock-in windows is only real if size rests
at that price. A headline ask of 14c with 1 contract behind it, and the next 99 at
98c, is worth nothing. These lock down the sweep math and the fact that every
lock-in now leaves a permanent L2 record in the log.
"""
import asyncio, logging, os, sqlite3, sys, tempfile
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


def test_depth_illusion():
    """The exact scenario this was built for: 1 contract cheap, the rest far away."""
    ob = OrderBook("T", timeutils.utc_now(),
                   no_bids=(PriceLevel(86, 1), PriceLevel(82, 12), PriceLevel(2, 200)))
    m = ob.get_l2_sweep_metrics(Side.YES, 100)
    check("headline ask is the cheap 1-lot", ob.best_yes_ask.price_cents == 14)
    check("top_levels shows the ladder, best first",
          m["top_levels"] == "14c x1, 18c x12, 98c x200")
    # 1@14 + 12@18 + 87@98 = 14 + 216 + 8526 = 8756 over 100
    check("VWAP exposes the illusion (87.56c, not 14c)", m["vwap_cents"] == 87.56)
    check("book was NOT cleared (213 resting)", m["book_cleared"] is False)
    check("fillable capped at target", m["fillable_size"] == 100)
    check("depth_size is the whole ladder", m["depth_size"] == 213)


def test_genuinely_deep_book():
    """A real opportunity must survive the same probe."""
    ob = OrderBook("T", timeutils.utc_now(), no_bids=(PriceLevel(86, 500),))
    m = ob.get_l2_sweep_metrics(Side.YES, 100)
    check("VWAP equals the touch when size is there", m["vwap_cents"] == 14.0)
    check("no illusion flagged", m["book_cleared"] is False)
    check("top_ask matches VWAP", m["top_ask_cents"] == 14)


def test_thin_book_is_flagged():
    ob = OrderBook("T", timeutils.utc_now(),
                   no_bids=(PriceLevel(90, 3), PriceLevel(88, 4)))
    m = ob.get_l2_sweep_metrics(Side.YES, 100)
    check("book_cleared when short of target", m["book_cleared"] is True)
    check("fillable is what actually rests", m["fillable_size"] == 7)
    # 3@10 + 4@12 = 30 + 48 = 78 over 7 = 11.142857 -> 11.14
    check("VWAP is for the PARTIAL fill", m["vwap_cents"] == 11.14)


def test_no_side_uses_the_other_ladder():
    """A NO sweep must read the YES bids, not the NO bids."""
    ob = OrderBook("T", timeutils.utc_now(),
                   yes_bids=(PriceLevel(95, 2), PriceLevel(60, 400)),
                   no_bids=(PriceLevel(30, 999),))
    no = ob.get_l2_sweep_metrics(Side.NO, 100)
    # yes_bids 95x2 / 60x400  ->  no_asks 5c x2, 40c x400
    check("NO ladder derives from YES bids", no["top_levels"] == "5c x2, 40c x400")
    check("NO best ask matches the book", no["top_ask_cents"] == ob.best_no_ask.price_cents)
    yes = ob.get_l2_sweep_metrics(Side.YES, 100)
    check("YES and NO ladders are different", yes["top_ask_cents"] != no["top_ask_cents"])


def test_edges():
    ob = OrderBook("T", timeutils.utc_now())
    m = ob.get_l2_sweep_metrics(Side.YES, 100)
    check("empty book -> vwap None", m["vwap_cents"] is None)
    check("empty book -> cleared", m["book_cleared"] is True and m["fillable_size"] == 0)
    check("empty book -> readable label", m["top_levels"] == "(empty)")
    deep = OrderBook("T", timeutils.utc_now(), no_bids=(PriceLevel(50, 10),))
    check("zero target never divides by zero",
          deep.get_l2_sweep_metrics(Side.YES, 0)["vwap_cents"] is None)
    check("does not mutate the book", deep.no_bids == (PriceLevel(50, 10),))


async def test_lock_in_logs_l2():
    """Every lock-in must leave a permanent L2 record in the log, alerts or not."""
    records = []
    class Cap(logging.Handler):
        def emit(self, r): records.append(r)
    lg = logging.getLogger("weather_arb.arb_engine")
    h = Cap(); lg.addHandler(h); lg.setLevel(logging.INFO)

    sent = []
    class Alerts:
        def send(self, title, message, *, priority="high", tags="warning"):
            sent.append(message)

    try:
        with tempfile.TemporaryDirectory() as d:
            settings = Settings(dry_run=True)
            async with Database(os.path.join(d, "l.sqlite3")) as db:
                eng = ArbitrageEngine(
                    settings=settings, db=db, client=KalshiClient(settings),
                    risk=RiskManager(settings=settings, db=db),
                    forecast=ForecastClient(session=None, settings=settings),
                    registry=MarketRegistry(), alerts=Alerts())
                mkt = WeatherMarket("KXHIGHDEN-26AUG18-B90.5", Venue.KALSHI, "KDEN",
                                    floor_strike_f=90.0, cap_strike_f=91.0,
                                    strike_type=STRIKE_BETWEEN)
                # certain NO; the NO ladder comes from the YES bids
                book = OrderBook("T", timeutils.utc_now(),
                                 yes_bids=(PriceLevel(58, 1), PriceLevel(20, 300)),
                                 no_bids=(PriceLevel(30, 300),))
                eng._get_book = lambda t: asyncio.sleep(0, result=book)
                eng._is_tradeable = lambda t: asyncio.sleep(0, result=True)
                await eng._evaluate_lock_in(mkt, climate_date="2026-08-18", tmax_f=93.0)
    finally:
        lg.removeHandler(h)

    l2 = [r for r in records if r.getMessage() == "lock-in L2 depth"]
    check("an L2 depth line was logged", len(l2) == 1)
    if l2:
        ctx = l2[0].context
        check("log carries top_levels", "x" in str(ctx["top_levels"]))
        check("log carries a VWAP", ctx["vwap_cents"] is not None)
        check("log records the probe size", ctx["probe_size"] == L2_PROBE_SIZE)
        check("log records whether the book cleared", "book_cleared" in ctx)
        # 1@42 + 99@80 -> the illusion, and the log must show it
        check("VWAP differs from the touch here",
              ctx["vwap_cents"] != ctx["top_ask_cents"])
    check("push carries the L2 line", sent and "L2 Depth:" in sent[0])
    check("push carries the VWAP label", sent and f"VWAP({L2_PROBE_SIZE})" in sent[0])


for fn in (test_depth_illusion, test_genuinely_deep_book, test_thin_book_is_flagged,
           test_no_side_uses_the_other_ladder, test_edges):
    print(f"\n-- {fn.__name__}")
    fn()
print("\n-- test_lock_in_logs_l2")
asyncio.run(test_lock_in_logs_l2())

print("\n" + ("ALL PASS" if not failures else f"FAILURES: {failures}"))
sys.exit(1 if failures else 0)
