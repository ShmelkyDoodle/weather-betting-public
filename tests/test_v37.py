"""#A Liquidity timing: when does the sure side vanish relative to the lock-in? (offline)

WHY THIS ANALYSIS EXISTS. The first ten live detections all showed ZERO sure-side
ask depth at T0 against a control median of 2,443 contracts. That is compatible
with two opposite worlds:

  * Strategy 1 is too LATE   -- liquidity was there and left before we looked
  * Strategy 1 is INVALID    -- the sure side is simply never quoted

The first is fixable with a faster observation; the second is not fixable at all.
These tests pin the machinery that separates them, and in particular the two
places it would be easiest to fool ourselves:

  1. **Never infer across a gap.** An offset with no snapshot in tolerance must
     stay unmatched. Interpolating a book across a ten-minute control gap would
     manufacture precisely the liquidity we are trying to detect the absence of.
  2. **Never let one offset steal another's snapshot.** Tolerance is half the
     distance to the nearest neighbouring offset, so a snapshot belongs to
     exactly one slot or to none.
"""
import asyncio, json, os, sys, tempfile
from datetime import datetime, timedelta, timezone
import os as _osp; sys.path.insert(0, _osp.path.dirname(_osp.path.dirname(_osp.path.abspath(__file__))))

from weather_arb.db import Database
from weather_arb.liquidity_analysis import (
    ALWAYS_EMPTY, ALWAYS_THIN, LIQUID_BEFORE_LOCKIN, LIQUID_UNTIL_LOCKIN,
    LIQUIDITY_DISAPPEARS_BEFORE_LOCKIN, OFFSETS_MINUTES,
    UNKNOWN_INSUFFICIENT_SNAPSHOTS, analyse_liquidity_timing, book_from_ladder,
    classify, decay_curve, reconstruct_event, slot_metrics,
    slot_tolerances_seconds,
)
from weather_arb.models import Side

failures = []
def check(n, c):
    print(("PASS" if c else "FAIL"), "-", n)
    if not c: failures.append(n)

T0 = datetime(2026, 8, 26, 18, 0, 0, tzinfo=timezone.utc)
TICKER = "KXHIGHDEN-26AUG26-B90.5"


def ladder(yes_bids=(), no_bids=()):
    return json.dumps({"yes_bids": [list(x) for x in yes_bids],
                       "no_bids": [list(x) for x in no_bids]})


def no_side_depth(size, price_cents=95):
    """A book giving the NO side `size` contracts offered at `price_cents`.

    NO asks derive from YES bids (no_ask = 100 - yes_bid), so a YES bid at 5c is
    a NO offer at 95c.
    """
    return ladder(yes_bids=((100 - price_cents, size),), no_bids=((30, 500),))


def snap(minutes_from_t0, ladder_json, sid=1, reason="armed", ceiling=98):
    return {"id": sid, "reason": reason, "ts": T0 + timedelta(minutes=minutes_from_t0),
            "ceiling": ceiling, "ladder": ladder_json,
            "market_status": "active", "gate_outcome": None}


EVENT = {"id": 1, "ticker": TICKER, "icao": "KDEN", "climate_date": "2026-08-26",
         "source": "metar", "side": "no", "margin_f": 2.0, "excess_f": 1.5,
         "observed_tmax_f": 93.0, "call_correct": None,
         "detected_at_utc": T0.isoformat()}


# --------------------------------------------------------------------------- #
# 1. Slot geometry
# --------------------------------------------------------------------------- #
def test_tolerances_cannot_overlap():
    tol = slot_tolerances_seconds()
    ordered = sorted(OFFSETS_MINUTES)
    ok = True
    for a, b in zip(ordered, ordered[1:]):
        gap_s = (b - a) * 60.0
        if tol[a] + tol[b] > gap_s + 1e-9:
            ok = False
    check("adjacent slots can never both reach the same instant", ok)
    check("the tightest slots get 30s (1-minute grid)", tol[0] == 30.0 and tol[-1] == 30.0)
    check("an isolated slot is capped, not unbounded", tol[-30] == 300.0)


def test_no_slot_steals_a_neighbours_snapshot():
    """A snapshot at T-3m sits between two slots and belongs to NEITHER."""
    snaps = [snap(-3.0, no_side_depth(500))]
    out = reconstruct_event(EVENT, snaps)
    check("T-5 does not reach a snapshot 2 minutes away",
          out["slots"]["-5"]["matched"] is False)
    check("T-2 does not reach a snapshot 1 minute away",
          out["slots"]["-2"]["matched"] is False)
    check("and nothing is invented in between",
          all(not out["slots"][str(o)]["matched"] for o in OFFSETS_MINUTES))


def test_nearest_within_tolerance_wins_and_error_is_recorded():
    snaps = [snap(-0.4, no_side_depth(500), sid=7),
             snap(-0.1, no_side_depth(600), sid=9)]
    out = reconstruct_event(EVENT, snaps)
    z = out["slots"]["0"]
    check("the closest snapshot is chosen", z["matched"] and z["snapshot_id"] == 9)
    check("the match error is recorded, not hidden",
          abs(z["match_error_s"] - 6.0) < 0.5)


def test_missing_data_is_never_inferred():
    """The single most important property: a gap stays a gap."""
    snaps = [snap(-30, no_side_depth(5000)), snap(0, no_side_depth(0))]
    out = reconstruct_event(EVENT, snaps)
    for off in (-15, -10, -5, -2, -1):
        s = out["slots"][str(off)]
        check(f"T{off}m stays unmatched rather than interpolated",
              s["matched"] is False and s["match_error_s"] is None
              and "sure_side_sweepable_to_ceiling" not in s)


# --------------------------------------------------------------------------- #
# 2. Book reconstruction and metrics
# --------------------------------------------------------------------------- #
def test_book_reconstruction_round_trip():
    b = book_from_ladder(TICKER, ladder(yes_bids=((5, 100), (2, 40)),
                                        no_bids=((30, 500),)), T0)
    check("yes bids are restored, best first",
          [l.price_cents for l in b.yes_bids] == [5, 2])
    check("NO asks derive correctly (100 - yes_bid)",
          [l.price_cents for l in b.no_asks] == [95, 98])
    check("a malformed ladder yields an empty book, not a crash",
          book_from_ladder(TICKER, "{not json", T0).yes_bids == ())
    check("an absent ladder yields an empty book",
          book_from_ladder(TICKER, "", T0).no_bids == ())


def test_slot_metrics_are_measured_from_the_buyers_side():
    b = book_from_ladder(TICKER, no_side_depth(140, price_cents=95), T0)
    m = slot_metrics(b, "no", 98)
    check("sure-side best ask is the NO ask", m["sure_side_best_ask_cents"] == 95)
    check("sure-side ask size is carried", m["sure_side_ask_size"] == 140)
    check("the opposite-side bid is the YES bid that creates it",
          m["opposite_side_bid_cents"] == 5 and m["opposite_side_bid_size"] == 140)
    check("sweepable-to-ceiling counts only levels at/below 98c",
          m["sure_side_sweepable_to_ceiling"] == 140)
    check("VWAPs are recomputed from the stored ladder",
          m["vwap_cents"]["100"] == 95.0 and m["vwap_cents"]["1"] == 95.0)
    check("a size larger than the book reports the partial fill",
          m["fillable"]["100"] == 100 and m["vwap_cents"]["100"] is not None)
    check("the book is not flagged empty when it has an offer",
          m["sure_side_empty"] is False)

    empty = book_from_ladder(TICKER, ladder(no_bids=((30, 500),)), T0)
    me = slot_metrics(empty, "no", 98)
    check("no YES bids means the SURE side is empty even though the book is not",
          me["sure_side_empty"] is True and me["book_completely_empty"] is False)
    check("and an empty sure side has no ask at any price",
          me["sure_side_best_ask_cents"] is None
          and me["sure_side_sweepable_to_ceiling"] == 0)


def test_ceiling_truncation_is_applied():
    """A 99c offer is above the lock-in ceiling and must not count as depth."""
    b = book_from_ladder(TICKER, ladder(yes_bids=((1, 900),)), T0)   # NO ask at 99c
    m = slot_metrics(b, "no", 98)
    check("a 99c sure-side offer is not sweepable under a 98c ceiling",
          m["sure_side_sweepable_to_ceiling"] == 0)
    check("but it is still reported as the best ask, so the state is visible",
          m["sure_side_best_ask_cents"] == 99)
    check("total depth is reported separately from sweepable depth",
          m["sure_side_total_depth"] == 900)


# --------------------------------------------------------------------------- #
# 3. Classification -- all six, mutually exclusive
# --------------------------------------------------------------------------- #
def _slots(**by_offset):
    """Build a slot dict; any offset not named is unmatched."""
    out = {}
    for off in OFFSETS_MINUTES:
        if str(off) in by_offset:
            depth = by_offset[str(off)]
            out[str(off)] = {"matched": True,
                             "sure_side_sweepable_to_ceiling": depth,
                             "sure_side_empty": depth == 0}
        else:
            out[str(off)] = {"matched": False, "match_error_s": None}
    return out


def test_all_six_classifications():
    cases = [
        ("survives the lock-in",
         _slots(**{"-5": 400, "-1": 300, "0": 250}), LIQUID_UNTIL_LOCKIN),
        ("gone during the final approach",
         _slots(**{"-5": 400, "-1": 300, "0": 0}),
         LIQUIDITY_DISAPPEARS_BEFORE_LOCKIN),
        ("gone well before the approach",
         _slots(**{"-30": 400, "-10": 300, "-5": 0, "-1": 0, "0": 0}),
         LIQUID_BEFORE_LOCKIN),
        ("never more than a handful",
         _slots(**{"-10": 3, "-5": 2, "-1": 4, "0": 1}), ALWAYS_THIN),
        ("never quoted at all",
         _slots(**{"-10": 0, "-5": 0, "-1": 0, "0": 0}), ALWAYS_EMPTY),
        ("no T0 snapshot",
         _slots(**{"-10": 400, "-5": 400}), UNKNOWN_INSUFFICIENT_SNAPSHOTS),
        ("no pre-lock-in snapshot",
         _slots(**{"0": 400, "1": 400}), UNKNOWN_INSUFFICIENT_SNAPSHOTS),
    ]
    seen = set()
    for name, slots, expected in cases:
        got = classify(slots, meaningful_size=10)
        check(f"{name} -> {expected}", got == expected)
        seen.add(got)
    check("all six labels are reachable", len(seen) == 6)


def test_meaningful_threshold_is_honoured():
    slots = _slots(**{"-5": 8, "-1": 8, "0": 8})
    check("8 contracts is THIN at a threshold of 10",
          classify(slots, meaningful_size=10) == ALWAYS_THIN)
    check("the same book is LIQUID at a threshold of 5",
          classify(slots, meaningful_size=5) == LIQUID_UNTIL_LOCKIN)
    check("post-lock-in slots never affect the classification",
          classify(_slots(**{"-1": 0, "0": 0, "5": 9999}), meaningful_size=10)
          == ALWAYS_EMPTY)


# --------------------------------------------------------------------------- #
# 4. Last executable moment
# --------------------------------------------------------------------------- #
def test_last_executable_moment_per_threshold():
    snaps = [
        snap(-20, no_side_depth(500)),   # >=100 available
        snap(-10, no_side_depth(40)),    # >=25 but not >=50
        snap(-4, no_side_depth(3)),      # >=1 only
        snap(-1, no_side_depth(0)),      # gone
        snap(0, no_side_depth(0)),
    ]
    out = reconstruct_event(EVENT, snaps)
    le = out["last_executable"]
    check("the last moment ANY size was available is found",
          abs(le["any_positive"]["minutes_before_lockin"] - 4.0) < 0.01)
    bt = le["by_threshold"]
    check(">=1 last available 4 minutes before",
          abs(bt["1"]["minutes_before_lockin"] - 4.0) < 0.01)
    check(">=25 last available 10 minutes before",
          abs(bt["25"]["minutes_before_lockin"] - 10.0) < 0.01)
    check(">=100 last available 20 minutes before",
          abs(bt["100"]["minutes_before_lockin"] - 20.0) < 0.01)
    check("it scans every snapshot, not just the slot grid",
          le["pre_snapshots"] == 4)


def test_never_available_reports_none_not_zero():
    snaps = [snap(-5, no_side_depth(0)), snap(0, no_side_depth(0))]
    le = reconstruct_event(EVENT, snaps)["last_executable"]
    check("a sure side that was never quoted reports None, not 0 minutes",
          le["any_positive"] is None and le["by_threshold"]["1"] is None)


def test_seconds_since_last_non_empty():
    snaps = [snap(-10, no_side_depth(500)), snap(0, no_side_depth(0))]
    out = reconstruct_event(EVENT, snaps)
    check("T0 records how long the sure side has been unquoted",
          abs(out["slots"]["0"]["seconds_since_last_non_empty"] - 600.0) < 1.0)
    check("a slot with its own quote reports zero",
          out["slots"]["-10"]["seconds_since_last_non_empty"] == 0.0)


# --------------------------------------------------------------------------- #
# 5. Decay curve reports COVERAGE, so a hole is not read as an absence
# --------------------------------------------------------------------------- #
def test_decay_curve_separates_a_hole_from_an_absence():
    e_full = reconstruct_event(EVENT, [snap(-5, no_side_depth(200)),
                                       snap(0, no_side_depth(0))])
    e_hole = reconstruct_event(EVENT, [snap(0, no_side_depth(0))])
    curve = decay_curve([e_full, e_hole])
    check("an offset only one event could match reports 50% coverage",
          abs(curve["-5"]["coverage_pct"] - 50.0) < 1e-9)
    check("and its median reflects only the matched event",
          curve["-5"]["sweepable_depth"]["median"] == 200)
    check("a fully covered offset reports 100%",
          abs(curve["0"]["coverage_pct"] - 100.0) < 1e-9)
    check("an offset nobody matched is reported as zero coverage, not zero depth",
          curve["-30"]["matched"] == 0 and curve["-30"]["coverage_pct"] == 0.0)
    check("empty% is measured against matched events only",
          curve["0"]["sure_side_empty_pct"] == 100.0)


# --------------------------------------------------------------------------- #
# 6. End to end against a real ledger
# --------------------------------------------------------------------------- #
async def test_end_to_end():
    with tempfile.TemporaryDirectory() as d:
        async with Database(os.path.join(d, "t.sqlite3")) as db:
            base = {"series": "KXHIGHDEN", "icao": "KDEN",
                    "climate_date": "2026-08-26", "side": "no", "action": "buy",
                    "strike_type": "between", "floor_strike_f": 90.0,
                    "cap_strike_f": 91.0, "boundary_f": 91.0,
                    "certainty_bound_f": 91.5, "observed_tmax_f": 93.0,
                    "lockin_margin_f": 0.5, "margin_f": 2.0, "excess_f": 1.5,
                    "observation_at_utc": None, "ingest_at_utc": None,
                    "observation_age_s": None}
            # A METAR detection at T0, and a 5-minute shadow detection 40 min earlier.
            eid = await db.record_lockin_event(
                {**base, "ticker": TICKER, "source": "metar",
                 "detected_at_utc": T0.isoformat()})
            await db.record_lockin_event(
                {**base, "ticker": TICKER, "source": "nws5min",
                 "detected_at_utc": (T0 - timedelta(minutes=40)).isoformat()})
            # Deep book at the 5-minute signal, empty by the METAR signal.
            for mins, lad in ((-40, no_side_depth(800)), (-5, no_side_depth(0)),
                              (-1, no_side_depth(0)), (0, no_side_depth(0))):
                await db.record_book_snapshot({
                    "ticker": TICKER, "event_id": eid, "reason": "armed",
                    "offset_seconds": None,
                    "requested_at_utc": (T0 + timedelta(minutes=mins)).isoformat(),
                    "received_at_utc": (T0 + timedelta(minutes=mins)).isoformat(),
                    "round_trip_ms": 40.0, "gate_outcome": None,
                    "best_yes_bid_cents": None, "best_yes_bid_size": None,
                    "best_yes_ask_cents": None, "best_yes_ask_size": None,
                    "best_no_bid_cents": None, "best_no_bid_size": None,
                    "best_no_ask_cents": None, "best_no_ask_size": None,
                    "spread_cents": None, "yes_depth_total": 0,
                    "no_depth_total": 0, "yes_depth_to_ceiling": 0,
                    "no_depth_to_ceiling": 0, "ceiling_cents": 98,
                    "n_yes_levels": 0, "n_no_levels": 0,
                    "market_status": "active", "ladder_json": lad})
            import sqlite3
            conn = sqlite3.connect(os.path.join(d, "t.sqlite3"))
            conn.row_factory = sqlite3.Row
            out = analyse_liquidity_timing(conn, "2026-01-01T00:00:00+00:00")
            conn.close()

    check("both detections are analysed", out["events"] == 2)
    wl = out["weather_lead"]
    check("the 5-minute and METAR detections are paired", wl["paired_events"] == 1)
    if wl["pairs"]:
        p0 = wl["pairs"][0]
        check("the DETECTION lead is measured at 40 minutes",
              abs(p0["nws_lockin_lead_vs_metar_seconds_detection"] - 2400.0) < 1.0)
        check("with no comparable intraday history it is not a VALID lead",
              p0["lead_measurement"] == "insufficient_history")
        check("so the lead statistic stays empty rather than reporting 40 min",
              wl["nws_lockin_lead_vs_metar_seconds_receipt"]["n"] == 0)
    check("the METAR event classifies as empty at the lock-in",
          out["classification_counts"].get(ALWAYS_EMPTY, 0) >= 1)
    le = out["last_executable"]["by_threshold"]["100"]
    check("100 contracts were last available 40 minutes before the METAR signal",
          le["events_with_any"] >= 1)


def test_history_gate_rejects_incomparable_intraday_histories():
    """A lead is only claimed when both sources watched the same day comparably."""
    from weather_arb.liquidity_analysis import validate_lead_history
    pure = {"uncontaminated": True, "nws_tenths_rows_to_crossing": 0,
            "nws_rows_to_crossing": 100,
            "running_max_from_five_minute_product": True}
    good = {"metar_first_obs_utc": "2026-08-26T06:00:00+00:00",
            "metar_last_obs_utc": "2026-08-26T18:00:00+00:00",
            "metar_observations": 12,
            "nws_first_obs_utc": "2026-08-26T05:00:00+00:00",
            "nws_last_obs_utc": "2026-08-26T18:00:00+00:00",
            "nws_observations": 130}
    at = datetime(2026, 8, 26, 15, 0, tzinfo=timezone.utc)
    check("a complete comparable day is a valid lead measurement",
          validate_lead_history(good, at, pure)["valid"] is True)

    # The restart case: the 5-minute history starts hours after the METAR's.
    late = {**good, "nws_first_obs_utc": "2026-08-26T13:00:00+00:00",
            "nws_observations": 30}
    v = validate_lead_history(late, at, pure)
    check("a 5-minute history that starts LATE is insufficient_history, not a lead",
          v["valid"] is False and v["starts_comparable"] is False)

    # Sparse coverage: same start, but nowhere near 5-minute density.
    sparse = {**good, "nws_observations": 20}
    v2 = validate_lead_history(sparse, at, pure)
    check("a history too sparse to be the 5-minute product is rejected",
          v2["valid"] is False and v2["cadence_ok"] is False)

    # Observing only after both fired.
    v3 = validate_lead_history(
        {**good, "nws_first_obs_utc": "2026-08-26T16:00:00+00:00"}, at, pure)
    check("a source that starts after the crossing cannot have led or lagged",
          v3["valid"] is False and v3["observed_before_either"] is False)

    # Contaminated stream.
    dirty = {**pure, "uncontaminated": False,
             "running_max_from_five_minute_product": False}
    v4 = validate_lead_history(good, at, dirty)
    check("a contaminated 5-minute stream is not a comparable history",
          v4["valid"] is False and v4["uncontaminated"] is False)


def test_stream_purity_detects_leaked_metar_rows():
    """api.weather.gov serves the :53 METAR with no rawMessage; it leaks in.

    The count is small (4-6 rows/station-day) but the running max is
    path-dependent, so one leaked METAR value can set it outright -- which turns
    the lead into METAR-versus-METAR.
    """
    import sqlite3
    from weather_arb.liquidity_analysis import nws_stream_purity
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE source_observations (icao TEXT, climate_date TEXT, "
                 "source TEXT, observed_at_utc TEXT, temp_f REAL, precision_f REAL)")
    rows = [("KMIA", "2026-08-26", "nws5min", "2026-08-26T13:00:00+00:00", 87.8, 1.8),
            ("KMIA", "2026-08-26", "nws5min", "2026-08-26T13:53:00+00:00", 89.96, 0.18)]
    conn.executemany("INSERT INTO source_observations VALUES (?,?,?,?,?,?)", rows)
    up_to = datetime(2026, 8, 26, 14, 0, tzinfo=timezone.utc)
    r = nws_stream_purity(conn, "KMIA", "2026-08-26", up_to)
    check("leaked tenths-degC rows are counted", r["nws_tenths_rows_to_crossing"] == 1)
    check("a running max set by a leaked METAR row is flagged contaminated",
          r["running_max_from_five_minute_product"] is False
          and r["uncontaminated"] is False)

    # Once a genuine 5-minute observation exceeds it, the max is clean again.
    conn.execute("INSERT INTO source_observations VALUES (?,?,?,?,?,?)",
                 ("KMIA", "2026-08-26", "nws5min",
                  "2026-08-26T13:55:00+00:00", 91.4, 1.8))
    r2 = nws_stream_purity(conn, "KMIA", "2026-08-26", up_to)
    check("a max set by a real 5-minute observation is uncontaminated",
          r2["uncontaminated"] is True)
    conn.close()


def test_crossing_times_separate_observation_from_receipt():
    """The decomposition: observation lead minus delivery delay."""
    import sqlite3
    from weather_arb.liquidity_analysis import crossing_observation_time
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE station_logs (icao TEXT, climate_date TEXT, "
                 "observed_at_utc TEXT, inserted_at_utc TEXT, temp_f REAL, accepted INT)")
    conn.execute("CREATE TABLE source_observations (icao TEXT, climate_date TEXT, "
                 "source TEXT, observed_at_utc TEXT, received_at_utc TEXT, "
                 "temp_f REAL, precision_f REAL)")
    # METAR: observed 13:53, held 4 minutes later.
    conn.execute("INSERT INTO station_logs VALUES "
                 "('KMIA','2026-08-26','2026-08-26T13:53:00+00:00',"
                 "'2026-08-26T13:57:00+00:00',89.96,1)")
    # 5-minute: observed 13:00 (EARLIER), but held 20 minutes later.
    conn.executemany("INSERT INTO source_observations VALUES (?,?,?,?,?,?,?)", [
        ("KMIA", "2026-08-26", "nws5min", "2026-08-26T13:00:00+00:00",
         "2026-08-26T13:20:00+00:00", 87.8, 1.8),
        # A leaked METAR row that must NOT be used as the 5-minute crossing.
        ("KMIA", "2026-08-26", "nws5min", "2026-08-26T12:53:00+00:00",
         "2026-08-26T13:20:00+00:00", 89.96, 0.18),
    ])
    m_obs, m_got = crossing_observation_time(conn, "KMIA", "2026-08-26", 86.5, "metar")
    n_obs, n_got = crossing_observation_time(conn, "KMIA", "2026-08-26", 86.5, "nws5min")
    check("the 5-minute source OBSERVED the crossing 53 minutes earlier",
          (m_obs - n_obs).total_seconds() == 53 * 60)
    check("but we HELD it only 37 minutes earlier -- delivery ate 16 minutes",
          (m_got - n_got).total_seconds() == 37 * 60)
    check("the leaked tenths-degC row is excluded from the 5-minute crossing",
          n_obs.isoformat().startswith("2026-08-26T13:00"))
    conn.close()


def test_negative_lead_is_flagged_not_filtered():
    """A negative lead is physically backwards and must stay visible.

    The 5-minute product samples the same air more often, so it cannot genuinely
    prove a crossing AFTER the hourly METAR. A negative lead means the 5-minute
    running max started behind -- it lives in memory and backfills ~2 h, while
    daily_tmax is DB-backed and complete, so a mid-day restart handicaps it for
    the rest of that climate day. Dropping such pairs would delete the evidence
    that a restart happened, so they are counted and reported separately.
    """
    import sqlite3
    from weather_arb.liquidity_analysis import weather_lead
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row

    def ev(source, minutes):
        return {"ticker": TICKER, "climate_date": "2026-08-26", "source": source,
                "detected_at_utc": (T0 + timedelta(minutes=minutes)).isoformat(),
                "slots": {"0": {"matched": True,
                                "sure_side_sweepable_to_ceiling": 0,
                                "sure_side_best_ask_cents": None}}}

    # 5-minute fired AFTER the METAR: backwards.
    conn.execute("CREATE TABLE station_logs (icao TEXT, climate_date TEXT, "
                 "observed_at_utc TEXT, inserted_at_utc TEXT, temp_f REAL, accepted INT)")
    conn.execute("CREATE TABLE source_observations (icao TEXT, climate_date TEXT, "
                 "source TEXT, observed_at_utc TEXT, received_at_utc TEXT, "
                 "temp_f REAL, precision_f REAL)")
    out = weather_lead(conn, "2026-01-01", [ev("metar", 0), ev("nws5min", 40)])
    check("a backwards pair is still counted as paired", out["paired_events"] == 1)
    check("with no intraday history it is insufficient_history, NOT a negative lead",
          out["insufficient_history"] == 1 and out["valid_lead_measurements"] == 0)
    check("and it is excluded from the lead statistic entirely",
          out["nws_lockin_lead_vs_metar_seconds_receipt"]["n"] == 0)
    check("but the pair itself is preserved for inspection",
          len(out["insufficient_history_pairs"]) == 1)
    conn.close()


def test_dropped_slots_are_not_reported_as_late():
    """A slot the API never exposed must NOT be folded into a latency statistic.

    THE ERROR THIS PREVENTS, made and retracted on 2026-08-26. A 30 s probe
    recorded only features[0] of each response and saw the newest observation go
    17:05 -> 17:10 -> 17:20, which was read as "the 17:15 slot never appeared".
    It had appeared -- KPHL and KMDW received 17:10 and 17:15 in the SAME
    response, at an identical received_at. Roughly 38% of productive responses
    carry two or more slots, so inspecting only the newest one systematically
    mistakes batch delivery for a drop.

    The authority is the stored data, not the newest-observation sequence:
    UNIQUE(icao, source, observed_at_utc) means every slot the API exposed while
    we polled has a row.
    """
    import sqlite3
    from weather_arb.liquidity_analysis import dropped_observations
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE source_observations (icao TEXT, climate_date TEXT, "
                 "source TEXT, observed_at_utc TEXT, received_at_utc TEXT, "
                 "temp_f REAL, precision_f REAL)")

    def add(minute, recv_minute, precision=1.8):
        conn.execute("INSERT INTO source_observations VALUES (?,?,?,?,?,?,?)",
                     ("KPHL", "2026-08-26", "nws5min",
                      f"2026-08-26T17:{minute:02d}:00+00:00",
                      f"2026-08-26T17:{recv_minute:02d}:00+00:00", 82.4, precision))

    # 17:00, 17:05 and 17:10 delivered; 17:10 and 17:15 arrive TOGETHER at 17:32.
    add(0, 20); add(5, 25); add(10, 32); add(15, 32); add(20, 40)
    r = dropped_observations(conn, "2026-01-01T00:00:00+00:00")
    check("a batch-delivered slot is PRESENT, not dropped",
          r["dropped_never_exposed"] == 0)
    check("and every expected slot is accounted for",
          r["expected_slots"] == 5 and r["present"] == 5)

    # Now a genuine drop: 17:25 missing while we were demonstrably polling.
    add(30, 45)
    r2 = dropped_observations(conn, "2026-01-01T00:00:00+00:00")
    check("a slot with no row at all IS reported as dropped",
          r2["dropped_never_exposed"] == 1)
    check("and it is flagged as never subsequently exposed",
          r2["affected"][0]["ever_subsequently_exposed"] is False)
    check("the dropped slot is identified by time",
          r2["affected"][0]["dropped_slots_utc"][0].startswith("2026-08-26T17:25"))
    check("dropped slots are never added to the latency sample",
          r2["present"] + r2["dropped_never_exposed"]
          + r2["downtime_slots"] == r2["expected_slots"])
    conn.close()


def test_downtime_is_separated_from_a_drop():
    """Not polling is not the same as the API not serving.

    The boundary is deliberately conservative: a slot within DOWNTIME_WINDOW_S of
    an actual receipt counts as a DROP, because we can prove we were polling near
    it. Only the interior of an unpolled stretch becomes downtime. That errs
    toward reporting drops rather than hiding them behind "we weren't looking".
    """
    import sqlite3
    from weather_arb.liquidity_analysis import dropped_observations
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE source_observations (icao TEXT, climate_date TEXT, "
                 "source TEXT, observed_at_utc TEXT, received_at_utc TEXT, "
                 "temp_f REAL, precision_f REAL)")
    # Two observations 3 hours apart: everything between is unobserved, and no
    # receipts exist in the gap, so it is downtime rather than dropped slots.
    for obs, recv in (("06:00", "06:20"), ("09:00", "09:20")):
        conn.execute("INSERT INTO source_observations VALUES (?,?,?,?,?,?,?)",
                     ("KPHL", "2026-08-26", "nws5min",
                      f"2026-08-26T{obs}:00+00:00",
                      f"2026-08-26T{recv}:00+00:00", 82.4, 1.8))
    r = dropped_observations(conn, "2026-01-01T00:00:00+00:00")
    check("the interior of an unpolled stretch is classified downtime",
          r["downtime_slots"] > 0)
    check("downtime is never counted as a drop",
          r["downtime_slots"] + r["dropped_never_exposed"] + r["present"]
          == r["expected_slots"])
    # Slots ADJACENT to a receipt stay classified as drops, and correctly so: we
    # demonstrably were polling then, so the API's failure to serve them is a
    # drop rather than a blind spot. Only the interior of the gap is downtime.
    check("slots adjacent to a receipt remain drops, not downtime",
          r["dropped_never_exposed"] > 0)
    conn.close()


def test_leaked_metar_rows_are_off_grid_and_excluded():
    """Off-grid tenths-degC rows must not count toward 5-minute coverage."""
    import sqlite3
    from weather_arb.liquidity_analysis import dropped_observations
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE source_observations (icao TEXT, climate_date TEXT, "
                 "source TEXT, observed_at_utc TEXT, received_at_utc TEXT, "
                 "temp_f REAL, precision_f REAL)")
    rows = [("KPHL", "2026-08-26", "nws5min", "2026-08-26T17:00:00+00:00",
             "2026-08-26T17:20:00+00:00", 82.4, 1.8),
            ("KPHL", "2026-08-26", "nws5min", "2026-08-26T17:05:00+00:00",
             "2026-08-26T17:25:00+00:00", 82.4, 1.8),
            # the :53 leak -- off the 5-minute grid, tenths precision
            ("KPHL", "2026-08-26", "nws5min", "2026-08-26T16:53:00+00:00",
             "2026-08-26T17:20:00+00:00", 82.94, 0.18)]
    conn.executemany("INSERT INTO source_observations VALUES (?,?,?,?,?,?,?)", rows)
    r = dropped_observations(conn, "2026-01-01T00:00:00+00:00")
    check("the off-grid leaked row does not extend the expected grid",
          r["expected_slots"] == 2 and r["dropped_never_exposed"] == 0)
    conn.close()


def test_availability_disclaimer_travels_with_the_statistic():
    """The framing is part of the measurement, so it is asserted like one."""
    from weather_arb import liquidity_analysis as la
    d = la.AVAILABILITY_DISCLAIMER.lower()
    check("the disclaimer denies it is publication latency",
          "not a measurement of nws publication latency" in d)
    check("it names the shared, uncontrolled cache phase",
          "shared and uncontrolled" in d)
    check("it warns against reading it as a feed characteristic",
          "not be interpreted as a stable feed characteristic" in d)
    check("cache-gating is recorded as measured, not asserted",
          la.PROBE_ADVANCES_CACHE_GATED == la.PROBE_ADVANCES_OBSERVED == 10)
    check("the probe constants carry provenance",
          la.PROBE_DATE == "2026-08-26" and la.PROBE_RESOLUTION_S == 30)
    check("the cache TTL range is the measured one",
          la.CACHE_TTL_OBSERVED_S == (245, 288))


async def main():
    test_tolerances_cannot_overlap()
    test_no_slot_steals_a_neighbours_snapshot()
    test_nearest_within_tolerance_wins_and_error_is_recorded()
    test_missing_data_is_never_inferred()
    test_book_reconstruction_round_trip()
    test_slot_metrics_are_measured_from_the_buyers_side()
    test_ceiling_truncation_is_applied()
    test_all_six_classifications()
    test_meaningful_threshold_is_honoured()
    test_last_executable_moment_per_threshold()
    test_never_available_reports_none_not_zero()
    test_seconds_since_last_non_empty()
    test_decay_curve_separates_a_hole_from_an_absence()
    test_history_gate_rejects_incomparable_intraday_histories()
    test_stream_purity_detects_leaked_metar_rows()
    test_crossing_times_separate_observation_from_receipt()
    test_negative_lead_is_flagged_not_filtered()
    test_dropped_slots_are_not_reported_as_late()
    test_downtime_is_separated_from_a_drop()
    test_leaked_metar_rows_are_off_grid_and_excluded()
    test_availability_disclaimer_travels_with_the_statistic()
    await test_end_to_end()
    print("\n" + ("ALL PASS" if not failures else f"{len(failures)} FAILED: {failures}"))
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
