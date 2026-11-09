"""Daily report + full analysis for the Strategy-1 discovery experiment.

THIS FILE IS THE ANSWER TO "WHERE DID THAT NUMBER COME FROM?"

The 17.4c latency edge, the $1,183/100-contracts estimate and the "421
detections, 0 wrong calls" safety claim all existed only as prose in CLAUDE.md.
The commits that recorded them changed no code, shipped no script, and cached no
data, so nothing in the repository could regenerate or refute them. Two of the
three did not survive re-derivation.

Every statistic printed here is computed from persisted rows by code you can
read, with the raw inputs still on disk. Nothing is carried forward from prose.

Usage::

    .venv/bin/python -m weather_arb.experiment_report                 # last 24 h
    .venv/bin/python -m weather_arb.experiment_report --days 21       # the window
    .venv/bin/python -m weather_arb.experiment_report --json out.json
    .venv/bin/python -m weather_arb.experiment_report --gate0         # Gate 0 report

Read the EXECUTABLE VWAP EDGE panel, never the apparent-edge panel. The apparent
edge is top-of-book and is printed only so the gap between it and reality stays
visible -- it is the number that produced 17.4c.
"""

from __future__ import annotations

import argparse
import json
import math
import sqlite3
import statistics
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from .config import Settings
from .liquidity_analysis import (
    AVAILABILITY_DISCLAIMER, AVAILABILITY_THRESHOLDS, CACHE_TTL_MEDIAN_S,
    CACHE_TTL_OBSERVED_S, DEFAULT_MEANINGFUL_SIZE, LABEL_TO_HYPOTHESIS,
    OFFSETS_MINUTES, PROBE_ADVANCES_CACHE_GATED, PROBE_ADVANCES_OBSERVED,
    PROBE_AVAILABILITY_DELAY_S, PROBE_AVAILABILITY_MEDIAN_S, PROBE_DATE,
    PROBE_RESOLUTION_S, PROBE_SPAN_MIN, PROBE_STATIONS,
    analyse_liquidity_timing, dropped_observations, slot_tolerances_seconds,
)

# Sizes reported in the economics panel. -1 is "the whole ladder at the ceiling".
REPORT_SIZES = (1, 10, 25, 50, 100, 250, 500)

# Adverse-selection buckets (P8). Margin here is observed_tmax - strike, so with
# the default 0.5F lock-in margin every detection starts at 0.5.
MARGIN_BUCKETS = ((0.5, 0.75), (0.75, 1.0), (1.0, 2.0), (2.0, float("inf")))

# Offsets the watchlist reports. Taken FROM OFFSETS_MINUTES rather than written
# out, so the key type can never drift (str(-5.0) != str(-5)).
WATCH_OFFSETS = tuple(o for o in OFFSETS_MINUTES if o in (-5, -2, -1, 0))


# --------------------------------------------------------------------------- #
# Raw data access
# --------------------------------------------------------------------------- #
_REQUIRED_TABLES = ("lockin_events", "book_snapshots", "sweep_metrics",
                    "source_observations")


def _connect(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    have = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table'")}
    missing = [t for t in _REQUIRED_TABLES if t not in have]
    if missing:
        raise SystemExit(
            f"{db_path} has no experiment tables ({', '.join(missing)}).\n"
            "They are created by Database.connect(), so the tracker has not been "
            "restarted on the new build yet:\n"
            "  launchctl unload ~/Library/LaunchAgents/com.weatherarb.tracker.plist\n"
            "  launchctl load   ~/Library/LaunchAgents/com.weatherarb.tracker.plist"
        )
    return conn


def _window(days: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()


def load_events(conn: sqlite3.Connection, since: str) -> list[dict[str, Any]]:
    """Detections in the window, each joined to its signal-instant book.

    LEFT JOIN, deliberately: an event with no snapshot is a data-quality finding
    (the book fetch failed), not a row to silently drop. Dropping it would bias
    the sample toward markets that were quotable, which is the same survivorship
    error that inflated earlier results.
    """
    rows = conn.execute(
        """
        SELECT e.*,
               s.id                  AS snapshot_id,
               s.gate_outcome        AS gate_outcome,
               s.best_yes_ask_cents  AS yes_ask,
               s.best_no_ask_cents   AS no_ask,
               s.best_yes_bid_cents  AS yes_bid,
               s.spread_cents        AS spread_cents,
               s.yes_depth_to_ceiling AS yes_depth,
               s.no_depth_to_ceiling  AS no_depth,
               s.round_trip_ms       AS round_trip_ms,
               s.market_status       AS market_status
        FROM lockin_events e
        LEFT JOIN book_snapshots s
               ON s.event_id = e.id AND s.reason = 'signal'
        WHERE e.detected_at_utc >= ?
        ORDER BY e.detected_at_utc
        """,
        (since,),
    ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        # The ask on the side we would BUY. Everything downstream is that side.
        d["entry_ask_cents"] = d["yes_ask"] if d["side"] == "yes" else d["no_ask"]
        d["entry_depth"] = d["yes_depth"] if d["side"] == "yes" else d["no_depth"]
        d["apparent_edge_cents"] = (
            100 - d["entry_ask_cents"] if d["entry_ask_cents"] is not None else None)
        out.append(d)
    return out


def load_sweeps(conn: sqlite3.Connection, snapshot_ids: list[int]
                ) -> dict[tuple[int, int], dict[str, Any]]:
    """(snapshot_id, target_size) -> executable economics."""
    if not snapshot_ids:
        return {}
    out: dict[tuple[int, int], dict[str, Any]] = {}
    for chunk in _chunks(snapshot_ids, 400):
        q = ",".join("?" for _ in chunk)
        for r in conn.execute(
            f"SELECT * FROM sweep_metrics WHERE snapshot_id IN ({q})", chunk
        ):
            out[(int(r["snapshot_id"]), int(r["target_size"]))] = dict(r)
    return out


def load_offsets(conn: sqlite3.Connection, since: str) -> list[dict[str, Any]]:
    """Every event-linked snapshot, for the repricing decay curve."""
    return [dict(r) for r in conn.execute(
        """
        SELECT s.event_id, s.offset_seconds, s.reason, s.best_yes_ask_cents,
               s.best_no_ask_cents, s.yes_depth_to_ceiling, s.no_depth_to_ceiling,
               e.side, e.ticker
        FROM book_snapshots s
        JOIN lockin_events e ON e.id = s.event_id
        WHERE e.detected_at_utc >= ? AND s.offset_seconds IS NOT NULL
        ORDER BY s.event_id, s.offset_seconds
        """,
        (since,),
    )]


def load_control(conn: sqlite3.Connection, since: str) -> list[dict[str, Any]]:
    """Control/armed samples -- the null hypothesis for "is 17c unusual?"."""
    return [dict(r) for r in conn.execute(
        """
        SELECT ticker, reason, best_yes_ask_cents, best_no_ask_cents,
               spread_cents, yes_depth_to_ceiling, no_depth_to_ceiling,
               round_trip_ms
        FROM book_snapshots
        WHERE event_id IS NULL AND received_at_utc >= ?
        """,
        (since,),
    )]


def load_source_observations(conn: sqlite3.Connection, since: str
                             ) -> list[dict[str, Any]]:
    return [dict(r) for r in conn.execute(
        "SELECT * FROM source_observations WHERE received_at_utc >= ?", (since,)
    )]


def load_metar(conn: sqlite3.Connection, since: str) -> list[dict[str, Any]]:
    return [dict(r) for r in conn.execute(
        "SELECT icao, observed_at_utc, inserted_at_utc, temp_f, accepted "
        "FROM station_logs WHERE inserted_at_utc >= ?", (since,)
    )]


# --------------------------------------------------------------------------- #
# Statistics (pure stdlib -- this project has no numpy and should not grow one)
# --------------------------------------------------------------------------- #
def _chunks(seq: list, n: int):
    for i in range(0, len(seq), n):
        yield seq[i:i + n]


def pearson(xs: list[float], ys: list[float]) -> Optional[float]:
    """Pearson r, or None when it is undefined (n<3 or a constant series)."""
    n = len(xs)
    if n < 3 or len(ys) != n:
        return None
    mx, my = statistics.fmean(xs), statistics.fmean(ys)
    sx = math.sqrt(sum((x - mx) ** 2 for x in xs))
    sy = math.sqrt(sum((y - my) ** 2 for y in ys))
    if sx == 0 or sy == 0:
        return None
    return sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / (sx * sy)


def quantiles(values: list[float]) -> dict[str, Optional[float]]:
    if not values:
        return {"n": 0, "min": None, "p25": None, "median": None,
                "p75": None, "p90": None, "max": None, "mean": None}
    v = sorted(values)
    def q(f): return v[min(len(v) - 1, int(len(v) * f))]
    return {"n": len(v), "min": v[0], "p25": q(0.25), "median": q(0.50),
            "p75": q(0.75), "p90": q(0.90), "max": v[-1],
            "mean": statistics.fmean(v)}


def _fmt_q(label: str, q: dict[str, Optional[float]], unit: str = "") -> str:
    if not q["n"]:
        return f"    {label:<34s} (no data)"
    return (f"    {label:<34s} n={q['n']:<5d} median {q['median']:>8.2f}{unit}"
            f"  mean {q['mean']:>8.2f}{unit}  p25 {q['p25']:>7.2f}"
            f"  p75 {q['p75']:>7.2f}  max {q['max']:>8.2f}")


# --------------------------------------------------------------------------- #
# Analysis
# --------------------------------------------------------------------------- #
def analyse(db_path: str, *, days: float, latency_seconds: float = 0.0,
            slippage_cents: float = 0.0,
            meaningful_size: int = DEFAULT_MEANINGFUL_SIZE) -> dict[str, Any]:
    conn = _connect(db_path)
    since = _window(days)
    events = load_events(conn, since)
    sweeps = load_sweeps(conn, [e["snapshot_id"] for e in events if e["snapshot_id"]])
    offsets = load_offsets(conn, since)
    control = load_control(conn, since)
    obs = load_source_observations(conn, since)
    metar = load_metar(conn, since)

    out: dict[str, Any] = {
        "window_days": days, "since_utc": since,
        "generated_utc": datetime.now(timezone.utc).isoformat(),
        "latency_seconds": latency_seconds, "slippage_cents": slippage_cents,
        "detections": _detections(events),
        "accuracy": _accuracy(events),
        "apparent_edge": _apparent_edge(events),
        "executable": _executable(events, sweeps, slippage_cents),
        "depth": _depth(events, control),
        "repricing": _repricing(events, offsets),
        "sources": _sources(obs, metar),
        "disagreement": _disagreement(conn, since),
        "adverse_selection": _adverse_selection(events, sweeps, slippage_cents),
        "data_quality": _data_quality(conn, since, events),
        # Read-only reconstruction of the book around each detection. Added
        # after the first ten live events all showed zero sure-side depth at T0
        # against a control median of 2,443 -- which separates "too late" from
        # "invalid", and those imply different next steps.
        "liquidity_timing": analyse_liquidity_timing(
            conn, since, meaningful_size=meaningful_size),
        "feed_availability": dropped_observations(conn, since),
    }
    conn.close()
    return out


def _detections(events: list[dict]) -> dict[str, Any]:
    by_source: dict[str, dict[str, Any]] = {}
    for e in events:
        b = by_source.setdefault(e["source"], {"total": 0, "gates": {}, "stations": {}})
        b["total"] += 1
        gate = e["gate_outcome"] or "no_snapshot"
        b["gates"][gate] = b["gates"].get(gate, 0) + 1
        b["stations"][e["icao"]] = b["stations"].get(e["icao"], 0) + 1
    qualifying = sum(1 for e in events if e["gate_outcome"] == "accepted")
    return {"total": len(events), "qualifying": qualifying,
            "rejected": len(events) - qualifying, "by_source": by_source}


def _accuracy(events: list[dict]) -> dict[str, Any]:
    graded = [e for e in events if e["call_correct"] is not None]
    def split(key_fn):
        d: dict[str, dict[str, int]] = {}
        for e in graded:
            k = key_fn(e)
            s = d.setdefault(str(k), {"n": 0, "correct": 0})
            s["n"] += 1
            s["correct"] += int(e["call_correct"])
        return d
    def price_bucket(e):
        a = e["entry_ask_cents"]
        if a is None:
            return "no_quote"
        for hi in (25, 50, 75, 90, 98, 100):
            if a <= hi:
                return f"<={hi}c"
        return ">100c"
    def margin_bucket(e):
        m = e["margin_f"]
        for lo, hi in MARGIN_BUCKETS:
            if lo <= m < hi:
                return f"[{lo}, {hi})"
        return f"<{MARGIN_BUCKETS[0][0]}"
    correct = sum(1 for e in graded if e["call_correct"])
    return {
        "graded": len(graded), "ungraded": len(events) - len(graded),
        "correct": correct, "incorrect": len(graded) - correct,
        "accuracy_pct": 100.0 * correct / len(graded) if graded else None,
        "by_margin": split(margin_bucket), "by_station": split(lambda e: e["icao"]),
        "by_side": split(lambda e: e["side"]), "by_source": split(lambda e: e["source"]),
        "by_price": split(price_bucket),
        "incorrect_rows": [
            {k: e[k] for k in ("ticker", "climate_date", "source", "side",
                               "settled_result", "observed_tmax_f", "margin_f",
                               "excess_f", "entry_ask_cents")}
            for e in graded if not e["call_correct"]],
    }


def _apparent_edge(events: list[dict]) -> dict[str, Any]:
    """Top-of-book discount. THE NUMBER NOT TO TRUST -- printed for contrast.

    This is how 17.4c was computed: 100 minus the headline ask, with no regard
    for how many contracts rested there. The live engine never pays this price;
    it sweeps every level up to the ceiling.
    """
    quoted = [e for e in events if e["apparent_edge_cents"] is not None]
    disc = [e for e in quoted if e["entry_ask_cents"] < 98]
    return {
        "quoted": len(quoted),
        "at_ceiling_or_above": len(quoted) - len(disc),
        "discounted": len(disc),
        "discounted_pct": 100.0 * len(disc) / len(quoted) if quoted else None,
        "all": quantiles([float(e["apparent_edge_cents"]) for e in quoted]),
        "discounted_only": quantiles([float(e["apparent_edge_cents"]) for e in disc]),
    }


def _executable(events: list[dict], sweeps: dict, slippage_cents: float
                ) -> dict[str, Any]:
    """P7: real economics at real sizes, ceiling-truncated and net of fees.

    ``gross_edge = 100 - VWAP`` (a correct lock-in settles at 100c), then fees,
    then an optional explicit slippage haircut. ``pct_available`` is the whole
    point: if 100 contracts cannot be bought, that shows up as 12% rather than
    silently as a good price.
    """
    per_size: dict[str, Any] = {}
    for size in REPORT_SIZES:
        nets, vwaps, fills, pcts, edges = [], [], [], [], []
        for e in events:
            sid = e["snapshot_id"]
            if sid is None:
                continue
            row = sweeps.get((int(sid), size))
            if row is None or row["vwap_cents"] is None or not row["fillable_size"]:
                continue
            vwap = float(row["vwap_cents"]) + slippage_cents
            filled = int(row["fillable_size"])
            fee = float(row["fee_cents"])
            nets.append(((100.0 - vwap) * filled - fee) / 100.0)
            vwaps.append(vwap)
            fills.append(float(filled))
            pcts.append(100.0 * filled / size)
            edges.append(100.0 - vwap)
        per_size[str(size)] = {
            "events_with_fill": len(nets),
            "net_usd": quantiles(nets),
            "vwap_cents": quantiles(vwaps),
            "gross_edge_cents": quantiles(edges),
            "fillable_size": quantiles(fills),
            "pct_of_requested": quantiles(pcts),
            "total_net_usd": sum(nets),
        }
    # Slippage: how much the VWAP is worse than the headline ask.
    slip = [float(sweeps[(int(e["snapshot_id"]), 100)]["slippage_cents"])
            for e in events
            if e["snapshot_id"] is not None
            and (int(e["snapshot_id"]), 100) in sweeps
            and sweeps[(int(e["snapshot_id"]), 100)]["slippage_cents"] is not None]
    return {"per_size": per_size, "slippage_at_100_cents": quantiles(slip)}


def _depth(events: list[dict], control: list[dict]) -> dict[str, Any]:
    """Depth at the signal vs depth when nothing is happening (the control)."""
    ev = [float(e["entry_depth"]) for e in events if e["entry_depth"] is not None]
    ctl = []
    for c in control:
        for k in ("yes_depth_to_ceiling", "no_depth_to_ceiling"):
            if c.get(k) is not None:
                ctl.append(float(c[k]))
    return {"at_signal": quantiles(ev), "control": quantiles(ctl),
            "control_samples": len(control)}


def _repricing(events: list[dict], offsets: list[dict]) -> dict[str, Any]:
    """P6: the ask on the traded side as a function of time since detection."""
    by_event: dict[int, dict[float, float]] = {}
    for o in offsets:
        ask = o["best_yes_ask_cents"] if o["side"] == "yes" else o["best_no_ask_cents"]
        if ask is None:
            continue
        by_event.setdefault(int(o["event_id"]), {})[float(o["offset_seconds"])] = float(ask)
    curve: dict[str, Any] = {}
    for off in (0.0, 60.0, 120.0, 300.0, 600.0, 1800.0):
        vals = [m[off] for m in by_event.values() if off in m]
        curve[str(int(off))] = quantiles(vals)
    # Time for the ask to reach the ceiling, per event.
    to_ceiling = []
    for m in by_event.values():
        hit = sorted(o for o, a in m.items() if a >= 98)
        if hit:
            to_ceiling.append(hit[0])
    return {"events": len(by_event), "ask_by_offset_cents": curve,
            "seconds_to_ceiling": quantiles(to_ceiling)}


def _sources(obs: list[dict], metar: list[dict]) -> dict[str, Any]:
    """P3: publication latency and availability per feed."""
    by_source: dict[str, dict[str, Any]] = {}
    for o in obs:
        b = by_source.setdefault(o["source"], {"ages": [], "stations": {}})
        # Only the FRESHEST observation of each poll measures feed latency. The
        # endpoint returns the last N observations, so the other 23 are backfill
        # whose "age" is just how long ago they happened -- averaging them in
        # reported KNYC latency as ~24 h on the first live run.
        if str(o.get("raw_value") or "").endswith("|newest"):
            b["ages"].append(float(o["publication_age_s"]))
        b["stations"][o["icao"]] = b["stations"].get(o["icao"], 0) + 1
    result = {k: {"publication_age_s": quantiles(v["ages"]),
                  "by_station": v["stations"]}
              for k, v in by_source.items()}
    lags = []
    for m in metar:
        try:
            lags.append((datetime.fromisoformat(m["inserted_at_utc"])
                         - datetime.fromisoformat(m["observed_at_utc"])).total_seconds())
        except (TypeError, ValueError):
            continue
    result["metar_ingest"] = {"publication_age_s": quantiles(lags),
                              "by_station": {}}
    # Which stations serve the 5-minute product at all (KNYC/KDEN do not).
    five = by_source.get("nws5min", {}).get("stations", {})
    hourly = by_source.get("nws_hourly", {}).get("stations", {})
    result["five_minute_available"] = sorted(five)
    result["five_minute_unavailable"] = sorted(set(hourly) - set(five))
    return result


def _disagreement(conn: sqlite3.Connection, since: str) -> dict[str, Any]:
    """P4: does the 5-minute feed agree with the METAR it overlaps?

    Matched by nearest observation within 5 minutes. The 5-minute product is
    quantized to whole degC (1.8 degF) while the METAR carries tenths, so a
    disagreement up to ~0.9 degF is EXPECTED -- the question is whether the tail
    is larger than that, which would mean it can manufacture false crossings.
    """
    five = conn.execute(
        "SELECT icao, observed_at_utc, temp_f FROM source_observations "
        "WHERE source = 'nws5min' AND received_at_utc >= ? ORDER BY observed_at_utc",
        (since,),
    ).fetchall()
    met = conn.execute(
        "SELECT icao, observed_at_utc, temp_f FROM station_logs "
        "WHERE accepted = 1 AND temp_f IS NOT NULL AND inserted_at_utc >= ? "
        "ORDER BY observed_at_utc",
        (since,),
    ).fetchall()
    by_station: dict[str, list[tuple[datetime, float]]] = {}
    for r in met:
        by_station.setdefault(r["icao"], []).append(
            (datetime.fromisoformat(r["observed_at_utc"]), float(r["temp_f"])))

    diffs: list[float] = []
    per_station: dict[str, list[float]] = {}
    for r in five:
        cands = by_station.get(r["icao"])
        if not cands:
            continue
        ts = datetime.fromisoformat(r["observed_at_utc"])
        best = min(cands, key=lambda c: abs((c[0] - ts).total_seconds()))
        if abs((best[0] - ts).total_seconds()) > 300:
            continue
        d = float(r["temp_f"]) - best[1]
        diffs.append(d)
        per_station.setdefault(r["icao"], []).append(d)
    return {
        "matched_pairs": len(diffs),
        "diff_f": quantiles(diffs),
        "abs_diff_f": quantiles([abs(d) for d in diffs]),
        "beyond_quantization": sum(1 for d in diffs if abs(d) > 0.95),
        "by_station": {k: quantiles(v) for k, v in per_station.items()},
    }


def _adverse_selection(events: list[dict], sweeps: dict, slippage_cents: float
                       ) -> dict[str, Any]:
    """P8: is the discount explained by the signal being weak?

    If the biggest discounts sit at the smallest margins, the market is cheap
    because the outcome is genuinely uncertain -- i.e. it is right and we are
    wrong -- and the "edge" is a bet against a correct price. That would invert
    the entire thesis, so it is tested directly rather than assumed away.
    """
    xs, ys, rows = [], [], []
    for e in events:
        sid = e["snapshot_id"]
        if sid is None or (int(sid), 100) not in sweeps:
            continue
        row = sweeps[(int(sid), 100)]
        if row["vwap_cents"] is None:
            continue
        disc = 100.0 - (float(row["vwap_cents"]) + slippage_cents)
        xs.append(disc)
        ys.append(float(e["margin_f"]))
        rows.append((disc, float(e["margin_f"]), e["call_correct"]))
    buckets = {}
    for lo, hi in MARGIN_BUCKETS:
        sel = [r for r in rows if lo <= r[1] < hi]
        graded = [r for r in sel if r[2] is not None]
        buckets[f"[{lo}, {hi})"] = {
            "n": len(sel),
            "discount_cents": quantiles([r[0] for r in sel]),
            "accuracy_pct": (100.0 * sum(int(r[2]) for r in graded) / len(graded)
                             if graded else None),
        }
    return {"n": len(xs), "corr_discount_margin": pearson(xs, ys),
            "by_margin_bucket": buckets}


def _data_quality(conn: sqlite3.Connection, since: str, events: list[dict]
                  ) -> dict[str, Any]:
    """P10: the failures that would quietly bias every number above."""
    def scalar(sql, args=()):
        row = conn.execute(sql, args).fetchone()
        return int(row[0]) if row else 0
    return {
        "events_without_snapshot": sum(1 for e in events if e["snapshot_id"] is None),
        "events_ungraded": sum(1 for e in events if e["call_correct"] is None),
        "metar_rejected": scalar(
            "SELECT COUNT(*) FROM station_logs WHERE accepted = 0 "
            "AND inserted_at_utc >= ?", (since,)),
        "metar_future_dated": scalar(
            "SELECT COUNT(*) FROM station_logs "
            "WHERE julianday(inserted_at_utc) < julianday(observed_at_utc) "
            "AND inserted_at_utc >= ?", (since,)),
        "metar_ingest_over_1h": scalar(
            "SELECT COUNT(*) FROM station_logs WHERE inserted_at_utc >= ? AND "
            "(julianday(inserted_at_utc) - julianday(observed_at_utc)) * 86400 > 3600",
            (since,)),
        "book_snapshots": scalar(
            "SELECT COUNT(*) FROM book_snapshots WHERE received_at_utc >= ?", (since,)),
        "source_observations": scalar(
            "SELECT COUNT(*) FROM source_observations WHERE received_at_utc >= ?",
            (since,)),
        "settlements_without_time": scalar(
            "SELECT COUNT(*) FROM market_results WHERE settled_at_utc IS NULL"),
        # Only freshly-FETCHED samples carry a real round trip. A `signal`
        # snapshot reuses the exact book the decision was priced off (which is
        # the point -- re-fetching would record a different book), so its
        # requested/received stamps are equal and its 0.0 would drag the median
        # to zero. Excluded rather than silently averaged in.
        "round_trip_ms": quantiles([float(r[0]) for r in conn.execute(
            "SELECT round_trip_ms FROM book_snapshots "
            "WHERE received_at_utc >= ? AND reason != 'signal'", (since,))]),
        "signal_snapshots_reused_book": scalar(
            "SELECT COUNT(*) FROM book_snapshots WHERE reason = 'signal' "
            "AND round_trip_ms = 0 AND received_at_utc >= ?", (since,)),
    }


# --------------------------------------------------------------------------- #
# Rendering
# --------------------------------------------------------------------------- #
def render(a: dict[str, Any]) -> str:
    L: list[str] = []
    W = 78
    L.append("=" * W)
    L.append("  STRATEGY-1 DISCOVERY EXPERIMENT -- daily report")
    L.append("=" * W)
    L.append(f"  window {a['window_days']}d since {a['since_utc'][:19]}Z"
             f"   generated {a['generated_utc'][:19]}Z")
    if a["slippage_cents"]:
        L.append(f"  slippage haircut applied: {a['slippage_cents']}c/contract")
    L.append("")

    d = a["detections"]
    L.append("1/2. DETECTIONS")
    L.append(f"    total {d['total']}   qualifying (passed every gate) {d['qualifying']}"
             f"   rejected {d['rejected']}")
    for src, b in sorted(d["by_source"].items()):
        L.append(f"    source {src}: {b['total']}")
        for gate, n in sorted(b["gates"].items(), key=lambda kv: -kv[1]):
            L.append(f"        {gate:<26s} {n}")
    L.append("")

    acc = a["accuracy"]
    L.append("3. CORRECTNESS (every detection graded, tradeable or not)")
    L.append(f"    graded {acc['graded']}   ungraded {acc['ungraded']}"
             f"   correct {acc['correct']}   INCORRECT {acc['incorrect']}"
             + (f"   accuracy {acc['accuracy_pct']:.2f}%"
                if acc["accuracy_pct"] is not None else ""))
    for label, key in (("by margin", "by_margin"), ("by station", "by_station"),
                       ("by side", "by_side"), ("by source", "by_source"),
                       ("by price", "by_price")):
        if acc[key]:
            parts = [f"{k}={v['correct']}/{v['n']}" for k, v in sorted(acc[key].items())]
            L.append(f"    {label:<12s} " + "  ".join(parts))
    for row in acc["incorrect_rows"][:10]:
        L.append(f"      WRONG {row['climate_date']} {row['ticker']} said "
                 f"{row['side']} settled {row['settled_result']} "
                 f"obs {row['observed_tmax_f']} margin {row['margin_f']:+.2f}")
    L.append("")

    ap = a["apparent_edge"]
    L.append("4. APPARENT EDGE -- top-of-book. DO NOT TRADE THIS NUMBER.")
    L.append(f"    quoted {ap['quoted']}   at/above ceiling {ap['at_ceiling_or_above']}"
             f"   discounted {ap['discounted']}"
             + (f" ({ap['discounted_pct']:.1f}%)" if ap["discounted_pct"] is not None else ""))
    L.append(_fmt_q("apparent edge, all (c)", ap["all"]))
    L.append(_fmt_q("apparent edge, discounted (c)", ap["discounted_only"]))
    L.append("")

    ex = a["executable"]
    L.append("5. EXECUTABLE VWAP EDGE -- ceiling-truncated sweep, net of fees")
    L.append(f"    {'size':>6s} {'n':>5s} {'med VWAP':>9s} {'med gross':>10s}"
             f" {'med net $':>10s} {'total $':>10s} {'med fill':>9s} {'% asked':>8s}")
    for size in REPORT_SIZES:
        p = ex["per_size"][str(size)]
        if not p["events_with_fill"]:
            L.append(f"    {size:>6d} {0:>5d}        (no fills)")
            continue
        L.append(f"    {size:>6d} {p['events_with_fill']:>5d}"
                 f" {p['vwap_cents']['median']:>9.2f}"
                 f" {p['gross_edge_cents']['median']:>10.2f}"
                 f" {p['net_usd']['median']:>10.2f}"
                 f" {p['total_net_usd']:>10.2f}"
                 f" {p['fillable_size']['median']:>9.0f}"
                 f" {p['pct_of_requested']['median']:>7.1f}%")
    L.append(_fmt_q("slippage at 100 (VWAP-top, c)", ex["slippage_at_100_cents"]))
    L.append("")

    dp = a["depth"]
    L.append("6. DEPTH (contracts available at or below the ceiling)")
    L.append(_fmt_q("at signal", dp["at_signal"]))
    L.append(_fmt_q("control (no signal)", dp["control"]))
    L.append(f"    control samples: {dp['control_samples']}")
    L.append("")

    rp = a["repricing"]
    L.append("7. REPRICING (ask on the traded side, by seconds after detection)")
    L.append(f"    events with offsets: {rp['events']}")
    for off, q in rp["ask_by_offset_cents"].items():
        if q["n"]:
            L.append(f"      t+{off:>5s}s  n={q['n']:<4d} median {q['median']:>6.1f}c"
                     f"  mean {q['mean']:>6.1f}c")
    L.append(_fmt_q("seconds to reach 98c", rp["seconds_to_ceiling"], "s"))
    L.append("")

    # _render_availability owns the 5-minute availability lines now.
    L.extend(_render_availability(a["sources"], a["feed_availability"]))
    L.append("")

    dg = a["disagreement"]
    L.append("9. NWS 5-MINUTE vs METAR (quantization is 1.8F, so |diff|<=0.9 is expected)")
    L.append(f"    matched pairs {dg['matched_pairs']}"
             f"   beyond quantization (|diff|>0.95F): {dg['beyond_quantization']}")
    L.append(_fmt_q("signed diff (F)", dg["diff_f"]))
    L.append(_fmt_q("absolute diff (F)", dg["abs_diff_f"]))
    L.append("")

    adv = a["adverse_selection"]
    L.append("10. ADVERSE SELECTION -- is the discount just a weak signal?")
    r = adv["corr_discount_margin"]
    L.append(f"    corr(discount, margin_f) = "
             + (f"{r:+.3f}" if r is not None else "n/a")
             + f"   (n={adv['n']})")
    L.append("    NEGATIVE and large means the cheapest windows are the least certain.")
    for k, v in adv["by_margin_bucket"].items():
        if v["n"]:
            L.append(f"      margin {k:<14s} n={v['n']:<4d}"
                     f" median discount {v['discount_cents']['median']:>6.2f}c"
                     + (f"  accuracy {v['accuracy_pct']:.1f}%"
                        if v["accuracy_pct"] is not None else "  accuracy n/a"))
    L.append("")

    q = a["data_quality"]
    L.append("11. DATA QUALITY")
    for k in ("events_without_snapshot", "events_ungraded", "metar_rejected",
              "metar_future_dated", "metar_ingest_over_1h", "book_snapshots",
              "source_observations", "settlements_without_time",
              "signal_snapshots_reused_book"):
        L.append(f"    {k:<28s} {q[k]}")
    L.append(_fmt_q("book round-trip (ms)", q["round_trip_ms"], "ms"))
    L.append("")
    L.extend(_render_liquidity(a["liquidity_timing"]))
    L.append("=" * W)
    return "\n".join(L)


def _render_liquidity(lt: dict[str, Any]) -> list[str]:
    """Panel 12: when the sure side's liquidity actually disappears."""
    L = ["12. LIQUIDITY TIMING -- does the sure side vanish before, at, or after T0?"]
    L.append(f"    events {lt['events']}   'meaningful' = {lt['meaningful_size']}"
             f" contracts sweepable at or below the ceiling")
    L.append("    classification (each label maps to one hypothesis):")
    for label, n in sorted(lt["classification_counts"].items(), key=lambda kv: -kv[1]):
        L.append(f"      {label:<36s} {n:>4d}   {LABEL_TO_HYPOTHESIS.get(label, '')}")
    c = lt["cohorts"]
    L.append(f"    cohorts at T0: empty {c['empty_at_t0']}"
             f"   non-empty {c['nonempty_at_t0']}   unmatched {c['t0_unmatched']}")
    L.append("")

    L.append("    LIQUIDITY DECAY CURVE -- sure-side sweepable depth by offset")
    L.append("    (coverage% is the share of events with a snapshot in tolerance;")
    L.append("     a low median at low coverage is a SAMPLING hole, not an absence)")
    for cohort in ("all", "empty_at_t0", "nonempty_at_t0"):
        curve = lt["decay_curve"][cohort]
        L.append(f"      cohort: {cohort}")
        L.append(f"        {'offset':>8s} {'cov%':>6s} {'n':>4s} {'p10':>8s}"
                 f" {'median':>9s} {'p90':>9s} {'empty%':>7s} {'|err|s':>7s}")
        for off in OFFSETS_MINUTES:
            d = curve[str(off)]
            if not d["matched"]:
                L.append(f"        {f'T{off:+.0f}m':>8s} {0.0:>5.1f}% {0:>4d}"
                         f"        (no snapshot in tolerance)")
                continue
            dep = d["sweepable_depth"]
            L.append(f"        {f'T{off:+.0f}m':>8s} {d['coverage_pct']:>5.1f}%"
                     f" {d['matched']:>4d} {dep['p10']:>8.0f} {dep['median']:>9.0f}"
                     f" {dep['p90']:>9.0f} {d['sure_side_empty_pct']:>6.1f}%"
                     f" {d['match_error_s']['median']:>7.0f}")
    L.append("")

    le = lt["last_executable"]
    L.append("    LAST EXECUTABLE MOMENT before T0 (minutes before the lock-in)")
    L.append(f"      any positive ask : {le['events_with_any_positive']} events"
             + (f", median {le['any_positive_minutes_before']['median']:.1f} min before"
                if le["any_positive_minutes_before"]["n"] else ""))
    L.append(f"        {'size':>6s} {'events':>7s} {'ever avail':>11s}"
             f" {'median min before':>18s} {'p90':>8s}")
    for t in AVAILABILITY_THRESHOLDS:
        b = le["by_threshold"][str(t)]
        m = b["minutes_before_lockin"]
        L.append(f"        {t:>6d} {b['events_with_any']:>7d}"
                 f" {b['pct_ever_available']:>10.1f}%"
                 + (f" {m['median']:>18.1f} {m['p90']:>8.1f}" if m["n"]
                    else f" {'--':>18s} {'--':>8s}"))
    L.append("")

    mc = lt["margin_correlation"]
    r = mc["corr_margin_vs_depth_at_t0"]
    L.append("    IS THE DISAPPEARANCE CORRELATED WITH THE WEATHER MARGIN?")
    L.append(f"      corr(margin_f, depth at T0) = "
             + (f"{r:+.3f}" if r is not None else "n/a") + f"   (n={mc['n']})")
    for k, v in mc["by_margin_bucket"].items():
        if v["n"]:
            L.append(f"        margin {k:<14s} n={v['n']:<4d}"
                     f" median depth {v['depth_at_t0']['median']:>8.0f}")
    L.append("")

    ev = lt["edge_vs_liquidity"]
    r2 = ev["corr_apparent_edge_vs_depth"]
    L.append("    IS THE APPARENT (top-of-book) EDGE BACKED BY SIZE?")
    L.append(f"      quoted at T0 {ev['quoted_at_t0']}   corr(apparent edge, depth) = "
             + (f"{r2:+.3f}" if r2 is not None else "n/a"))
    L.append(_fmt_q("apparent edge WITH depth (c)", ev["apparent_edge_with_depth"]))
    L.append(_fmt_q("apparent edge with NO depth (c)", ev["apparent_edge_without_depth"]))
    L.append("")

    wl = lt["weather_lead"]
    L.extend(_render_lead_and_liquidity(wl))
    return L


def _lead_triplet(wl: dict[str, Any], *, indent: str) -> list[str]:
    """The lead, decomposed into the three things that actually move it.

    They are reported together because the difference between them IS the
    finding: an observation lead is eroded first by delivery delay (concept 1)
    and then by when the engine happens to fire.

        _observation  observation VALID times            pure weather
        _receipt      when we HELD each observation      observation - delivery
        _detection    when the engine fired              also engine behaviour

    The shadow path fires on a NEW HIGH rather than on first crossing, so
    _detection is a LOWER bound on the lead and must not be quoted as the feed's
    performance.
    """
    L = [f"{indent}three measures over VALID pairs. Their DIFFERENCES are the finding:"]
    for key, label, note in (
        ("observation", "_observation", "pure weather"),
        ("receipt", "_receipt    ", "= observation - delivery delay (metric 1)"),
        ("detection", "_detection  ", "also carries engine firing behaviour"),
    ):
        q = wl[f"nws_lockin_lead_vs_metar_seconds_{key}"]
        if q["n"]:
            L.append(f"{indent}  {label}: n={q['n']:<3d} median {q['median']:>8.0f}s"
                     f" ({q['median'] / 60:>6.1f} min)  p10 {q['p10']:>8.0f}s"
                     f"  p90 {q['p90']:>8.0f}s   {note}")
        else:
            L.append(f"{indent}  {label}: none yet   {note}")
    obs = wl["nws_lockin_lead_vs_metar_seconds_observation"]
    rec = wl["nws_lockin_lead_vs_metar_seconds_receipt"]
    if obs["n"] and rec["n"]:
        L.append(f"{indent}  delivery cost = observation - receipt ="
                 f" {obs['median'] - rec['median']:.0f}s"
                 f" ({(obs['median'] - rec['median']) / 60:.1f} min) at the median")
    if wl["pairs_with_negative_lead_among_valid"]:
        L.append(f"{indent}  ⚠ {wl['pairs_with_negative_lead_among_valid']} VALID pairs"
                 " have a NEGATIVE receipt lead. Not a restart artefact:")
        L.append(f"{indent}    the 5-minute feed OBSERVES sooner but is DELIVERED"
                 " later, and when the")
        L.append(f"{indent}    observation lead is small the delivery gap dominates"
                 " and flips the sign.")
    return L


def _render_availability(sr: dict[str, Any], fa: dict[str, Any]) -> list[str]:
    """Panel 8: observed availability delay -- NOT publication latency.

    Deliberately leads with min/max/n rather than a median. Measurement on
    2026-08-26 showed the delay is gated by a SHARED cache whose phase we neither
    control nor observe, so its central tendency describes that cache's traffic,
    not the feed. The minimum is the only figure that bounds anything real.
    """
    L = ["8. observed_availability_delay_seconds -- FEED ONLY"]
    L.append("    = our receipt clock minus the observation's VALID time, on the")
    L.append("      freshest observation of each poll.")
    L.append("    ⚠ " + AVAILABILITY_DISCLAIMER.replace(". ", ".\n        "))
    L.append("    ⚠ The API exposes NO publication, update or modification timestamp.")
    L.append("      Only `Date` (response generation) and per-observation `timestamp`")
    L.append("      (valid time) exist. No quantity here is publication latency.")
    L.append("")
    for src in sorted(k for k in sr
                      if isinstance(sr[k], dict) and "publication_age_s" in sr[k]):
        q = sr[src]["publication_age_s"]
        if not q["n"]:
            L.append(f"    {src:<22s} (no data)")
            continue
        L.append(f"    {src}")
        L.append(f"      n={q['n']}   MIN {q['min']:.0f}s ({q['min']/60:.1f} min)"
                 f"   MAX {q['max']:.0f}s ({q['max']/60:.1f} min)"
                 f"   spread {q['max']-q['min']:.0f}s")
        L.append(f"      percentiles: p25 {q['p25']:.0f}s  p50 {q['median']:.0f}s"
                 f"  p75 {q['p75']:.0f}s  p90 {q['p90']:.0f}s")
        L.append(f"      -> read the MIN ({q['min']:.0f}s); the median describes"
                 " cache traffic, not the feed")
    L.append("")
    L.append(f"    CACHE BEHAVIOUR (measured {PROBE_DATE}, one-off probe:"
             f" {', '.join(PROBE_STATIONS)},")
    L.append(f"    {PROBE_SPAN_MIN:.0f} min at {PROBE_RESOLUTION_S}s resolution."
             " NOT live -- the collector stores no headers.)")
    L.append(f"      cache TTL at reset (max-age): {CACHE_TTL_OBSERVED_S[0]}-"
             f"{CACHE_TTL_OBSERVED_S[1]}s, median {CACHE_TTL_MEDIAN_S}s;"
             " counts down 1:1 with time")
    L.append(f"      newest-observation advances: {PROBE_ADVANCES_CACHE_GATED} of "
             f"{PROBE_ADVANCES_OBSERVED} coincided with a cache-TTL RESET")
    L.append("        -> the newest observation NEVER advanced mid-TTL")
    L.append(f"      probe observed_to_first_available: "
             f"{PROBE_AVAILABILITY_DELAY_S[0]}-{PROBE_AVAILABILITY_DELAY_S[1]}s"
             f" (median {PROBE_AVAILABILITY_MEDIAN_S}s, n={PROBE_ADVANCES_OBSERVED})")
    L.append("      client Cache-Control: no-cache / max-age=0 do NOT bypass it;"
             " a ?_cb= param is HTTP 400")
    L.append("")
    L.append("    DROPPED 5-MINUTE SLOTS -- never exposed. NOT counted as 'late'.")
    L.append(f"      expected {fa['expected_slots']}   present {fa['present']}"
             f"   DROPPED {fa['dropped_never_exposed']}"
             + (f" ({fa['dropped_pct']:.1f}%)" if fa["dropped_pct"] is not None else "")
             + f"   downtime {fa['downtime_slots']}")
    L.append("      A dropped slot has no row at all: UNIQUE(icao, source,")
    L.append("      observed_at_utc) means anything the API exposed while we polled is")
    L.append("      stored. 'downtime' is separated out -- we were not looking.")
    if fa["affected"]:
        for d in fa["affected"]:
            if not d["dropped"]:
                continue
            L.append(f"        {d['icao']} {d['climate_date']}"
                     f"  dropped {d['dropped']}/{d['expected_slots']}"
                     f"  ever subsequently exposed: {d['ever_subsequently_exposed']}")
            L.append(f"          slots: {[x[11:16] for x in d['dropped_slots_utc'][:10]]}")
    else:
        L.append("      (none)")
    L.append(f"    5-minute available   : {', '.join(sr['five_minute_available']) or '(none)'}")
    L.append(f"    5-minute UNAVAILABLE : {', '.join(sr['five_minute_unavailable']) or '(none)'}")
    return L


def _render_lead_and_liquidity(wl: dict[str, Any]) -> list[str]:
    """Concepts (2) and (3), rendered as two separate blocks on purpose.

    A weather lead and the liquidity at that instant answer different questions
    and can point opposite ways: a 40-minute lead into an empty book is worth
    nothing. They are never combined into a single figure.
    """
    L = ["    (2) nws_lockin_lead_vs_metar_seconds -- WEATHER ONLY, no market in it"]
    L.append(f"      paired {wl['paired_events']}"
             f"   valid {wl['valid_lead_measurements']}"
             f"   insufficient_history {wl['insufficient_history']}"
             f"   (metar-only {wl['unpaired']['metar_only']},"
             f" nws5min-only {wl['unpaired']['nws5min_only']})")
    L.extend(_lead_triplet(wl, indent="      "))
    if wl["insufficient_history"]:
        L.append(f"      insufficient_history ({wl['insufficient_history']}): the two"
                 " intraday histories are not comparable")
        L.append("        for that station-day, so no lead is claimed either way."
                 " Most often a")
        L.append("        mid-day restart, which leaves the 5-minute running max"
                 " under-seeded.")
        for p in wl["insufficient_history_pairs"][:5]:
            h = p["history"]
            fails = [k for k in ("starts_comparable", "observed_before_either",
                                 "cadence_ok", "uncontaminated") if not h[k]]
            L.append(f"          {p['climate_date']} {p['icao']:<5s}"
                     f" nws_obs={h['nws_observations']:<5d}"
                     f" metar_obs={h['metar_observations']:<4d}"
                     f" ratio="
                     + (f"{h['cadence_ratio']:.1f}" if h["cadence_ratio"] else "n/a")
                     + f"  failed: {', '.join(fails)}")
    L.append("")

    L.append("    (3) liquidity_at_nws_lockin vs liquidity_at_metar_lockin -- MARKET ONLY")
    L.append("        (executable sure-side depth at or below the ceiling; a long")
    L.append("         lead into an empty book is worth nothing, hence the split)")
    L.append(_fmt_q("liquidity_at_nws_lockin", wl["liquidity_at_nws_lockin"]))
    L.append(_fmt_q("liquidity_at_metar_lockin", wl["liquidity_at_metar_lockin"]))
    L.append(f"      events where NWS had depth and METAR did not: "
             f"{wl['events_where_nws_had_depth_and_metar_did_not']}"
             f"  <- the only number that would justify a faster feed")
    return L


def render_gate0(a: dict[str, Any]) -> str:
    """The ten Gate-0 questions, answered from data or marked unanswerable."""
    acc, ex, dp, rp, adv, dg = (a["accuracy"], a["executable"], a["depth"],
                                a["repricing"], a["adverse_selection"],
                                a["disagreement"])
    p100 = ex["per_size"]["100"]
    L = ["=" * 78, "  GATE 0 REPORT", "=" * 78]

    def answer(n, q, val, verdict):
        L.append(f"  {n}. {q}")
        L.append(f"     {val}")
        L.append(f"     -> {verdict}")

    answer(1, "Is the corrected deterministic rule reliable?",
           f"{acc['correct']}/{acc['graded']} correct"
           + (f" ({acc['accuracy_pct']:.2f}%)" if acc["accuracy_pct"] is not None else ""),
           "PASS" if acc["graded"] >= 300 and acc["incorrect"] == 0
           else "INSUFFICIENT DATA" if acc["graded"] < 300 else "FAIL")
    five = a["sources"].get("five_minute_available", [])
    answer(2, "Does an earlier source identify the signal materially earlier?",
           f"5-minute product available at {len(five)} stations: {', '.join(five) or 'none'}",
           "see the source-latency panel; needs paired t1/t2 detections to conclude")
    answer(3, "Is there meaningful displayed depth at the signal?",
           f"median {dp['at_signal']['median']} contracts (control "
           f"{dp['control']['median']})" if dp["at_signal"]["n"] else "no data",
           "PASS" if (dp["at_signal"]["median"] or 0) >= 50 else "FAIL/INSUFFICIENT")
    answer(4, "Is that depth available at a profitable VWAP?",
           f"median net at 100 contracts: "
           + (f"${p100['net_usd']['median']:.2f}" if p100["events_with_fill"] else "no fills"),
           "PASS" if p100["events_with_fill"] and (p100["net_usd"]["median"] or 0) >= 3.0
           else "FAIL/INSUFFICIENT")
    answer(5, "Does the discount survive fees and realistic execution?",
           f"median slippage {ex['slippage_at_100_cents']['median']}c; "
           f"total net ${p100['total_net_usd']:.2f}"
           if p100["events_with_fill"] else "no fills",
           "see panel 5")
    r = adv["corr_discount_margin"]
    answer(6, "Does the discount disappear because of adverse selection?",
           f"corr(discount, margin) = " + (f"{r:+.3f}" if r is not None else "n/a"),
           "PASS" if r is not None and r > -0.15
           else "FAIL" if r is not None and r < -0.30 else "INSUFFICIENT DATA")
    answer(7, "How quickly does the market reprice?",
           f"median seconds to 98c: {rp['seconds_to_ceiling']['median']}"
           if rp["seconds_to_ceiling"]["n"] else "no data",
           "PASS" if (rp["seconds_to_ceiling"]["median"] or 0) >= 600 else "FAIL/INSUFFICIENT")
    answer(8, "Is the opportunity repeatable across stations and dates?",
           f"stations with detections: {len(acc['by_station'])}", "see panel 3")
    answer(9, "Conservative expected value per event?",
           f"p25 net at 100 contracts: "
           + (f"${p100['net_usd']['p25']:.2f}" if p100["events_with_fill"] else "no fills"),
           "PASS" if p100["events_with_fill"] and (p100["net_usd"]["p25"] or 0) >= 1.0
           else "FAIL/INSUFFICIENT")
    answer(10, "Maximum plausible capital deployment?",
           f"median fillable at ceiling: {dp['at_signal']['median']} contracts"
           if dp["at_signal"]["n"] else "no data",
           "multiply by median VWAP for dollars at risk per event")
    L.append("=" * 78)
    L.extend(_gate0_liquidity(a["liquidity_timing"]))
    L.append("=" * 78)
    L.append("  Any INSUFFICIENT DATA verdict means the experiment is not finished.")
    L.append("  Do not proceed to paper trading on a partial Gate 0.")
    L.append("=" * 78)
    return "\n".join(L)


def _gate0_liquidity(lt: dict[str, Any]) -> list[str]:
    """Gate 0, liquidity-timing half: is Strategy 1 too LATE or simply INVALID?

    These six are scored separately from the ten above because they can fail for
    a different reason. A strategy that is too late is fixable with a faster
    observation; one whose sure side is never quoted is not fixable at all.
    """
    counts = lt["classification_counts"]
    total = lt["events"]
    known = total - counts.get("UNKNOWN_INSUFFICIENT_SNAPSHOTS", 0)
    le = lt["last_executable"]
    wl = lt["weather_lead"]
    ev = lt["edge_vs_liquidity"]
    mc = lt["margin_correlation"]
    L = ["", "  GATE 0 -- LIQUIDITY TIMING", "-" * 78]

    def answer(n, q, val, verdict):
        L.append(f"  {n}. {q}")
        L.append(f"     {val}")
        L.append(f"     -> {verdict}")

    if known == 0:
        L.append("  NOT MEASURABLE YET: no event has both a T0 and a pre-lock-in")
        L.append("  snapshot inside tolerance. Nothing below is claimed.")
        L.append("-" * 78)
        return L

    ever = le["by_threshold"]["1"]
    ever_100 = le["by_threshold"]["100"]
    answer(1, "Is there executable liquidity BEFORE deterministic lock-in?",
           f"{ever['events_with_any']}/{ever['events_total']} events had >=1 contract "
           f"at some point pre-lock-in ({ever['pct_ever_available']:.0f}%); "
           f">=100 contracts: {ever_100['pct_ever_available']:.0f}%",
           "YES" if (ever["pct_ever_available"] or 0) >= 50 else
           "NO -- the sure side is essentially never quoted before the signal"
           if (ever["pct_ever_available"] or 0) < 10 else "PARTIAL")

    m = le["any_positive_minutes_before"]
    answer(2, "How many minutes before lock-in does liquidity disappear?",
           (f"median {m['median']:.1f} min before T0 "
            f"(p10 {m['p10']:.1f}, p90 {m['p90']:.1f}, n={m['n']})"
            if m["n"] else "no event had any positive ask pre-lock-in"),
           "the window a faster feed would have to beat" if m["n"]
           else "there is no window to beat -- see question 6")

    lead = wl["nws_lockin_lead_vs_metar_seconds_receipt"]
    answer(3, "Does the NWS 5-minute feed arrive early enough to observe it?",
           (f"nws_lockin_lead_vs_metar_seconds_receipt median {lead['median']:.0f}s "
            f"({lead['median'] / 60:.1f} min) over {lead['n']} VALID pairs; "
            f"{wl['insufficient_history']} classified insufficient_history; "
            f"liquidity_at_nws_lockin > liquidity_at_metar_lockin in "
            f"{wl['events_where_nws_had_depth_and_metar_did_not']}"
            if lead["n"] else
            f"no VALID lead measurements yet "
            f"({wl['paired_events']} paired, {wl['insufficient_history']} "
            f"insufficient_history, metar-only {wl['unpaired']['metar_only']}, "
            f"5min-only {wl['unpaired']['nws5min_only']})"),
           "YES -- the earlier signal reaches live liquidity"
           if wl["events_where_nws_had_depth_and_metar_did_not"] > 0
           else "NOT DEMONSTRATED -- an earlier signal into an empty book buys nothing"
           if lead["n"] else "INSUFFICIENT DATA -- no comparable intraday histories yet")

    r = mc["corr_margin_vs_depth_at_t0"]
    answer(4, "Is the disappearance correlated with the weather margin?",
           (f"corr(margin_f, depth at T0) = {r:+.3f} (n={mc['n']})"
            if r is not None else f"not computable (n={mc['n']})"),
           "depth collapses hardest at SMALL margins -- consistent with an "
           "informed book, not a slow one" if r is not None and r > 0.3
           else "no clear relationship" if r is not None else "INSUFFICIENT DATA")

    r2 = ev["corr_apparent_edge_vs_depth"]
    wd, nd = ev["apparent_edge_with_depth"], ev["apparent_edge_without_depth"]

    def _med(q: dict[str, Any]) -> str:
        """A median only exists when the bucket does. Empty is 'n/a', not 0.

        This crashed once formatting a None median: one bucket can be empty while
        the other has data, so the guard has to be per-bucket, not on their sum.
        Printing 0.0 instead would invent an edge of zero cents where there was
        no observation at all.
        """
        return f"{q['median']:.1f}c" if q["n"] else "n/a"

    answer(5, "Is the apparent (17.4c-style) edge associated with executable liquidity?",
           (f"apparent edge median {_med(wd)} WITH depth (n={wd['n']}) vs "
            f"{_med(nd)} with NONE (n={nd['n']}); "
            f"corr = " + (f"{r2:+.3f}" if r2 is not None else "n/a"))
           if (wd["n"] or nd["n"]) else "no quoted sure side at T0 yet",
           "the discount is backed by size" if wd["n"] and (wd["median"] or 0) > 0
           and nd["n"] == 0 else
           "a cheap sure side is largely an UNQUOTED one -- the historical "
           "top-of-book edge would not have been executable"
           if nd["n"] > wd["n"] else "INSUFFICIENT DATA")

    too_late = (counts.get("LIQUID_BEFORE_LOCKIN", 0)
                + counts.get("LIQUIDITY_DISAPPEARS_BEFORE_LOCKIN", 0))
    never = counts.get("ALWAYS_EMPTY", 0) + counts.get("ALWAYS_THIN", 0)
    survives = counts.get("LIQUID_UNTIL_LOCKIN", 0)
    answer(6, "Is Strategy 1 too LATE by construction, or simply INVALID?",
           f"of {known} measurable events: too-late {too_late}, "
           f"never-there {never}, survives {survives}",
           "TOO LATE -- a faster observation is the lever"
           if too_late > never and too_late > survives else
           "INVALID -- the sure side is not quoted at all, and no feed fixes that"
           if never > too_late and never > survives else
           "REACHABLE -- liquidity survives the lock-in"
           if survives >= max(too_late, never) else "MIXED")
    L.append("-" * 78)
    L.append(f"  Classification boundaries and per-offset tolerances are printed in")
    L.append(f"  panel 12; nothing here is interpolated across a sampling gap.")
    return L


# --------------------------------------------------------------------------- #
# Watchlist -- the seven measurements being tracked through the 21-day run
# --------------------------------------------------------------------------- #
def render_watchlist(a: dict[str, Any]) -> str:
    """The standing watch view. Read-only; changes nothing.

    Deliberately narrow: seven numbers, each with the caveat that currently
    limits it, so a glance cannot turn a sampling artefact into a finding.
    """
    lt = a["liquidity_timing"]
    det, acc, sr = a["detections"], a["accuracy"], a["sources"]
    curve = lt["decay_curve"]["all"]
    le = lt["last_executable"]
    wl = lt["weather_lead"]
    W = 78
    L = ["=" * W, "  WATCHLIST -- 21-day Strategy-1 discovery experiment", "=" * W]
    L.append(f"  window {a['window_days']}d   generated {a['generated_utc'][:19]}Z")
    L.append("")
    L.append("  ⚠ STANDING CAVEAT. The 2026-08-26 reading of ALWAYS_EMPTY 10/10 is")
    L.append("    NOT evidence. n=10, all end-of-day already-decided markets, and a")
    L.append("    pre-window only ~5 minutes deep (the tracker had just restarted),")
    L.append("    so T-10/-15/-30 coverage was 0%. It is a placeholder, not a result.")
    L.append("")

    L.append("  1. LOCK-IN EVENTS")
    L.append(f"       total {det['total']}   qualifying {det['qualifying']}"
             f"   rejected {det['rejected']}")
    for src, b in sorted(det["by_source"].items()):
        L.append(f"       {src:<10s} {b['total']:>5d}")
    L.append(f"       graded {acc['graded']}  correct {acc['correct']}"
             f"  INCORRECT {acc['incorrect']}"
             + (f"  ({acc['accuracy_pct']:.2f}%)" if acc["accuracy_pct"] is not None else ""))
    L.append("")

    L.append("  2. L2 COVERAGE at the offsets that matter")
    L.append(f"       {'offset':>8s} {'coverage':>9s} {'matched':>8s} {'|err| med':>10s}")
    for off in WATCH_OFFSETS:
        d = curve[str(off)]
        cov = d["coverage_pct"]
        err = d["match_error_s"]["median"]
        L.append(f"       {f'T{off:+.0f}m':>8s} "
                 + (f"{cov:>8.1f}%" if cov is not None else f"{'--':>9s}")
                 + f" {d['matched']:>8d} "
                 + (f"{err:>9.0f}s" if err is not None else f"{'--':>10s}"))
    L.append("       (T-10/-15/-30 remain sparse by design -- armed sampling starts")
    L.append("        within 3F of the bound. NOT widened; revisit on a real sample.)")
    L.append("")

    L.append("  3. POSITIVE SURE-SIDE ASK DEPTH BEFORE LOCK-IN")
    ever = le["by_threshold"]["1"]
    L.append(f"       events with ANY positive ask pre-lock-in: "
             f"{ever['events_with_any']}/{ever['events_total']}"
             + (f"  ({ever['pct_ever_available']:.1f}%)"
                if ever["pct_ever_available"] is not None else ""))
    for off in WATCH_OFFSETS:
        d = curve[str(off)]
        if d["matched"]:
            L.append(f"       T{off:+.0f}m median depth {d['sweepable_depth']['median']:>8.0f}"
                     f"   empty {d['sure_side_empty_pct']:.0f}% of {d['matched']}")
    L.append("")

    L.append("  4. LAST EXECUTABLE MOMENT (minutes before lock-in)")
    L.append(f"       {'size':>6s} {'events':>7s} {'ever':>7s} {'median':>8s} {'p90':>8s}")
    for t in AVAILABILITY_THRESHOLDS:
        b = le["by_threshold"][str(t)]
        m = b["minutes_before_lockin"]
        L.append(f"       {t:>6d} {b['events_with_any']:>7d}"
                 f" {b['pct_ever_available']:>6.1f}%"
                 + (f" {m['median']:>8.1f} {m['p90']:>8.1f}" if m["n"]
                    else f" {'--':>8s} {'--':>8s}"))
    L.append("")

    L.append("  5. nws_lockin_lead_vs_metar_seconds -- WEATHER ONLY")
    L.append(f"       paired {wl['paired_events']}"
             f"   valid {wl['valid_lead_measurements']}"
             f"   insufficient_history {wl['insufficient_history']}"
             f"   metar-only {wl['unpaired']['metar_only']}"
             f"   5min-only {wl['unpaired']['nws5min_only']}")
    L.extend(_lead_triplet(wl, indent="       "))
    if wl["insufficient_history"]:
        L.append(f"       {wl['insufficient_history']} pair(s) classified"
                 " insufficient_history (NOT negative lead):")
        L.append("         the two intraday histories are not comparable for that")
        L.append("         station-day -- usually a mid-day restart leaving the")
        L.append("         5-minute running max under-seeded. No lead is claimed.")
    L.append("")

    L.append("  6. liquidity_at_nws_lockin vs liquidity_at_metar_lockin -- MARKET ONLY")
    L.append(_fmt_q("liquidity_at_nws_lockin", wl["liquidity_at_nws_lockin"]))
    L.append(_fmt_q("liquidity_at_metar_lockin", wl["liquidity_at_metar_lockin"]))
    L.append(f"       events where NWS had depth and METAR did not: "
             f"{wl['events_where_nws_had_depth_and_metar_did_not']}")
    L.append("         <- the ONLY number that would justify a faster feed")
    L.append("")

    L.append("  7. observed_availability_delay_seconds -- FEED ONLY")
    L.append("       NOT publication latency. The API exposes no publication, update")
    L.append("       or modification timestamp -- only `Date` (response generation)")
    L.append("       and per-observation `timestamp` (valid time).")
    L.append("       Cache-influenced and cache-phase is SHARED and uncontrolled, so")
    L.append("       this is not a stable feed characteristic. READ THE MINIMUM.")
    for src in ("nws5min", "nws_hourly", "metar_ingest"):
        d = sr.get(src)
        if not d or not d["publication_age_s"]["n"]:
            continue
        q = d["publication_age_s"]
        L.append(f"       {src:<14s} n={q['n']:<5d} MIN {q['min']:>6.0f}s"
                 f"  MAX {q['max']:>6.0f}s  p25 {q['p25']:>6.0f}s"
                 f"  p50 {q['median']:>6.0f}s  p75 {q['p75']:>6.0f}s")
    L.append(f"       cache TTL {CACHE_TTL_OBSERVED_S[0]}-{CACHE_TTL_OBSERVED_S[1]}s;"
             f" {PROBE_ADVANCES_CACHE_GATED}/{PROBE_ADVANCES_OBSERVED} advances were"
             " cache-reset gated"
             f" (probe {PROBE_DATE})")
    fa = a["feed_availability"]
    L.append(f"       DROPPED 5-min slots (never exposed, NOT late):"
             f" {fa['dropped_never_exposed']}/{fa['expected_slots']}"
             + (f" ({fa['dropped_pct']:.1f}%)" if fa["dropped_pct"] is not None else "")
             + f"; downtime {fa['downtime_slots']}")
    for d in fa["affected"]:
        if d["dropped"]:
            L.append(f"         {d['icao']} {d['climate_date']}: {d['dropped']} dropped,"
                     f" ever subsequently exposed = {d['ever_subsequently_exposed']}")
    L.append(f"       5-minute available   : {', '.join(sr['five_minute_available']) or '(none)'}")
    L.append(f"       5-minute UNAVAILABLE : {', '.join(sr['five_minute_unavailable']) or '(none)'}")
    L.append("")
    L.append("  ⚠ On any day the tracker RESTARTS mid-day, the 5-minute running max is")
    L.append("    under-seeded: it lives in memory and backfills only ~2 h, while")
    L.append("    daily_tmax is DB-backed and complete. Paired events on restart days")
    L.append("    are therefore biased AGAINST the 5-minute source. Full days are clean.")
    L.append("=" * W)
    return "\n".join(L)


def main() -> int:
    settings = Settings.from_env()
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", default=settings.db_path)
    ap.add_argument("--days", type=float, default=1.0)
    ap.add_argument("--slippage-cents", type=float, default=0.0,
                    help="explicit extra haircut per contract on the VWAP")
    ap.add_argument("--latency-seconds", type=float, default=0.0,
                    help="reserved: report the offset panel to see decay instead")
    ap.add_argument("--json", help="write the full analysis as JSON")
    ap.add_argument("--gate0", action="store_true", help="also print the Gate 0 report")
    ap.add_argument("--watchlist", action="store_true",
                    help="print only the seven standing measurements")
    ap.add_argument("--meaningful-size", type=int, default=DEFAULT_MEANINGFUL_SIZE,
                    help="contracts at/below the ceiling for a book to count as "
                         "MEANINGFUL in the liquidity classification")
    args = ap.parse_args()

    a = analyse(args.db, days=args.days, latency_seconds=args.latency_seconds,
                slippage_cents=args.slippage_cents,
                meaningful_size=args.meaningful_size)
    if args.watchlist:
        print(render_watchlist(a))
    else:
        print(render(a))
    if args.gate0:
        print()
        print(render_gate0(a))
    if args.json:
        with open(args.json, "w") as fh:
            json.dump(a, fh, indent=1, default=str)
        print(f"\n  wrote {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
