"""Strategy-1 lock-in monitoring: ntfy push + ledger tagging (offline, stubbed).

Strategy 1 fires rarely and has never fired in 4+ weeks of paper running, so the
only way to know it worked is a push the moment it happens. These lock down:
  * the push fires exactly when an order clears the RISK GATE -- not merely when
    the temperature condition is met, and never when risk rejects;
  * the payload names the right breached boundary (cap for certain-NO, floor for
    certain-YES) -- reporting the other one would be meaningless;
  * the push never runs on the event loop and never breaks the trade path;
  * the ledger row is tagged `lock_in`, distinguishable from `model_edge`.
"""
import asyncio, logging, os, sqlite3, sys, tempfile
import os as _osp
sys.path.insert(0, _osp.path.dirname(_osp.path.dirname(_osp.path.abspath(__file__))))

from weather_arb import timeutils
from weather_arb.arb_engine import ArbitrageEngine
from weather_arb.config import Settings
from weather_arb.db import Database
from weather_arb.forecast_ingest import ForecastClient
from weather_arb.market_client import KalshiClient
from weather_arb.market_discovery import MarketRegistry
from weather_arb.models import (
    OrderBook, PriceLevel, Side, Strategy, Venue, WeatherMarket,
    STRIKE_BETWEEN, STRIKE_GREATER,
)
from weather_arb.risk_manager import RiskManager

# The dead-ntfy test deliberately raises inside log.exception; silence the traceback.
logging.getLogger("weather_arb.arb_engine").setLevel(logging.CRITICAL)

failures = []
def check(n, c):
    print(("PASS" if c else "FAIL"), "-", n)
    if not c: failures.append(n)


class StubAlerts:
    """Captures pushes. `boom` makes send() raise, as a dead ntfy host would."""

    def __init__(self, boom=False):
        self.sent = []
        self.boom = boom
        self.thread_ids = []

    def send(self, title, message, *, priority="high", tags="warning"):
        import threading
        self.thread_ids.append(threading.get_ident())
        if self.boom:
            raise RuntimeError("ntfy unreachable")
        self.sent.append({"title": title, "message": message,
                          "priority": priority, "tags": tags})


async def _engine(db, alerts, settings=None):
    settings = settings or Settings(dry_run=True)
    return ArbitrageEngine(
        settings=settings, db=db, client=KalshiClient(settings),
        risk=RiskManager(settings=settings, db=db),
        forecast=ForecastClient(session=None, settings=settings),
        registry=MarketRegistry(), alerts=alerts,
    )


def _book(yes_ask_c, size=500):
    """A book whose best YES ask is `yes_ask_c` (NO ask = 100 - that)."""
    return OrderBook("T", timeutils.utc_now(),
                     yes_bids=(PriceLevel(yes_ask_c - 2, size),),
                     no_bids=(PriceLevel(100 - yes_ask_c, size),))


async def test_certain_no_push_and_tagging():
    """Interior bucket [90,91]; observed max 93 blew past the cap -> certain NO."""
    alerts = StubAlerts()
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "a.sqlite3")
        async with Database(path) as db:
            eng = await _engine(db, alerts)
            mkt = WeatherMarket("KXHIGHDEN-26AUG18-B90.5", Venue.KALSHI, "KDEN",
                                floor_strike_f=90.0, cap_strike_f=91.0,
                                strike_type=STRIKE_BETWEEN)
            eng._get_book = lambda t: asyncio.sleep(0, result=_book(60))
            eng._is_tradeable = lambda t: asyncio.sleep(0, result=True)
            await eng._evaluate_lock_in(mkt, climate_date="2026-08-18", tmax_f=93.0)

        check("exactly one push fired", len(alerts.sent) == 1)
        if alerts.sent:
            a = alerts.sent[0]
            m, t = a["message"], a["title"]
            check("title names the ticker and side",
                  "KXHIGHDEN-26AUG18-B90.5" in t and "NO" in t)
            check("title marks DRY-RUN", "DRY-RUN" in t)
            check("payload carries the observed tmax", "93.0" in m and "93.00" in m)
            check("payload names the CAP as the breached boundary (not the floor)",
                  "cap 91F" in m and "floor" not in m.split("->")[0])
            # Derive it rather than hardcode: for a NO lock-in the engine crosses
            # best_no_ask, which is 100 - best_yes_bid, not the YES ask.
            expected_ask = _book(60).best_no_ask.price_cents
            check(f"payload carries the NO ask being crossed ({expected_ask}c)",
                  f"ask {expected_ask}c" in m)
            check("high priority", a["priority"] == "high")
            check("distinct tag, not the generic warning", a["tags"] != "warning")

        # Ledger tagging -- the row must be attributable to Strategy 1.
        conn = sqlite3.connect(path)
        rows = conn.execute(
            "SELECT ticker, strategy, side, count, price_cents, status "
            "FROM open_limit_orders").fetchall()
        conn.close()
        check("order journaled in dry-run", len(rows) == 1)
        if rows:
            check("tagged strategy='lock_in'", rows[0][1] == Strategy.LOCK_IN.value)
            check("tag is not model_edge", rows[0][1] != Strategy.MODEL_EDGE.value)
            check("bought the certain NO side", rows[0][2] == Side.NO.value)


async def test_certain_yes_names_the_floor():
    """Tail-high bucket (>=105); observed 106 -> certain YES, floor is the boundary."""
    alerts = StubAlerts()
    with tempfile.TemporaryDirectory() as d:
        async with Database(os.path.join(d, "b.sqlite3")) as db:
            eng = await _engine(db, alerts)
            mkt = WeatherMarket("KXHIGHAUS-26AUG22-T105", Venue.KALSHI, "KAUS",
                                floor_strike_f=105.0, strike_type=STRIKE_GREATER)
            eng._get_book = lambda t: asyncio.sleep(0, result=_book(70))
            eng._is_tradeable = lambda t: asyncio.sleep(0, result=True)
            await eng._evaluate_lock_in(mkt, climate_date="2026-08-22", tmax_f=106.0)
    check("push fired for certain-YES", len(alerts.sent) == 1)
    if alerts.sent:
        m = alerts.sent[0]["message"]
        check("names the FLOOR as the breached boundary", "floor 105F" in m)
        check("does not claim a cap was breached", "cap" not in m.split("->")[0])
        check("carries the YES ask being crossed", "ask 70c" in m)


async def test_no_push_when_risk_rejects():
    """An alert for an order that was never journaled would be a lie."""
    alerts = StubAlerts()
    # Zero equity -> the risk manager approves nothing.
    settings = Settings(dry_run=True)
    object.__setattr__(settings.risk, "account_equity_usd", 0.0)
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "c.sqlite3")
        async with Database(path) as db:
            eng = await _engine(db, alerts, settings)
            mkt = WeatherMarket("KXHIGHNY-26AUG20-B84.5", Venue.KALSHI, "KNYC",
                                floor_strike_f=84.0, cap_strike_f=85.0,
                                strike_type=STRIKE_BETWEEN)
            eng._get_book = lambda t: asyncio.sleep(0, result=_book(50))
            eng._is_tradeable = lambda t: asyncio.sleep(0, result=True)
            await eng._evaluate_lock_in(mkt, climate_date="2026-08-20", tmax_f=88.0)
        conn = sqlite3.connect(path)
        n_orders, = conn.execute("SELECT count(*) FROM open_limit_orders").fetchone()
        conn.close()
    check("risk rejected -> nothing journaled", n_orders == 0)
    check("risk rejected -> NO push", alerts.sent == [])


async def test_no_push_when_condition_not_met():
    alerts = StubAlerts()
    with tempfile.TemporaryDirectory() as d:
        async with Database(os.path.join(d, "d.sqlite3")) as db:
            eng = await _engine(db, alerts)
            mkt = WeatherMarket("KXHIGHNY-26AUG20-B84.5", Venue.KALSHI, "KNYC",
                                floor_strike_f=84.0, cap_strike_f=85.0,
                                strike_type=STRIKE_BETWEEN)
            eng._get_book = lambda t: asyncio.sleep(0, result=_book(50))
            eng._is_tradeable = lambda t: asyncio.sleep(0, result=True)
            # 84.5 sits INSIDE [84, 85] -- settlement is not yet certain.
            await eng._evaluate_lock_in(mkt, climate_date="2026-08-20", tmax_f=84.5)
    check("no lock-in -> no push", alerts.sent == [])


async def test_alerting_never_breaks_the_trade_path():
    """A dead ntfy host must not lose the order, and must not run inline."""
    import threading
    alerts = StubAlerts(boom=True)
    main_thread = threading.get_ident()
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "e.sqlite3")
        async with Database(path) as db:
            eng = await _engine(db, alerts)
            mkt = WeatherMarket("KXHIGHDEN-26AUG18-B90.5", Venue.KALSHI, "KDEN",
                                floor_strike_f=90.0, cap_strike_f=91.0,
                                strike_type=STRIKE_BETWEEN)
            eng._get_book = lambda t: asyncio.sleep(0, result=_book(60))
            eng._is_tradeable = lambda t: asyncio.sleep(0, result=True)
            await eng._evaluate_lock_in(mkt, climate_date="2026-08-18", tmax_f=93.0)
        conn = sqlite3.connect(path)
        n, = conn.execute("SELECT count(*) FROM open_limit_orders "
                          "WHERE strategy='lock_in'").fetchone()
        conn.close()
    check("push raised, order still journaled", n == 1)
    check("send() ran OFF the event loop thread",
          alerts.thread_ids and all(t != main_thread for t in alerts.thread_ids))


async def test_engine_without_alerts_still_works():
    """alerts=None is the default for every offline test that builds an engine."""
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "f.sqlite3")
        async with Database(path) as db:
            eng = await _engine(db, None)
            mkt = WeatherMarket("KXHIGHDEN-26AUG18-B90.5", Venue.KALSHI, "KDEN",
                                floor_strike_f=90.0, cap_strike_f=91.0,
                                strike_type=STRIKE_BETWEEN)
            eng._get_book = lambda t: asyncio.sleep(0, result=_book(60))
            eng._is_tradeable = lambda t: asyncio.sleep(0, result=True)
            await eng._evaluate_lock_in(mkt, climate_date="2026-08-18", tmax_f=93.0)
        conn = sqlite3.connect(path)
        n, = conn.execute("SELECT count(*) FROM open_limit_orders").fetchone()
        conn.close()
    check("no alerts configured -> order still placed, no crash", n == 1)


async def main():
    for fn in (test_certain_no_push_and_tagging, test_certain_yes_names_the_floor,
               test_no_push_when_risk_rejects, test_no_push_when_condition_not_met,
               test_alerting_never_breaks_the_trade_path,
               test_engine_without_alerts_still_works):
        print(f"\n-- {fn.__name__}")
        await fn()

asyncio.run(main())
print("\n" + ("ALL PASS" if not failures else f"FAILURES: {failures}"))
sys.exit(1 if failures else 0)
