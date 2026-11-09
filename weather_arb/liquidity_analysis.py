"""When does the sure side's liquidity disappear, relative to the lock-in?

THE QUESTION. The first ten live detections all showed **zero** sure-side ask
depth at T0, against a control median of 2,443 contracts. So "priced at 100c" and
"no bid at all" are not the same thing, and candlestick data cannot tell them
apart -- the first is an expensive trade, the second is no trade. That leaves a
question the existing panels cannot answer:

  A. liquidity vanishes BEFORE the lock-in       -> Strategy 1 is too LATE
  B. liquidity vanishes exactly AT the lock-in   -> Strategy 1 is too late by a hair
  C. liquidity survives the lock-in              -> Strategy 1 is reachable
  D. liquidity was never there                   -> Strategy 1 is INVALID

A/B say the signal is late; D says the premise is wrong. Those imply completely
different next steps, so they must be separated by measurement.

This module is READ-ONLY. It adds no collection, changes no threshold, and does
not touch the definition of a deterministic lock-in. It reconstructs the book at
fixed offsets around each recorded detection from snapshots that already exist.

TWO RULES IT WILL NOT BEND
--------------------------
**Nothing is inferred.** An offset with no snapshot inside tolerance is ``None``,
and an event without a T0 or without any pre-lock-in slot is
``UNKNOWN_INSUFFICIENT_SNAPSHOTS``. Interpolating a book across a ten-minute gap
would manufacture exactly the liquidity this is trying to detect the absence of.

**No slot may steal another slot's snapshot.** Tolerance for each offset is half
the distance to its nearest neighbouring offset (capped at
``MAX_TOLERANCE_S``), so the assignment is unambiguous by construction. Every
matched slot also records ``match_error_s``, so a "match" 280 s from its nominal
offset is visible as such rather than read as a measurement at that offset.

⚠ KNOWN COVERAGE LIMIT, stated up front. Pre-lock-in snapshots come from the
control stream (600 s) and the armed stream (60 s, and only once a market is
within ``arm_threshold_f`` of its certainty bound). So T-1/-2/-5 are well covered
once a market is armed, while T-10/-15/-30 depend on the sparse control cadence
and will frequently be unmatched. ``coverage`` reports the match rate per offset
so that gap is never mistaken for an absence of liquidity.
"""

from __future__ import annotations

import json
import math
import sqlite3
import statistics
from datetime import datetime, timedelta
from typing import Any, Optional

from .models import OrderBook, PriceLevel, Side

# Offsets in MINUTES relative to the detection instant (T0).
OFFSETS_MINUTES: tuple[float, ...] = (-30, -15, -10, -5, -2, -1, 0, 1, 2, 5, 10)

# Sizes at which the sweep is repriced from the stored ladder.
VWAP_SIZES: tuple[int, ...] = (1, 10, 25, 50, 100)

# Depth thresholds for "the last moment N contracts were available".
AVAILABILITY_THRESHOLDS: tuple[int, ...] = (1, 10, 25, 50, 100)

# A slot never reaches further than this, however isolated its offset.
MAX_TOLERANCE_S = 300.0

# Contracts at or below the ceiling for a book to count as MEANINGFUL rather than
# merely non-empty. Deliberately modest: the question is whether anything
# executable existed, not whether a full clip did. Reported alongside every
# classification so it can be varied without re-reading the prose.
DEFAULT_MEANINGFUL_SIZE = 10

# How far back to look for snapshots. Wider than the widest offset so the
# "last executable moment" scan has room beyond the slot grid.
LOOKBACK_MINUTES = 45.0
LOOKAHEAD_MINUTES = 15.0

# Classification labels.
LIQUID_BEFORE_LOCKIN = "LIQUID_BEFORE_LOCKIN"
LIQUID_UNTIL_LOCKIN = "LIQUID_UNTIL_LOCKIN"
LIQUIDITY_DISAPPEARS_BEFORE_LOCKIN = "LIQUIDITY_DISAPPEARS_BEFORE_LOCKIN"
ALWAYS_THIN = "ALWAYS_THIN"
ALWAYS_EMPTY = "ALWAYS_EMPTY"
UNKNOWN_INSUFFICIENT_SNAPSHOTS = "UNKNOWN_INSUFFICIENT_SNAPSHOTS"

# Which of the four hypotheses each label supports. Printed with the counts so
# the classification cannot be read as more than it is.
LABEL_TO_HYPOTHESIS = {
    LIQUID_BEFORE_LOCKIN: "A: gone before lock-in (Strategy 1 too late)",
    LIQUIDITY_DISAPPEARS_BEFORE_LOCKIN: "B: gone at the approach (too late by a hair)",
    LIQUID_UNTIL_LOCKIN: "C: survives the lock-in (reachable)",
    ALWAYS_THIN: "D: never meaningfully there",
    ALWAYS_EMPTY: "D: never there at all",
    UNKNOWN_INSUFFICIENT_SNAPSHOTS: "-- not measurable from stored snapshots",
}


# --------------------------------------------------------------------------- #
# Slot geometry
# --------------------------------------------------------------------------- #
def slot_tolerances_seconds(
    offsets: tuple[float, ...] = OFFSETS_MINUTES,
    max_tolerance_s: float = MAX_TOLERANCE_S,
) -> dict[float, float]:
    """Per-offset matching tolerance, in seconds.

    Half the distance to the nearest neighbouring offset, capped. Halving is what
    makes the assignment unambiguous: two adjacent slots can never both reach the
    same instant, so a snapshot belongs to exactly one offset or to none.

    With the default grid that yields, in minutes:
    -30 -> 7.5 (capped to 5), -15 -> 2.5, -10 -> 2.5, -5 -> 1.5, -2/-1/0/+1/+2 ->
    0.5, +5 -> 1.5, +10 -> 2.5.
    """
    ordered = sorted(offsets)
    out: dict[float, float] = {}
    for i, off in enumerate(ordered):
        gaps = []
        if i > 0:
            gaps.append(off - ordered[i - 1])
        if i < len(ordered) - 1:
            gaps.append(ordered[i + 1] - off)
        half = (min(gaps) / 2.0) * 60.0 if gaps else max_tolerance_s
        out[off] = min(half, max_tolerance_s)
    return out


# --------------------------------------------------------------------------- #
# Book reconstruction
# --------------------------------------------------------------------------- #
def book_from_ladder(ticker: str, ladder: str, ts: datetime) -> OrderBook:
    """Rebuild an :class:`OrderBook` from a stored ``ladder_json``.

    This is why the raw ladder is persisted rather than a three-level summary:
    control snapshots carry no ``sweep_metrics`` rows, so every VWAP in this
    module is recomputed here. The reconstruction feeds the SAME
    ``get_l2_sweep_metrics`` the live engine prices with -- re-deriving the sweep
    would be a second implementation free to drift from production.
    """
    try:
        d = json.loads(ladder) if ladder else {}
    except (TypeError, ValueError):
        d = {}

    def levels(rows: Any) -> tuple[PriceLevel, ...]:
        out = []
        for row in rows or []:
            try:
                price, size = int(row[0]), int(row[1])
            except (TypeError, ValueError, IndexError):
                continue
            # Drop non-positive sizes exactly as the live parser does. A level
            # with size 0 is not a quote, and keeping it would make an empty sure
            # side read as "quoted" -- which is the precise distinction this
            # whole module exists to measure.
            if size <= 0:
                continue
            out.append(PriceLevel(price, size))
        return tuple(sorted(out, key=lambda l: l.price_cents, reverse=True))

    return OrderBook(ticker=ticker, timestamp=ts,
                     yes_bids=levels(d.get("yes_bids")),
                     no_bids=levels(d.get("no_bids")))


def sure_side(side: str) -> Side:
    """The side we would BUY: the one the lock-in says is certain."""
    return Side.YES if side == "yes" else Side.NO


def slot_metrics(
    book: OrderBook, side: str, ceiling_cents: int,
    vwap_sizes: tuple[int, ...] = VWAP_SIZES,
) -> dict[str, Any]:
    """Everything measurable about one book, from the buyer's point of view."""
    buy = sure_side(side)
    if buy is Side.YES:
        ask, ladder = book.best_yes_ask, book.yes_asks
        opp_bid = book.best_no_bid
        total_depth = sum(l.size for l in book.yes_asks)
        sweepable = book.buyable_yes_size(ceiling_cents)
    else:
        ask, ladder = book.best_no_ask, book.no_asks
        opp_bid = book.best_yes_bid
        total_depth = sum(l.size for l in book.no_asks)
        sweepable = book.buyable_no_size(ceiling_cents)

    yb, ya = book.best_yes_bid, book.best_yes_ask
    vwaps: dict[str, Optional[float]] = {}
    fills: dict[str, int] = {}
    for size in vwap_sizes:
        m = book.get_l2_sweep_metrics(buy, target_size=size,
                                      max_price_cents=ceiling_cents)
        vwaps[str(size)] = m["vwap_cents"]
        fills[str(size)] = int(m["fillable_size"])

    return {
        "sure_side_best_ask_cents": ask.price_cents if ask else None,
        "sure_side_ask_size": ask.size if ask else None,
        "opposite_side_bid_cents": opp_bid.price_cents if opp_bid else None,
        "opposite_side_bid_size": opp_bid.size if opp_bid else None,
        "sure_side_total_depth": total_depth,
        "sure_side_sweepable_to_ceiling": sweepable,
        "vwap_cents": vwaps,
        "fillable": fills,
        "spread_cents": (ya.price_cents - yb.price_cents) if (ya and yb) else None,
        "sure_side_empty": len(ladder) == 0,
        "book_completely_empty": not book.yes_bids and not book.no_bids,
    }


# --------------------------------------------------------------------------- #
# Data access
# --------------------------------------------------------------------------- #
def _parse(ts: Optional[str]) -> Optional[datetime]:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(str(ts))
    except ValueError:
        return None


def load_event_snapshots(
    conn: sqlite3.Connection, ticker: str, t0: datetime,
    lookback_min: float = LOOKBACK_MINUTES,
    lookahead_min: float = LOOKAHEAD_MINUTES,
) -> list[dict[str, Any]]:
    """Every snapshot for one ticker in the window around T0, time-ordered.

    Deliberately includes EVERY reason (control, armed, signal, offset). The
    question is what the book looked like, not which loop happened to look.
    """
    lo = (t0 - timedelta(minutes=lookback_min)).isoformat()
    hi = (t0 + timedelta(minutes=lookahead_min)).isoformat()
    rows = conn.execute(
        "SELECT id, reason, received_at_utc, ceiling_cents, ladder_json, "
        "       market_status, gate_outcome "
        "FROM book_snapshots WHERE ticker = ? AND received_at_utc >= ? "
        "  AND received_at_utc <= ? ORDER BY received_at_utc",
        (ticker, lo, hi),
    ).fetchall()
    out = []
    for r in rows:
        ts = _parse(r["received_at_utc"])
        if ts is None:
            continue
        out.append({"id": int(r["id"]), "reason": r["reason"], "ts": ts,
                    "ceiling": int(r["ceiling_cents"]),
                    "ladder": r["ladder_json"],
                    "market_status": r["market_status"],
                    "gate_outcome": r["gate_outcome"]})
    return out


# --------------------------------------------------------------------------- #
# Per-event reconstruction
# --------------------------------------------------------------------------- #
def reconstruct_event(
    event: dict[str, Any], snapshots: list[dict[str, Any]], *,
    meaningful_size: int = DEFAULT_MEANINGFUL_SIZE,
    offsets: tuple[float, ...] = OFFSETS_MINUTES,
) -> dict[str, Any]:
    """The book at every offset around one detection, plus its classification."""
    t0 = _parse(event["detected_at_utc"])
    side = str(event["side"])
    tolerances = slot_tolerances_seconds(offsets)

    slots: dict[str, Any] = {}
    for off in offsets:
        target = t0 + timedelta(minutes=off)
        tol = tolerances[off]
        best = None
        best_err = None
        for snap in snapshots:
            err = abs((snap["ts"] - target).total_seconds())
            if err <= tol and (best_err is None or err < best_err):
                best, best_err = snap, err
        if best is None:
            # NOT INFERRED. A missing book is missing.
            slots[str(off)] = {"matched": False, "match_error_s": None}
            continue
        book = book_from_ladder(event["ticker"], best["ladder"], best["ts"])
        m = slot_metrics(book, side, best["ceiling"])
        m.update({
            "matched": True, "match_error_s": best_err,
            "snapshot_id": best["id"], "reason": best["reason"],
            "at_utc": best["ts"].isoformat(),
            "market_status": best["market_status"],
            "seconds_since_last_non_empty": _seconds_since_non_empty(
                snapshots, best["ts"], side, event["ticker"]),
        })
        slots[str(off)] = m

    last = _last_executable(snapshots, t0, side, event["ticker"])
    return {
        "event_id": event.get("id"),
        "ticker": event["ticker"],
        "icao": event["icao"],
        "climate_date": event["climate_date"],
        "source": event["source"],
        "side": side,
        "margin_f": event.get("margin_f"),
        "excess_f": event.get("excess_f"),
        # Carried through so the observation-time lead can be measured against
        # the SAME bound the live rule used, rather than re-deriving one.
        "certainty_bound_f": event.get("certainty_bound_f"),
        "boundary_f": event.get("boundary_f"),
        "observed_tmax_f": event.get("observed_tmax_f"),
        "call_correct": event.get("call_correct"),
        "detected_at_utc": event["detected_at_utc"],
        "n_snapshots_in_window": len(snapshots),
        "slots": slots,
        "last_executable": last,
        "classification": classify(slots, meaningful_size=meaningful_size),
        "meaningful_size": meaningful_size,
    }


def _sure_side_sweepable(snap: dict[str, Any], side: str, ticker: str) -> int:
    book = book_from_ladder(ticker, snap["ladder"], snap["ts"])
    if sure_side(side) is Side.YES:
        return book.buyable_yes_size(snap["ceiling"])
    return book.buyable_no_size(snap["ceiling"])


def _seconds_since_non_empty(
    snapshots: list[dict[str, Any]], at: datetime, side: str, ticker: str,
) -> Optional[float]:
    """Age of the most recent snapshot at/before ``at`` with any sure-side ask.

    ``0.0`` means this very snapshot has a quote. ``None`` means no snapshot in
    the window ever did -- which is information, not a gap.
    """
    latest = None
    for snap in snapshots:
        if snap["ts"] > at:
            break
        if _sure_side_sweepable(snap, side, ticker) > 0:
            latest = snap["ts"]
    return None if latest is None else (at - latest).total_seconds()


def _last_executable(
    snapshots: list[dict[str, Any]], t0: datetime, side: str, ticker: str,
) -> dict[str, Any]:
    """The last moment before T0 at which each size was available.

    Scans EVERY snapshot in the window, not just the slot grid: the slots answer
    "what did it look like at -5 minutes", this answers "when did it actually
    stop being possible", and the second needs all the resolution there is.
    """
    out: dict[str, Any] = {"any_positive": None, "by_threshold": {}}
    pre = [s for s in snapshots if s["ts"] < t0]
    depths = [(s["ts"], _sure_side_sweepable(s, side, ticker)) for s in pre]
    for threshold in AVAILABILITY_THRESHOLDS:
        hits = [ts for ts, d in depths if d >= threshold]
        if hits:
            last = max(hits)
            out["by_threshold"][str(threshold)] = {
                "timestamp": last.isoformat(),
                "minutes_before_lockin": (t0 - last).total_seconds() / 60.0,
            }
        else:
            out["by_threshold"][str(threshold)] = None
    positive = [ts for ts, d in depths if d > 0]
    if positive:
        last = max(positive)
        out["any_positive"] = {
            "timestamp": last.isoformat(),
            "minutes_before_lockin": (t0 - last).total_seconds() / 60.0,
        }
    out["pre_snapshots"] = len(pre)
    return out


def classify(slots: dict[str, Any], *, meaningful_size: int) -> str:
    """Which of the four hypotheses this event's slot series supports.

    Mutually exclusive and exhaustive. The category names come from the brief;
    the boundaries are defined HERE so they are auditable rather than implied:

    * ``UNKNOWN_INSUFFICIENT_SNAPSHOTS`` -- no T0 slot, or no pre-lock-in slot.
      Nothing about liquidity timing is claimed.
    * ``ALWAYS_EMPTY`` -- no matched slot at or before T0 ever had a sure-side ask.
    * ``ALWAYS_THIN`` -- some had an ask, none reached ``meaningful_size``.
    * ``LIQUID_UNTIL_LOCKIN`` -- meaningful depth AT T0.
    * ``LIQUIDITY_DISAPPEARS_BEFORE_LOCKIN`` -- not meaningful at T0, but
      meaningful somewhere in the final five minutes (T-5 .. T-1).
    * ``LIQUID_BEFORE_LOCKIN`` -- not meaningful at T0 nor in the final five
      minutes, but meaningful earlier (T-30 .. T-10).

    The split between the last two is the difference between "we missed it by
    minutes" and "it was gone long before we looked", which point at different
    remedies.
    """
    def matched(off: float) -> Optional[dict]:
        s = slots.get(str(off))
        return s if s and s.get("matched") else None

    at_zero = matched(0)
    pre_offsets = [o for o in OFFSETS_MINUTES if o < 0]
    pre = [(o, matched(o)) for o in pre_offsets]
    pre_matched = [(o, s) for o, s in pre if s is not None]
    if at_zero is None or not pre_matched:
        return UNKNOWN_INSUFFICIENT_SNAPSHOTS

    considered = [s for _o, s in pre_matched] + [at_zero]
    depths = [int(s["sure_side_sweepable_to_ceiling"]) for s in considered]
    if max(depths) <= 0:
        return ALWAYS_EMPTY
    if max(depths) < meaningful_size:
        return ALWAYS_THIN
    if int(at_zero["sure_side_sweepable_to_ceiling"]) >= meaningful_size:
        return LIQUID_UNTIL_LOCKIN
    late = [s for o, s in pre_matched if -5 <= o <= -1
            and int(s["sure_side_sweepable_to_ceiling"]) >= meaningful_size]
    if late:
        return LIQUIDITY_DISAPPEARS_BEFORE_LOCKIN
    return LIQUID_BEFORE_LOCKIN


# --------------------------------------------------------------------------- #
# Aggregation
# --------------------------------------------------------------------------- #
def _q(values: list[float]) -> dict[str, Optional[float]]:
    if not values:
        return {"n": 0, "p10": None, "p25": None, "median": None,
                "p75": None, "p90": None, "mean": None, "max": None}
    v = sorted(values)
    def pick(f): return v[min(len(v) - 1, int(len(v) * f))]
    return {"n": len(v), "p10": pick(0.10), "p25": pick(0.25),
            "median": pick(0.50), "p75": pick(0.75), "p90": pick(0.90),
            "mean": statistics.fmean(v), "max": v[-1]}


def _pearson(xs: list[float], ys: list[float]) -> Optional[float]:
    n = len(xs)
    if n < 3 or len(ys) != n:
        return None
    mx, my = statistics.fmean(xs), statistics.fmean(ys)
    sx = math.sqrt(sum((x - mx) ** 2 for x in xs))
    sy = math.sqrt(sum((y - my) ** 2 for y in ys))
    if sx == 0 or sy == 0:
        return None
    return sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / (sx * sy)


def decay_curve(events: list[dict[str, Any]],
                offsets: tuple[float, ...] = OFFSETS_MINUTES) -> dict[str, Any]:
    """Sure-side sweepable depth by offset, with match coverage.

    ``coverage_pct`` is reported beside every offset on purpose: an offset that
    matched 12% of events says nothing about liquidity there, and without the
    coverage number a low median is indistinguishable from a sampling hole.
    """
    out: dict[str, Any] = {}
    for off in offsets:
        depths, asks, errs, empty = [], [], [], 0
        matched = 0
        for e in events:
            s = e["slots"].get(str(off))
            if not s or not s.get("matched"):
                continue
            matched += 1
            depths.append(float(s["sure_side_sweepable_to_ceiling"]))
            errs.append(float(s["match_error_s"]))
            if s["sure_side_empty"]:
                empty += 1
            if s["sure_side_best_ask_cents"] is not None:
                asks.append(float(s["sure_side_best_ask_cents"]))
        out[str(off)] = {
            "matched": matched,
            "coverage_pct": (100.0 * matched / len(events)) if events else None,
            "sweepable_depth": _q(depths),
            "sure_side_ask_cents": _q(asks),
            "sure_side_empty": empty,
            "sure_side_empty_pct": (100.0 * empty / matched) if matched else None,
            "match_error_s": _q(errs),
        }
    return out


def analyse_liquidity_timing(
    conn: sqlite3.Connection, since: str, *,
    meaningful_size: int = DEFAULT_MEANINGFUL_SIZE,
) -> dict[str, Any]:
    """Full liquidity-timing analysis over every detection in the window."""
    rows = conn.execute(
        "SELECT * FROM lockin_events WHERE detected_at_utc >= ? "
        "ORDER BY detected_at_utc", (since,)
    ).fetchall()
    events: list[dict[str, Any]] = []
    for r in rows:
        ev = dict(r)
        t0 = _parse(ev["detected_at_utc"])
        if t0 is None:
            continue
        snaps = load_event_snapshots(conn, ev["ticker"], t0)
        events.append(reconstruct_event(ev, snaps,
                                        meaningful_size=meaningful_size))

    counts: dict[str, int] = {}
    for e in events:
        counts[e["classification"]] = counts.get(e["classification"], 0) + 1

    empty_at_zero, nonempty_at_zero = [], []
    for e in events:
        z = e["slots"].get("0")
        if not z or not z.get("matched"):
            continue
        (empty_at_zero if z["sure_side_empty"] else nonempty_at_zero).append(e)

    last_any = [e["last_executable"]["any_positive"]["minutes_before_lockin"]
                for e in events if e["last_executable"]["any_positive"]]
    by_threshold: dict[str, Any] = {}
    for t in AVAILABILITY_THRESHOLDS:
        mins = [e["last_executable"]["by_threshold"][str(t)]["minutes_before_lockin"]
                for e in events
                if e["last_executable"]["by_threshold"].get(str(t))]
        by_threshold[str(t)] = {
            "events_with_any": len(mins),
            "events_total": len(events),
            "pct_ever_available": (100.0 * len(mins) / len(events)) if events else None,
            "minutes_before_lockin": _q(mins),
        }

    return {
        "meaningful_size": meaningful_size,
        "events": len(events),
        "classification_counts": counts,
        "decay_curve": {
            "all": decay_curve(events),
            "empty_at_t0": decay_curve(empty_at_zero),
            "nonempty_at_t0": decay_curve(nonempty_at_zero),
        },
        "cohorts": {"empty_at_t0": len(empty_at_zero),
                    "nonempty_at_t0": len(nonempty_at_zero),
                    "t0_unmatched": len(events) - len(empty_at_zero)
                                    - len(nonempty_at_zero)},
        "last_executable": {
            "any_positive_minutes_before": _q(last_any),
            "events_with_any_positive": len(last_any),
            "by_threshold": by_threshold,
        },
        "margin_correlation": _margin_correlation(events),
        "edge_vs_liquidity": _edge_vs_liquidity(events),
        "weather_lead": weather_lead(conn, since, events),
        "per_event": events,
    }


def _margin_correlation(events: list[dict[str, Any]]) -> dict[str, Any]:
    """Is the disappearance correlated with how far past certainty we were?

    If depth collapses hardest at the SMALLEST margins, the book is thinning
    exactly where the outcome is least obvious -- which would be a market that
    knows something, not a market that is slow.
    """
    xs, ys = [], []
    for e in events:
        z = e["slots"].get("0")
        if not z or not z.get("matched") or e.get("margin_f") is None:
            continue
        xs.append(float(e["margin_f"]))
        ys.append(float(z["sure_side_sweepable_to_ceiling"]))
    buckets: dict[str, Any] = {}
    for lo, hi in ((0.5, 1.0), (1.0, 2.0), (2.0, 4.0), (4.0, float("inf"))):
        sel = [(x, y) for x, y in zip(xs, ys) if lo <= x < hi]
        buckets[f"[{lo}, {hi})"] = {
            "n": len(sel), "depth_at_t0": _q([y for _x, y in sel])}
    return {"n": len(xs), "corr_margin_vs_depth_at_t0": _pearson(xs, ys),
            "by_margin_bucket": buckets}


def _edge_vs_liquidity(events: list[dict[str, Any]]) -> dict[str, Any]:
    """Is a cheap sure side a tradeable one, or an empty one?

    Directly tests the shape of the (unreproducible) 17.4c claim: it was
    100 - top_of_book_ask with no regard for size. Here the apparent edge is
    split by whether ANY depth stood behind it.
    """
    with_depth, without_depth, xs, ys = [], [], [], []
    for e in events:
        z = e["slots"].get("0")
        if not z or not z.get("matched"):
            continue
        ask = z["sure_side_best_ask_cents"]
        if ask is None:
            continue
        edge = 100.0 - float(ask)
        depth = float(z["sure_side_sweepable_to_ceiling"])
        xs.append(edge)
        ys.append(depth)
        (with_depth if depth > 0 else without_depth).append(edge)
    return {
        "quoted_at_t0": len(xs),
        "corr_apparent_edge_vs_depth": _pearson(xs, ys),
        "apparent_edge_with_depth": _q(with_depth),
        "apparent_edge_without_depth": _q(without_depth),
    }


# A pair is only a valid lead measurement when the 5-minute source had at least
# this many observations per METAR observation over the same station-day. The
# true ratio is 12x (5 min vs hourly); 3x is a loose floor that tolerates gaps
# and METAR SPECIs while still rejecting a history that is mostly hole.
MIN_CADENCE_RATIO = 3.0


def intraday_history(conn: sqlite3.Connection, icao: str, climate_date: str
                     ) -> dict[str, Any]:
    """First/last observation and count for BOTH sources on one station-day.

    Used to decide whether a lead measurement is meaningful at all. The METAR
    side comes from ``station_logs`` and the 5-minute side from
    ``source_observations``; both are read-only.
    """
    m = conn.execute(
        "SELECT MIN(observed_at_utc) AS first, MAX(observed_at_utc) AS last, "
        "       COUNT(*) AS n FROM station_logs "
        "WHERE icao = ? AND climate_date = ? AND accepted = 1 AND temp_f IS NOT NULL",
        (icao, climate_date),
    ).fetchone()
    n5 = conn.execute(
        "SELECT MIN(observed_at_utc) AS first, MAX(observed_at_utc) AS last, "
        "       COUNT(*) AS n FROM source_observations "
        "WHERE icao = ? AND climate_date = ? AND source = 'nws5min'",
        (icao, climate_date),
    ).fetchone()
    return {
        "metar_first_obs_utc": m["first"], "metar_last_obs_utc": m["last"],
        "metar_observations": int(m["n"] or 0),
        "nws_first_obs_utc": n5["first"], "nws_last_obs_utc": n5["last"],
        "nws_observations": int(n5["n"] or 0),
    }


def nws_stream_purity(conn: sqlite3.Connection, icao: str, climate_date: str,
                      up_to: datetime) -> dict[str, Any]:
    """Was the 5-minute running max actually set by the 5-minute product?

    ⚠ THE CONTAMINATION THIS CATCHES, measured 2026-08-26. api.weather.gov serves
    the hourly METAR observation on the same endpoint, and sometimes WITHOUT a
    ``rawMessage`` field. ``nws_obs.classify_precision`` treats a missing
    rawMessage at a sub-hourly station as the 5-minute product, so those hourly
    rows land in the ``nws5min`` stream: 4-6 rows per station-day, all at :53,
    all carrying tenths-degC precision.

    The count is small; the effect is not. The running max is path-dependent, so
    ONE leaked METAR value permanently raises it -- and the "5-minute" crossing
    then fires off a METAR number. On KMIA 2026-08-26 the 13:53 METAR value
    (89.96 F / 32.2 C) set the 5-minute running max outright, which turns the
    lead measurement into METAR-versus-METAR and makes it meaningless.

    Precision is the discriminator, and it is already stored: the 5-minute
    product is whole degC (1.8 F steps), the METAR carries tenths (0.18 F). This
    checks whether the MAXIMUM observation up to the crossing came from a
    whole-degC row -- the precise question, rather than the blunt "were any
    tenths rows present at all".

    Read-only. Fixing the classifier itself would be a collection-layer change.
    """
    rows = conn.execute(
        "SELECT temp_f, precision_f FROM source_observations "
        "WHERE icao = ? AND climate_date = ? AND source = 'nws5min' "
        "  AND observed_at_utc <= ? AND temp_f IS NOT NULL",
        (icao, climate_date, up_to.isoformat()),
    ).fetchall()
    if not rows:
        return {"nws_rows_to_crossing": 0, "nws_tenths_rows_to_crossing": 0,
                "running_max_from_five_minute_product": False,
                "uncontaminated": False}
    tenths = sum(1 for r in rows if float(r["precision_f"] or 0.0) < 1.0)
    max_temp = max(float(r["temp_f"]) for r in rows)
    max_is_whole = any(
        abs(float(r["temp_f"]) - max_temp) < 1e-9
        and float(r["precision_f"] or 0.0) >= 1.0 for r in rows)
    return {
        "nws_rows_to_crossing": len(rows),
        "nws_tenths_rows_to_crossing": tenths,
        "running_max_from_five_minute_product": max_is_whole,
        "uncontaminated": max_is_whole,
    }


def validate_lead_history(hist: dict[str, Any], earliest_detection: datetime,
                          purity: Optional[dict[str, Any]] = None
                          ) -> dict[str, Any]:
    """Are the two intraday histories complete and comparable enough to compare?

    ⚠ THIS EXISTS BECAUSE A NEGATIVE LEAD IS NOT A FINDING. The 5-minute running
    max lives in memory and backfills only ~2 h at startup, while ``daily_tmax``
    is DB-backed and complete. So on any day the tracker restarts mid-day the
    5-minute source begins behind and crosses its bound LATE -- producing a
    "lead" that is negative, physically backwards, and purely an artefact of when
    the process happened to start.

    Rather than let those pairs drag the lead statistic, they are classified as
    ``insufficient_history`` and excluded from it. They are still counted and
    listed: discarding them would delete the evidence that a restart happened.

    Three checks, reported separately so a rejection is diagnosable:

    * ``starts_comparable`` -- the 5-minute history begins no LATER than the
      METAR history. If it starts later it was structurally unable to see the
      crossing first, whatever the feed's merits.
    * ``observed_before_either`` -- the 5-minute source was already observing
      before either detection fired. A source that starts mid-crossing cannot be
      said to have led or lagged.
    * ``cadence_ok`` -- the 5-minute history is at least MIN_CADENCE_RATIO times
      denser than the METAR history over the same day.
    * ``uncontaminated`` -- the 5-minute running max at the crossing was set by an
      actual 5-minute observation, not by an hourly METAR row that leaked into
      the stream (see :func:`nws_stream_purity`). A contaminated stream is not a
      comparable history: the "lead" it produces is METAR against METAR.
    """
    m_first = _parse(hist.get("metar_first_obs_utc"))
    n_first = _parse(hist.get("nws_first_obs_utc"))
    m_n, n_n = hist["metar_observations"], hist["nws_observations"]

    has_both = m_first is not None and n_first is not None and m_n > 0 and n_n > 0
    starts_comparable = bool(has_both and n_first <= m_first)
    observed_before_either = bool(n_first is not None
                                  and n_first <= earliest_detection)
    ratio = (n_n / m_n) if m_n else None
    cadence_ok = bool(ratio is not None and ratio >= MIN_CADENCE_RATIO)
    purity = purity or {"uncontaminated": False,
                        "nws_tenths_rows_to_crossing": None,
                        "nws_rows_to_crossing": None,
                        "running_max_from_five_minute_product": False}
    return {
        **hist, **purity,
        "cadence_ratio": ratio,
        "starts_comparable": starts_comparable,
        "observed_before_either": observed_before_either,
        "cadence_ok": cadence_ok,
        "valid": bool(has_both and starts_comparable and observed_before_either
                      and cadence_ok and purity["uncontaminated"]),
    }


def crossing_observation_time(
    conn: sqlite3.Connection, icao: str, climate_date: str, bound_f: float,
    source: str,
) -> Optional[datetime]:
    """VALID time of the first observation whose running max reaches ``bound_f``.

    This is concept (2) measured properly -- as a property of the WEATHER RECORD,
    independent of when we happened to receive it. ``weather_lead``'s other
    measure uses detection times, which additionally carry each feed's delivery
    delay, and the two can point in opposite directions.

    It does NOT redefine a deterministic lock-in: ``bound_f`` is the
    ``certainty_bound_f`` already stored on the event by the live rule. This only
    asks when each observation record first reached that same bound.

    For the 5-minute source it considers WHOLE-degC rows only, excluding the
    hourly METAR observations that leak into the stream (see
    :func:`nws_stream_purity`) -- otherwise the "5-minute" crossing time could be
    a METAR observation's, and the lead would be measuring a feed against itself.
    """
    if source == "metar":
        rows = conn.execute(
            "SELECT observed_at_utc, inserted_at_utc AS got_at, temp_f "
            "FROM station_logs WHERE icao = ? AND climate_date = ? AND accepted = 1 "
            "  AND temp_f IS NOT NULL ORDER BY observed_at_utc",
            (icao, climate_date)).fetchall()
    else:
        rows = conn.execute(
            "SELECT observed_at_utc, received_at_utc AS got_at, temp_f "
            "FROM source_observations WHERE icao = ? AND climate_date = ? "
            "  AND source = 'nws5min' AND precision_f >= 1.0 AND temp_f IS NOT NULL "
            "ORDER BY observed_at_utc", (icao, climate_date)).fetchall()
    running = None
    for r in rows:
        t = float(r["temp_f"])
        running = t if running is None else max(running, t)
        if running >= bound_f:
            return _parse(r["observed_at_utc"]), _parse(r["got_at"])
    return None, None


def weather_lead(conn: sqlite3.Connection, since: str,
                 events: list[dict[str, Any]]) -> dict[str, Any]:
    """THREE STRICTLY SEPARATE THINGS, and they must not be conflated.

    This function reports (2) and (3) only. (1) lives in the sources panel.

    1. ``nws_observation_to_receipt_seconds`` -- how long after an observation's
       VALID time we hold it. An upper bound on NWS publication latency, since it
       includes up to one poll interval of our own delay. NOT a measure of the
       feed's publication speed on its own.
    2. ``nws_lockin_lead_vs_metar_seconds`` -- how much earlier the 5-minute
       running max proved certainty than the hourly METAR did. A property of the
       WEATHER data, independent of any market.
    3. ``liquidity_at_nws_lockin`` / ``liquidity_at_metar_lockin`` -- executable
       sure-side depth at each of those two instants. A property of the MARKET.
       A large lead into an empty book is worth exactly nothing, which is why (2)
       and (3) are never combined into one number.

    Pairs each METAR detection with the shadow ``nws5min`` detection for the same
    (ticker, climate_date). The shadow path applies the UNCHANGED lock-in rule to
    the 5-minute running max, so nothing is redefined here. The join is done at
    analysis time rather than by populating ``lockin_events.paired_event_id``,
    which would mean changing the collection layer.
    """
    by_key: dict[tuple[str, str], dict[str, dict]] = {}
    for e in events:
        by_key.setdefault((e["ticker"], e["climate_date"]), {})[e["source"]] = e

    pairs = []
    hist_cache: dict[tuple[str, str], dict[str, Any]] = {}
    for (ticker, cdate), sources in by_key.items():
        metar, five = sources.get("metar"), sources.get("nws5min")
        if metar is None or five is None:
            continue
        t_metar = _parse(metar["detected_at_utc"])
        t_five = _parse(five["detected_at_utc"])
        if t_metar is None or t_five is None:
            continue
        icao = metar.get("icao") or five.get("icao")
        key = (icao, cdate)
        if key not in hist_cache:
            hist_cache[key] = intraday_history(conn, icao, cdate)
        # Purity is evaluated up to the 5-MINUTE crossing: that is the instant
        # whose running max is in question.
        purity = nws_stream_purity(conn, icao, cdate, t_five)
        history = validate_lead_history(
            hist_cache[key], min(t_metar, t_five), purity)

        bound = metar.get("certainty_bound_f")
        m_cross = n_cross = m_got = n_got = None
        obs_lead = receipt_lead = None
        if bound is not None:
            m_cross, m_got = crossing_observation_time(
                conn, icao, cdate, float(bound), "metar")
            n_cross, n_got = crossing_observation_time(
                conn, icao, cdate, float(bound), "nws5min")
            if m_cross and n_cross:
                obs_lead = (m_cross - n_cross).total_seconds()
            if m_got and n_got:
                receipt_lead = (m_got - n_got).total_seconds()

        five_zero = five["slots"].get("0") or {}
        metar_zero = metar["slots"].get("0") or {}
        pairs.append({
            "ticker": ticker, "climate_date": cdate, "icao": icao,
            "first_earlier_weather_signal_timestamp": five["detected_at_utc"],
            "metar_signal_timestamp": metar["detected_at_utc"],
            # (2) WEATHER, two ways, and they can disagree in SIGN.
            #  - ..._receipt: from detection times. What we could have ACTED on;
            #    it carries each feed's delivery delay, i.e. concept (1).
            #  - ..._observation: from observation VALID times. The pure weather
            #    property, with delivery removed.
            # _receipt: when we actually HELD the crossing observation from each
            # feed. The actionable measure -- observation lead minus delivery gap.
            "nws_lockin_lead_vs_metar_seconds_receipt": receipt_lead,
            "nws_lockin_lead_vs_metar_seconds_observation": obs_lead,
            # _detection: when the ENGINE fired. Kept separate because the shadow
            # path fires on a NEW HIGH rather than on first crossing, so this
            # carries the engine's firing behaviour as well as the feed's delay
            # and is a LOWER bound on the lead, not a clean measurement of it.
            "nws_lockin_lead_vs_metar_seconds_detection":
                (t_metar - t_five).total_seconds(),
            "metar_crossing_received_at_utc": m_got.isoformat() if m_got else None,
            "nws_crossing_received_at_utc": n_got.isoformat() if n_got else None,
            "metar_crossing_observed_at_utc":
                m_cross.isoformat() if m_cross else None,
            "nws_crossing_observed_at_utc":
                n_cross.isoformat() if n_cross else None,
            # (3) MARKET: executable depth at each of those two instants.
            "liquidity_at_nws_lockin": (five_zero.get("sure_side_sweepable_to_ceiling")
                                        if five_zero.get("matched") else None),
            "liquidity_at_metar_lockin": (metar_zero.get("sure_side_sweepable_to_ceiling")
                                          if metar_zero.get("matched") else None),
            "ask_at_nws_lockin_cents": (five_zero.get("sure_side_best_ask_cents")
                                        if five_zero.get("matched") else None),
            "ask_at_metar_lockin_cents": (metar_zero.get("sure_side_best_ask_cents")
                                          if metar_zero.get("matched") else None),
            "history": history,
            "lead_measurement": "valid" if history["valid"] else "insufficient_history",
        })

    valid = [p for p in pairs if p["lead_measurement"] == "valid"]
    insufficient = [p for p in pairs if p["lead_measurement"] != "valid"]
    leads = [p["nws_lockin_lead_vs_metar_seconds_receipt"] for p in valid
             if p["nws_lockin_lead_vs_metar_seconds_receipt"] is not None]
    det_leads = [p["nws_lockin_lead_vs_metar_seconds_detection"] for p in valid]
    # A negative lead among VALID pairs is a genuine anomaly worth looking at
    # (quantization can push a 1-degC-resolution crossing later than a
    # tenths-degC one). A negative lead among pairs that FAILED the history check
    # is just a restart artefact and is already excluded from the statistic.
    negative_valid = [p for p in valid
                      if (p["nws_lockin_lead_vs_metar_seconds_receipt"] or 0) < 0]
    obs_leads = [p["nws_lockin_lead_vs_metar_seconds_observation"] for p in valid
                 if p["nws_lockin_lead_vs_metar_seconds_observation"] is not None]
    gained = [p for p in valid
              if p["liquidity_at_nws_lockin"] and not p["liquidity_at_metar_lockin"]]
    unpaired = {
        "metar_only": sum(1 for s in by_key.values()
                          if "metar" in s and "nws5min" not in s),
        "nws5min_only": sum(1 for s in by_key.values()
                            if "nws5min" in s and "metar" not in s),
    }
    return {
        "paired_events": len(pairs),
        "valid_lead_measurements": len(valid),
        "insufficient_history": len(insufficient),
        "unpaired": unpaired,
        # (2) WEATHER-ONLY. Computed over VALID pairs exclusively.
        # Receipt-based carries delivery delay (concept 1); observation-based
        # does not. Reported side by side because they can differ in sign.
        "nws_lockin_lead_vs_metar_seconds_receipt": _q(leads),
        "nws_lockin_lead_vs_metar_seconds_observation": _q(obs_leads),
        "nws_lockin_lead_vs_metar_seconds_detection": _q(det_leads),
        "pairs_with_negative_lead_among_valid": len(negative_valid),
        # (3) MARKET-ONLY, at each of the two instants. Never combined with (2).
        "liquidity_at_nws_lockin": _q([float(p["liquidity_at_nws_lockin"])
                                       for p in valid
                                       if p["liquidity_at_nws_lockin"] is not None]),
        "liquidity_at_metar_lockin": _q([float(p["liquidity_at_metar_lockin"])
                                         for p in valid
                                         if p["liquidity_at_metar_lockin"] is not None]),
        "events_where_nws_had_depth_and_metar_did_not": len(gained),
        "pairs": pairs[:50],
        "insufficient_history_pairs": insufficient[:50],
    }


# --------------------------------------------------------------------------- #
# Feed availability: dropped grid slots, reported as MISSING, never as "late"
# --------------------------------------------------------------------------- #
# Measured 2026-08-26 by a one-off 23-minute probe at 30 s resolution against
# api.weather.gov (KPHL / KMDW / KMIA). These are NOT computed from the
# collector's data -- the collector stores no response headers -- so they are
# recorded constants WITH PROVENANCE, not live statistics. They are reported
# because the reporting is otherwise silent about why the delay varies.
PROBE_DATE = "2026-08-26"
PROBE_STATIONS = ("KPHL", "KMDW", "KMIA")
PROBE_RESOLUTION_S = 30
PROBE_SPAN_MIN = 22.9
CACHE_TTL_OBSERVED_S = (245, 288)      # max-age at reset, min..max
CACHE_TTL_MEDIAN_S = 268
PROBE_ADVANCES_OBSERVED = 10           # newest-observation advances seen
PROBE_ADVANCES_CACHE_GATED = 10        # ...of which coincided with a TTL reset
PROBE_AVAILABILITY_DELAY_S = (704, 1281)   # observed_to_first_available, min..max
PROBE_AVAILABILITY_MEDIAN_S = 941

# The disclaimer that must travel with every availability-delay figure.
AVAILABILITY_DISCLAIMER = (
    "Observed availability delay is cache-influenced and is NOT a measurement of "
    "NWS publication latency. Because cache phase is shared and uncontrolled, "
    "this statistic must not be interpreted as a stable feed characteristic."
)

FIVE_MINUTE_GRID_S = 300
# If no observation was RECEIVED from any station within this window of a missing
# slot, the collector was not polling and the slot is downtime, not a drop.
DOWNTIME_WINDOW_S = 2700.0


def dropped_observations(conn: sqlite3.Connection, since: str) -> dict[str, Any]:
    """5-minute grid slots that were never exposed, separated from downtime.

    ⚠ A MISSING OBSERVATION IS NOT A LATE OBSERVATION. During a 23-minute probe
    at 30 s resolution on 2026-08-26 the 17:15 slot never appeared at any of
    KPHL, KMDW or KMIA -- the sequence ran 17:05 -> 17:10 -> 17:20 -> 17:25. The
    feed does not merely delay the grid, it drops points from it. Folding those
    into a latency statistic would silently convert "never arrived" into "arrived
    slowly", which is the opposite of what happened and would overstate coverage.

    ``source_observations`` is keyed UNIQUE(icao, source, observed_at_utc) and the
    endpoint returns the last 24 observations, so any slot the API exposed while
    we were polling is stored. A slot with no row was therefore never exposed to
    us -- provided we were polling at the time, which is what the downtime check
    establishes.

    Only whole-degC rows count as the 5-minute product; leaked hourly METAR rows
    sit off-grid (:51/:53/:54) and are excluded (see :func:`nws_stream_purity`).
    """
    rows = conn.execute(
        "SELECT icao, climate_date, observed_at_utc FROM source_observations "
        "WHERE source = 'nws5min' AND precision_f >= 1.0 AND received_at_utc >= ? "
        "ORDER BY icao, climate_date, observed_at_utc",
        (since,),
    ).fetchall()
    receipts = sorted(
        t for (t,) in conn.execute(
            "SELECT DISTINCT received_at_utc FROM source_observations "
            "WHERE received_at_utc >= ?", (since,)))
    receipt_times = [_parse(t) for t in receipts]
    receipt_times = [t for t in receipt_times if t is not None]

    by_day: dict[tuple[str, str], list[datetime]] = {}
    for r in rows:
        ts = _parse(r["observed_at_utc"])
        if ts is not None:
            by_day.setdefault((r["icao"], r["climate_date"]), []).append(ts)

    def polling_near(t: datetime) -> bool:
        return any(abs((rt - t).total_seconds()) <= DOWNTIME_WINDOW_S
                   for rt in receipt_times)

    per_day: list[dict[str, Any]] = []
    total_expected = total_present = total_dropped = total_downtime = 0
    for (icao, cdate), stamps in sorted(by_day.items()):
        stamps.sort()
        first, last = stamps[0], stamps[-1]
        have = set(stamps)
        expected = 0
        dropped: list[datetime] = []
        downtime = 0
        slot = first
        while slot <= last:
            expected += 1
            if slot not in have:
                # Polling at the time -> the API never exposed it (a DROP).
                # Not polling -> we simply were not looking (downtime).
                if polling_near(slot):
                    dropped.append(slot)
                else:
                    downtime += 1
            slot += timedelta(seconds=FIVE_MINUTE_GRID_S)
        present = expected - len(dropped) - downtime
        total_expected += expected
        total_present += present
        total_dropped += len(dropped)
        total_downtime += downtime
        if dropped or downtime:
            per_day.append({
                "icao": icao, "climate_date": cdate,
                "expected_slots": expected, "present": present,
                "dropped": len(dropped), "downtime_slots": downtime,
                "dropped_slots_utc": [d.isoformat() for d in dropped[:20]],
                # Each of these was never exposed in ANY poll -- if it had been,
                # UNIQUE(icao, source, observed_at_utc) would hold a row for it.
                "ever_subsequently_exposed": False,
            })
    return {
        "expected_slots": total_expected,
        "present": total_present,
        "dropped_never_exposed": total_dropped,
        "downtime_slots": total_downtime,
        "dropped_pct": (100.0 * total_dropped / total_expected
                        if total_expected else None),
        "affected": per_day,
    }
