"""#A Discovery-experiment telemetry: every detection persists, at every gate (offline).

WHAT THIS PROTECTS. `backtest --strategy 1` graded only detections that cleared
the 98c price ceiling -- 1 of 421 in a 30-day run -- so a 3% error rate in the
lock-in rule went unmeasured while the project recorded "421 detections, 0 wrong
calls" as verified ground truth. The experiment's whole denominator is
DETECTIONS, not tradeable detections, and these tests are what stop that
regressing.

Also locks the parts of the substrate that are easy to get quietly wrong:
ceiling-truncated sweeps (the 17.4c estimate came from reading top-of-book),
the raw ladder being stored rather than a 3-level summary, one-way grading, and
the 5-minute source being unable to reach an order path.
"""
import asyncio, json, os, sys, tempfile
from datetime import datetime, timedelta, timezone
import os as _osp
sys.path.insert(0, _osp.path.dirname(_osp.path.dirname(_osp.path.abspath(__file__))))

from weather_arb import timeutils
from weather_arb.arb_engine import ArbitrageEngine, SOURCE_METAR, SOURCE_NWS5MIN
from weather_arb.config import Settings
from weather_arb.db import Database
from weather_arb.forecast_ingest import ForecastClient
from weather_arb.market_client import KalshiClient
from weather_arb.market_discovery import MarketRegistry
from weather_arb.models import (
    OrderBook, PriceLevel, Side, Venue, WeatherMarket, STRIKE_BETWEEN,
)
from weather_arb.nws_obs import (
    PRECISION_TENTH_C_F, PRECISION_WHOLE_C_F, SOURCE_5MIN, SOURCE_HOURLY,
    SUB_HOURLY_MAX_GAP_S, NwsObservationPoller, classify_precision,
    median_gap_seconds, _parse_features,
)
from weather_arb.risk_manager import RiskManager
from weather_arb.telemetry import (
    GATE_ACCEPTED, GATE_NOT_TRADEABLE, GATE_PRICED_OUT, BookProbe, ladder_json,
    sweep_rows,
)

failures = []
def check(n, c):
    print(("PASS" if c else "FAIL"), "-", n)
    if not c: failures.append(n)


def _market(ticker="KXHIGHDEN-26AUG18-B90.5"):
    # certain-NO once the max passes the {90,91} bucket: bound 91.5.
    return WeatherMarket(ticker, Venue.KALSHI, "KDEN",
                         floor_strike_f=90.0, cap_strike_f=91.0,
                         strike_type=STRIKE_BETWEEN)


def _priced_out_book():
    """The 420/421 case: a 1c YES bid is a 99c NO offer, above the 98c ceiling."""
    return OrderBook("T", timeutils.utc_now(),
                     yes_bids=(PriceLevel(1, 500),), no_bids=(PriceLevel(30, 300),))


def _thin_book():
    """The depth illusion: a cheap touch one contract deep, then the wall.

    NO asks derive from YES bids (no_ask = 100 - yes_bid), so yes_bids of
    86/82/2 give NO offers at 14c x1, 18c x12, 98c x200.
    """
    return OrderBook("T", timeutils.utc_now(),
                     yes_bids=(PriceLevel(86, 1), PriceLevel(82, 12),
                               PriceLevel(2, 200)),
                     no_bids=(PriceLevel(30, 300),))


async def _engine(db, recorder=None):
    settings = Settings(dry_run=True)
    return ArbitrageEngine(
        settings=settings, db=db, client=KalshiClient(settings),
        risk=RiskManager(settings=settings, db=db),
        forecast=ForecastClient(session=None, settings=settings),
        registry=MarketRegistry(), recorder=recorder)


async def _events(db):
    async with db._c.execute("SELECT * FROM lockin_events ORDER BY id") as cur:
        return [dict(r) for r in await cur.fetchall()]


async def _snapshots(db):
    async with db._c.execute("SELECT * FROM book_snapshots ORDER BY id") as cur:
        return [dict(r) for r in await cur.fetchall()]


async def _sweeps(db):
    async with db._c.execute("SELECT * FROM sweep_metrics") as cur:
        return [dict(r) for r in await cur.fetchall()]


# --------------------------------------------------------------------------- #
# 1. Every detection persists, whichever gate stops it
# --------------------------------------------------------------------------- #
async def test_priced_out_detection_is_persisted():
    with tempfile.TemporaryDirectory() as d:
        async with Database(os.path.join(d, "t.sqlite3")) as db:
            eng = await _engine(db)
            book = _priced_out_book()
            eng._get_book = lambda t: asyncio.sleep(0, result=book)
            eng._is_tradeable = lambda t: asyncio.sleep(0, result=True)
            await eng._evaluate_lock_in(_market(), climate_date="2026-08-18",
                                        tmax_f=93.0)
            ev, sn = await _events(db), await _snapshots(db)
            orders = await db.get_open_orders()

    check("a priced-out detection IS recorded", len(ev) == 1)
    check("no order followed it", len(orders) == 0)
    if ev:
        e = ev[0]
        check("the gate reason is recorded on its snapshot",
              sn and sn[0]["gate_outcome"] == GATE_PRICED_OUT)
        check("source is metar", e["source"] == SOURCE_METAR)
        check("the certainty bound is stored, not just the strike",
              e["certainty_bound_f"] == 91.5 and e["boundary_f"] == 91.0)
        check("excess is measured from the bound", abs(e["excess_f"] - 1.5) < 1e-9)
        check("the margin used at detection is recorded",
              e["lockin_margin_f"] == 0.5)
        check("it starts ungraded", e["call_correct"] is None)


async def test_untradeable_detection_is_persisted():
    """The gate that used to `return` before a book was ever fetched."""
    with tempfile.TemporaryDirectory() as d:
        async with Database(os.path.join(d, "t.sqlite3")) as db:
            eng = await _engine(db)
            eng._is_tradeable = lambda t: asyncio.sleep(0, result=False)
            eng._get_book = lambda t: asyncio.sleep(0, result=None)
            # No book is available at all: telemetry must still record the event.
            eng._probe.fetch = lambda t: asyncio.sleep(
                0, result=(None, timeutils.utc_now(), timeutils.utc_now()))
            await eng._evaluate_lock_in(_market(), climate_date="2026-08-18",
                                        tmax_f=93.0)
            ev, sn = await _events(db), await _snapshots(db)

    check("an untradeable detection IS recorded", len(ev) == 1)
    check("with no snapshot, which the report counts as a data-quality miss",
          len(sn) == 0)


async def test_one_immutable_row_per_market_per_day():
    with tempfile.TemporaryDirectory() as d:
        async with Database(os.path.join(d, "t.sqlite3")) as db:
            eng = await _engine(db)
            book = _priced_out_book()
            eng._get_book = lambda t: asyncio.sleep(0, result=book)
            eng._is_tradeable = lambda t: asyncio.sleep(0, result=True)
            mkt = _market()
            for _ in range(30):          # an hour of 120s evaluation cycles
                await eng._evaluate_lock_in(mkt, climate_date="2026-08-18",
                                            tmax_f=93.0)
            same_day = await _events(db)
            await eng._evaluate_lock_in(mkt, climate_date="2026-08-19", tmax_f=93.0)
            next_day = await _events(db)

    check("30 cycles produce exactly one event row", len(same_day) == 1)
    check("a new climate day re-arms it", len(next_day) == 2)


# --------------------------------------------------------------------------- #
# 2. Executable economics, not top-of-book
# --------------------------------------------------------------------------- #
async def test_sweeps_are_ceiling_truncated_and_record_the_illusion():
    with tempfile.TemporaryDirectory() as d:
        async with Database(os.path.join(d, "t.sqlite3")) as db:
            eng = await _engine(db)
            book = _thin_book()
            eng._get_book = lambda t: asyncio.sleep(0, result=book)
            eng._is_tradeable = lambda t: asyncio.sleep(0, result=True)
            await eng._evaluate_lock_in(_market(), climate_date="2026-08-18",
                                        tmax_f=93.0)
            sw = {int(r["target_size"]): r for r in await _sweeps(db)
                  if r["side"] == "no"}
            sn = await _snapshots(db)

    check("sweeps are stored for the traded side", bool(sw))
    if 1 in sw:
        check("1 contract gets the headline 14c", sw[1]["vwap_cents"] == 14.0)
    if 100 in sw:
        r = sw[100]
        check(f"100 contracts cost the real VWAP, not 14c (got {r['vwap_cents']})",
              abs(float(r["vwap_cents"]) - 87.56) < 0.01)
        check("slippage vs top-of-book is recorded",
              abs(float(r["slippage_cents"]) - 73.56) < 0.01)
        check("a wrong call is booked as the full loss it would be",
              float(r["net_if_wrong_usd"]) < -80.0)
    if 500 in sw:
        check("an unfillable size is flagged rather than silently truncated",
              sw[500]["book_cleared"] == 1 and sw[500]["fillable_size"] == 213)
    if -1 in sw:
        check("the whole-ladder row records what was actually available",
              sw[-1]["fillable_size"] == 213)
    if sn:
        lad = json.loads(sn[0]["ladder_json"])
        check("the RAW ladder is stored, not a 3-level summary",
              lad["yes_bids"] == [[86, 1], [82, 12], [2, 200]])
        check("depth to the ceiling is recorded",
              sn[0]["no_depth_to_ceiling"] == 213)


def test_sweep_respects_the_ceiling():
    book = _thin_book()
    rows = {int(r["target_size"]): r
            for r in sweep_rows(book, snapshot_id=1, ceiling_cents=20,
                                sides=(Side.NO,))}
    check("a 20c ceiling cannot reach the 98c wall",
          rows[100]["fillable_size"] == 13 and rows[100]["book_cleared"] == 1)
    check("and prices only what it could actually take",
          abs(float(rows[100]["vwap_cents"]) - 17.69) < 0.01)


def test_ladder_json_is_the_raw_kalshi_view():
    book = _thin_book()
    lad = json.loads(ladder_json(book))
    check("both sides are stored as Kalshi serves them (bids)",
          set(lad) == {"yes_bids", "no_bids"} and lad["no_bids"] == [[30, 300]])


# --------------------------------------------------------------------------- #
# 3. Grading is one-way and covers every detection
# --------------------------------------------------------------------------- #
async def test_grading_is_one_way():
    with tempfile.TemporaryDirectory() as d:
        async with Database(os.path.join(d, "t.sqlite3")) as db:
            eid = await db.record_lockin_event({
                "ticker": "KXHIGHDEN-26AUG18-B90.5", "series": "KXHIGHDEN",
                "icao": "KDEN", "climate_date": "2026-08-18", "source": "metar",
                "side": "no", "action": "buy", "strike_type": "between",
                "floor_strike_f": 90.0, "cap_strike_f": 91.0, "boundary_f": 91.0,
                "certainty_bound_f": 91.5, "observed_tmax_f": 93.0,
                "lockin_margin_f": 0.5, "margin_f": 2.0, "excess_f": 1.5,
                "observation_at_utc": None, "ingest_at_utc": None,
                "detected_at_utc": timeutils.utc_now().isoformat(),
                "observation_age_s": None,
            })
            pending = await db.ungraded_lockin_events()
            await db.grade_lockin_event(eid, result="no", settled_at_utc=None,
                                        correct=True)
            after_first = await _events(db)
            # A second, contradictory grade must not take.
            await db.grade_lockin_event(eid, result="yes", settled_at_utc=None,
                                        correct=False)
            after_second = await _events(db)
            still_pending = await db.ungraded_lockin_events()

    check("an ungraded detection is listed for grading", len(pending) == 1)
    check("grading records the outcome", after_first[0]["call_correct"] == 1)
    check("a grade can never be silently rewritten",
          after_second[0]["call_correct"] == 1
          and after_second[0]["settled_result"] == "no")
    check("and it leaves the pending queue", len(still_pending) == 0)


# --------------------------------------------------------------------------- #
# 4. The 5-minute source is shadow-only and honest about its precision
# --------------------------------------------------------------------------- #
def test_precision_classification():
    check("whole degC is the coarse 5-minute product",
          classify_precision(22.0, False) == (SOURCE_5MIN, PRECISION_WHOLE_C_F))
    check("tenths degC with a raw METAR is the hourly report",
          classify_precision(22.2, True) == (SOURCE_HOURLY, PRECISION_TENTH_C_F))
    check("precision is read from the VALUE, not assumed from the source",
          classify_precision(22.2, False) == (SOURCE_5MIN, PRECISION_TENTH_C_F))
    # THE FIRST LIVE RUN GOT THIS WRONG. KNYC/KDEN return rows with no
    # rawMessage that are plainly hourly (60 min apart, 3.5-24 h old), so
    # absence of a raw METAR alone reported every station as 5-minute.
    check("an hourly-cadence station is hourly even with no rawMessage",
          classify_precision(22.0, False, sub_hourly_station=False)
          == (SOURCE_HOURLY, PRECISION_WHOLE_C_F))


def test_cadence_detection():
    base = datetime(2026, 8, 26, 3, 0, tzinfo=timezone.utc)
    five = [base + timedelta(minutes=5 * i) for i in range(12)]
    hourly = [base + timedelta(hours=i) for i in range(12)]
    check("a 5-minute station is detected by cadence",
          median_gap_seconds(five) == 300.0)
    check("an hourly station is detected by cadence",
          median_gap_seconds(hourly) == 3600.0)
    check("the threshold separates them",
          median_gap_seconds(five) <= SUB_HOURLY_MAX_GAP_S
          and median_gap_seconds(hourly) > SUB_HOURLY_MAX_GAP_S)
    check("a single observation yields no cadence", median_gap_seconds(five[:1]) is None)


def test_feature_parsing_skips_unusable_rows():
    payload = {"features": [
        {"properties": {"timestamp": "2026-08-26T03:25:00+00:00",
                        "temperature": {"value": 22}, "rawMessage": ""}},
        {"properties": {"timestamp": "2026-08-26T02:54:00+00:00",
                        "temperature": {"value": 22.2}, "rawMessage": "METAR ..."}},
        {"properties": {"timestamp": "2026-08-26T02:50:00+00:00",
                        "temperature": {"value": None}}},       # dropped
        {"properties": {"temperature": {"value": 21}}},          # no timestamp
    ]}
    parsed = _parse_features(payload)
    check("only usable observations survive parsing", len(parsed) == 2)
    check("the 5-minute row is flagged as having no raw METAR",
          parsed[0][2] is False and parsed[1][2] is True)


async def test_shadow_source_records_but_cannot_trade():
    with tempfile.TemporaryDirectory() as d:
        async with Database(os.path.join(d, "t.sqlite3")) as db:
            eng = await _engine(db)
            eng._registry = MarketRegistry((_market(),))
            book = _thin_book()          # deliberately TRADEABLE at 14c
            eng._probe.fetch = lambda t: asyncio.sleep(
                0, result=(book, timeutils.utc_now(), timeutils.utc_now()))
            n = await eng.observe_shadow_lockin(
                icao="KDEN", climate_date="2026-08-18", tmax_f=93.0,
                source=SOURCE_NWS5MIN)
            ev = await _events(db)
            orders = await db.get_open_orders()

    check("the shadow source records a detection", n == 1 and len(ev) == 1)
    check("tagged with its own source", ev and ev[0]["source"] == SOURCE_NWS5MIN)
    check("NO ORDER is placed even though the book was tradeable at 14c",
          len(orders) == 0)


async def test_shadow_source_journals_observations():
    class _Resp:
        status = 200
        async def json(self, content_type=None):
            now = datetime.now(timezone.utc).replace(microsecond=0)
            return {"features": [
                {"properties": {"timestamp": (now - timedelta(minutes=5)).isoformat(),
                                "temperature": {"value": 22}, "rawMessage": ""}},
                {"properties": {"timestamp": (now - timedelta(minutes=10)).isoformat(),
                                "temperature": {"value": 21}, "rawMessage": ""}},
            ]}
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False

    class _Session:
        def get(self, url, params=None, headers=None): return _Resp()

    with tempfile.TemporaryDirectory() as d:
        async with Database(os.path.join(d, "t.sqlite3")) as db:
            settings = Settings(dry_run=True)
            poller = NwsObservationPoller(
                session=_Session(), db=db, settings=settings,
                on_shadow_lockin=None)
            station = settings.station_by_icao("KDEN")
            n = await poller._poll_station(station)
            n_again = await poller._poll_station(station)   # idempotent
            async with db._c.execute(
                "SELECT * FROM source_observations ORDER BY observed_at_utc") as cur:
                rows = [dict(r) for r in await cur.fetchall()]
            # Read inside the context: the whole point is that this feed writes
            # source_observations and NEVER the value the lock-in rule consumes.
            leaked = (await db.get_tmax("KDEN", rows[0]["climate_date"])
                      if rows else "no-rows")

    check("observations are journalled", n == 2 and len(rows) == 2)
    check("re-polling the same valid times inserts nothing new",
          n_again == 2 and len(rows) == 2)
    if rows:
        check("publication age is recorded", rows[0]["publication_age_s"] > 0)
        check("whole-degC precision is recorded honestly",
              rows[0]["precision_f"] == PRECISION_WHOLE_C_F)
        check("the running max advances on this source alone",
              rows[-1]["running_max_f"] == max(r["temp_f"] for r in rows))
    check("and it never touches daily_tmax (the value the rule trades on)",
          leaked is None)


# --------------------------------------------------------------------------- #
# 5. Gate decision reports the reason it actually returned
# --------------------------------------------------------------------------- #
async def test_gate_decision_names_the_blocking_gate():
    with tempfile.TemporaryDirectory() as d:
        async with Database(os.path.join(d, "t.sqlite3")) as db:
            eng = await _engine(db)
            mkt = _market()
            eng._is_tradeable = lambda t: asyncio.sleep(0, result=False)
            out1, *_ = await eng._lock_in_decision(
                mkt, side=Side.NO, ceiling=98, client_order_id="x")
            eng._is_tradeable = lambda t: asyncio.sleep(0, result=True)
            eng._get_book = lambda t: asyncio.sleep(0, result=_priced_out_book())
            out2, *_ = await eng._lock_in_decision(
                mkt, side=Side.NO, ceiling=98, client_order_id="x")
            eng._get_book = lambda t: asyncio.sleep(0, result=_thin_book())
            out3, *_ = await eng._lock_in_decision(
                mkt, side=Side.NO, ceiling=98, client_order_id="x")

    check("an untradeable market is named", out1 == GATE_NOT_TRADEABLE)
    check("a 99c ask is named as priced out", out2 == GATE_PRICED_OUT)
    check("a buyable book is accepted", out3 == GATE_ACCEPTED)


async def main():
    await test_priced_out_detection_is_persisted()
    await test_untradeable_detection_is_persisted()
    await test_one_immutable_row_per_market_per_day()
    await test_sweeps_are_ceiling_truncated_and_record_the_illusion()
    test_sweep_respects_the_ceiling()
    test_ladder_json_is_the_raw_kalshi_view()
    await test_grading_is_one_way()
    test_precision_classification()
    test_cadence_detection()
    test_feature_parsing_skips_unusable_rows()
    await test_shadow_source_records_but_cannot_trade()
    await test_shadow_source_journals_observations()
    await test_gate_decision_names_the_blocking_gate()
    print("\n" + ("ALL PASS" if not failures else f"{len(failures)} FAILED: {failures}"))
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
