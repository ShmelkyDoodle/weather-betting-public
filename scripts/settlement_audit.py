#!/usr/bin/env python3
"""Audit Kalshi's settlement against its stated basis, and price the risk to Strategy 1.

WHY THIS EXISTS.  ``weather_arb.settlement_basis`` answers "which basis does Kalshi
settle on" with a single aggregate percentage.  That is the right question for the
sigma floor and the wrong one for Strategy 1, which does not care about the *rate*
of agreement -- it cares about the *direction* of every disagreement.

Strategy 1 buys the sure side at up to 98c.  Its proof has exactly two premises:

  1. the final max is >= the running max we observed  (monotonicity -- definitional);
  2. Kalshi settles on ``round(final true max)``      (the basis claim).

Premise 2 is the one that can fail, and when it fails the direction decides
everything.  Every certainty branch in ``WeatherMarket.deterministic_lock_in``
proves its bucket NO (or a tail YES) by showing the settlement must be *at or
above* a boundary::

    between [F, F+1]  certain NO   needs  m >= F+1.5  =>  round(max) >= F+2
    less    cap C     certain NO   needs  m >= C-0.5  =>  round(max) >= C
    greater floor F   certain YES  needs  m >= F+0.5  =>  round(max) >= F+1

A settlement that comes in *higher* than the truth leaves all three conclusions
intact.  A settlement that comes in *lower* breaks all three.  So the question is
not "how often does Kalshi disagree with the CLI" but "how often does Kalshi settle
BELOW what we observed", and the answer to the first does not bound the second.

KNOWN PRIOR.  Two failures are already documented (``CROSS-VENUE-STEP1.md``):
MIA 2026-08-29, where Kalshi paid [90,91] against a CLI of 85, our own observations
of 85 and Polymarket's [84,85]; and the mechanism behind it -- the CLI was issued
twice and Kalshi settled the withdrawn first issuance, 2h50m after the correction
was on the wire.  Both are *upward* errors.  This script exists to find out whether
that is luck or structure.

GROUND TRUTH IS THE PAYOUT.  ``expiration_value`` is the number Kalshi settled on
and ``result`` is what it paid.  Candidate bases are scored against those, never
against a feed derived from Kalshi.  This repo has made the circular-measurement
mistake once already (the retracted 2026-08-21 TWC finding), so observations come
from the IEM archive and the CLI from the IEM CLI archive, both independent of
Kalshi, and the production ledger is read only for "what did the live engine see".

WHAT IT REPORTS
    1. basis agreement, exact (expiration_value == CLI) and bucket-level;
    2. every disagreeing event, individually, with its direction;
    3. the signed residual against observations, split by direction;
    4. a full replay of the LIVE lock-in rule over every settled market in the
       window, graded on Kalshi's own result -- the wrong-call rate at scale;
    5. the resulting expected cost per lock-in, against the +$3.64 the one real
       live trade earned.

WINDOW LIMIT.  Kalshi's ``status=settled`` listing retains ~70 days whatever
``--days`` you pass (measured: the cliff sits at 2026-08-23).  ``--from-db`` adds
the local ``market_results`` ledger, which reaches back to 2026-07-21 but records
only yes/no -- a two-integer range, not the exact value.  Both are reported.

Read-only: Kalshi's public API, IEM's archives, and the ledger opened ``mode=ro``.
No trading, no writes, no tracker interaction.

Usage
-----
    PYTHONPATH=. .venv/bin/python scripts/settlement_audit.py --days 70
    PYTHONPATH=. .venv/bin/python scripts/settlement_audit.py --days 70 --from-db
    PYTHONPATH=. .venv/bin/python scripts/settlement_audit.py --cache /tmp/sa.json
    PYTHONPATH=. .venv/bin/python scripts/settlement_audit.py --from-cache /tmp/sa.json
"""
from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import sqlite3
import statistics
import sys
import time
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import aiohttp

from weather_arb.backtest import _iem_temps_all, _settled_markets
from weather_arb.config import Settings
from weather_arb.fees import fee_cents
from weather_arb.models import LOCKIN_MARGIN_F, Side, Venue, WeatherMarket
from weather_arb.settlement_basis import (
    SERIES_ICAO, event_date, fetch_cli_highs, settled_range)
from weather_arb import timeutils

# --------------------------------------------------------------------------- #
# Pure helpers
# --------------------------------------------------------------------------- #
def market_of(raw: dict[str, Any]) -> WeatherMarket:
    """A production ``WeatherMarket`` from an API row, so rules cannot drift."""
    floor, cap = raw.get("floor"), raw.get("cap")
    return WeatherMarket(
        ticker=str(raw.get("ticker", "")), venue=Venue.KALSHI, icao="",
        floor_strike_f=None if floor is None else float(floor),
        cap_strike_f=None if cap is None else float(cap),
        strike_type=str(raw.get("strike_type", "")),
    )


def lockin_pnl_cents(entry_cents: int, won: bool, size: int,
                     coefficient: float = 0.07) -> float:
    """Net cents on a lock-in: payout minus cost minus fee.

    The fee comes from ``weather_arb.fees.fee_cents`` -- production's own model --
    rather than a local re-derivation, so the loss this script prices is the loss
    the engine would actually book.
    """
    payout = 100.0 * size if won else 0.0
    return payout - entry_cents * size - fee_cents(entry_cents, size, coefficient)


def wilson(hits: int, n: int, z: float = 1.96) -> tuple[float, float]:
    """Wilson score interval -- stays sane at 0 or n hits, unlike the normal one."""
    if n == 0:
        return (0.0, 1.0)
    p = hits / n
    d = 1.0 + z * z / n
    centre = (p + z * z / (2 * n)) / d
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return (max(0.0, centre - half), min(1.0, centre + half))


# --------------------------------------------------------------------------- #
# Collection
# --------------------------------------------------------------------------- #
@dataclass
class Event:
    """One settled city-day: Kalshi's payout plus the independent observations."""
    event_key: str
    series: str
    icao: str
    climate_date: str
    markets: list[dict[str, Any]] = field(default_factory=list)
    expiration_value: Optional[float] = None   # the number Kalshi settled on
    settled_lo: Optional[int] = None           # inclusive integer range from the YES bucket
    settled_hi: Optional[int] = None
    cli_high_f: Optional[float] = None         # NWS CLI, from IEM's CLI archive
    obs_max_f: Optional[float] = None          # IEM archive max over the LST climate day
    obs_n: int = 0
    db_max_f: Optional[float] = None           # what production's own ingest recorded
    source: str = "api"                        # api (exact value) | db (range only)


async def collect(settings: Settings, days: int, use_db: bool, db_path: str) -> list[Event]:
    offsets = {s.icao: s.standard_utc_offset_hours for s in settings.stations}
    today = datetime.now(timezone.utc).date()
    start = today - timedelta(days=days)
    min_close = int(time.time()) - (days + 3) * 86400

    events: dict[str, Event] = {}

    def ensure(key: str, source: str) -> Optional[Event]:
        series = key.split("-")[0]
        icao, cdate = SERIES_ICAO.get(series), event_date(key)
        if icao is None or cdate is None:
            return None
        ev = events.get(key)
        if ev is None:
            ev = events[key] = Event(event_key=key, series=series, icao=icao,
                                     climate_date=cdate, source=source)
        return ev

    async with aiohttp.ClientSession() as session:
        # 1. Kalshi's own settled markets -- the ground truth, with exact values.
        for series in SERIES_ICAO:
            for raw in await _settled_markets(session, series, min_close):
                ticker = str(raw.get("ticker") or "")
                if not ticker:
                    continue
                ev = ensure("-".join(ticker.split("-")[:2]), "api")
                if ev is None:
                    continue
                floor, cap = raw.get("floor_strike"), raw.get("cap_strike")
                ev.markets.append({
                    "ticker": ticker,
                    "floor": None if floor is None else int(floor),
                    "cap": None if cap is None else int(cap),
                    "strike_type": str(raw.get("strike_type", "")),
                    "result": str(raw.get("result", "")).lower(),
                })
                exp = raw.get("expiration_value")
                if exp is not None and ev.expiration_value is None:
                    try:
                        ev.expiration_value = float(exp)
                    except (TypeError, ValueError):
                        pass
            await asyncio.sleep(0.4)          # public API rate-limits at ~3 req/s

        # 2. The local ledger, for the days the API has already dropped.
        if use_db:
            for key, markets in _db_events(db_path).items():
                if key in events:
                    continue
                ev = ensure(key, "db")
                if ev is not None:
                    ev.markets = markets

        # 3. Independent observations + the CLI, neither derived from Kalshi.
        months = sorted({(d.year, d.month) for d in
                         (start + timedelta(days=i) for i in range(days + 2))})
        for icao in sorted({e.icao for e in events.values()}):
            temps = await _iem_temps_all(session, icao, start.isoformat(),
                                         (today + timedelta(days=1)).isoformat())
            await asyncio.sleep(0.8)          # be gentle with the IEM archive
            cli = await fetch_cli_highs(session, icao, months)
            offset = offsets.get(icao)
            for ev in events.values():
                if ev.icao != icao or offset is None:
                    continue
                lo_utc, hi_utc = timeutils.climate_day_bounds_utc(ev.climate_date, offset)
                day = [t for ts, t in temps if lo_utc <= ts < hi_utc]
                ev.obs_n = len(day)
                ev.obs_max_f = max(day) if day else None
                ev.cli_high_f = cli.get(ev.climate_date)

    # 4. What production's own ingest recorded, for the "what would it have done" leg.
    db_max = _db_daily_tmax(db_path)
    out: list[Event] = []
    for ev in events.values():
        if ev.climate_date < start.isoformat():
            continue
        rng = settled_range(ev.markets)
        if rng is not None:
            ev.settled_lo, ev.settled_hi = rng
        ev.db_max_f = db_max.get((ev.icao, ev.climate_date))
        out.append(ev)
    return sorted(out, key=lambda e: (e.climate_date, e.series))


def _connect_ro(db_path: str) -> sqlite3.Connection:
    """Open the live ledger strictly read-only -- the tracker is writing to it."""
    return sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)


def _db_events(db_path: str) -> dict[str, list[dict[str, Any]]]:
    """Settled markets from the local ledger, shaped like the API rows.

    ``market_results`` stores only yes/no, so strike geometry is recovered from the
    ticker.  ``T<n>`` is ambiguous -- it is the ``less`` tail at an event's bottom
    and the ``greater`` tail at its top -- so it is disambiguated per event by
    position, then checked against the interior buckets it must abut.
    """
    out: dict[str, list[dict[str, Any]]] = defaultdict(list)
    with _connect_ro(db_path) as conn:
        rows = conn.execute("SELECT ticker, result FROM market_results").fetchall()
    by_event: dict[str, list[tuple[str, str]]] = defaultdict(list)
    for ticker, result in rows:
        parts = str(ticker).split("-")
        if len(parts) < 3:
            continue
        by_event["-".join(parts[:2])].append((str(ticker), str(result).lower()))

    for key, items in by_event.items():
        tails = sorted(int(t.split("-")[2][1:]) for t, _ in items
                       if t.split("-")[2].startswith("T") and t.split("-")[2][1:].isdigit())
        if len(tails) != 2:
            continue
        low_cap, high_floor = tails[0], tails[1]
        markets: list[dict[str, Any]] = []
        for ticker, result in items:
            tok = ticker.split("-")[2]
            if tok.startswith("B"):
                try:
                    mid = float(tok[1:])
                except ValueError:
                    continue
                floor = int(math.floor(mid))
                markets.append({"ticker": ticker, "floor": floor, "cap": floor + 1,
                                "strike_type": "between", "result": result})
            elif tok.startswith("T") and tok[1:].isdigit():
                n = int(tok[1:])
                if n == low_cap:
                    markets.append({"ticker": ticker, "floor": None, "cap": n,
                                    "strike_type": "less", "result": result})
                else:
                    markets.append({"ticker": ticker, "floor": n, "cap": None,
                                    "strike_type": "greater", "result": result})
        out[key] = markets
    return out


def _db_daily_tmax(db_path: str) -> dict[tuple[str, str], float]:
    """``daily_tmax`` -- the running max the live engine actually traded off."""
    with _connect_ro(db_path) as conn:
        rows = conn.execute("SELECT icao, climate_date, tmax_f FROM daily_tmax").fetchall()
    return {(str(i), str(d)): float(t) for i, d, t in rows if t is not None}


def _db_lockins(db_path: str) -> list[dict[str, Any]]:
    """Live lock-in detections, already graded against settlement by the reconciler."""
    with _connect_ro(db_path) as conn:
        conn.row_factory = sqlite3.Row
        return [dict(r) for r in conn.execute(
            "SELECT ticker, icao, climate_date, source, side, strike_type, "
            "       floor_strike_f, cap_strike_f, boundary_f, observed_tmax_f, "
            "       margin_f, settled_result, call_correct FROM lockin_events"
        ).fetchall()]


# --------------------------------------------------------------------------- #
# Analysis
# --------------------------------------------------------------------------- #
def in_range(value: float, lo: Optional[int], hi: Optional[int]) -> bool:
    return (lo is None or value >= lo) and (hi is None or value <= hi)


def analyze(events: list[Event], lockins: list[dict[str, Any]], size: int,
            margin_f: float) -> dict[str, Any]:
    api = [e for e in events if e.source == "api"]
    gradable = [e for e in events if e.settled_lo is not None or e.settled_hi is not None]

    # --- 1. Basis agreement ------------------------------------------------- #
    exact = [e for e in api if e.expiration_value is not None and e.cli_high_f is not None]
    exact_hits = {e.event_key for e in exact
                  if abs(e.expiration_value - e.cli_high_f) < 1e-9}
    bucket = [e for e in gradable if e.cli_high_f is not None]
    bucket_hits = {e.event_key for e in bucket
                   if in_range(e.cli_high_f, e.settled_lo, e.settled_hi)}

    basis_misses = [{
        "event": e.event_key, "icao": e.icao, "date": e.climate_date,
        "expiration_value": e.expiration_value, "cli": e.cli_high_f,
        "settled_range": [e.settled_lo, e.settled_hi],
        "obs_max_f": e.obs_max_f, "obs_n": e.obs_n, "db_max_f": e.db_max_f,
        "direction": ("settled ABOVE cli" if (e.expiration_value or 0) > (e.cli_high_f or 0)
                      else "settled BELOW cli"),
        "delta_vs_cli": (None if e.expiration_value is None or e.cli_high_f is None
                         else e.expiration_value - e.cli_high_f),
    } for e in exact if e.event_key not in exact_hits]

    bucket_misses = [{
        "event": e.event_key, "icao": e.icao, "date": e.climate_date,
        "expiration_value": e.expiration_value, "cli": e.cli_high_f,
        "settled_range": [e.settled_lo, e.settled_hi], "obs_max_f": e.obs_max_f,
        "obs_n": e.obs_n, "db_max_f": e.db_max_f,
    } for e in bucket if e.event_key not in bucket_hits]

    # --- 2. Signed residual against observations ---------------------------- #
    # This is the Strategy-1 axis.  NEGATIVE means Kalshi settled BELOW what we
    # observed, which is the only direction that can break a certainty proof.
    resid = [(e, e.expiration_value - e.obs_max_f) for e in api
             if e.expiration_value is not None and e.obs_max_f is not None and e.obs_n >= 20]
    negatives = [{
        "event": e.event_key, "icao": e.icao, "date": e.climate_date,
        "expiration_value": e.expiration_value, "cli": e.cli_high_f,
        "obs_max_f": e.obs_max_f, "obs_n": e.obs_n, "db_max_f": e.db_max_f,
        "residual_f": r, "settled_range": [e.settled_lo, e.settled_hi],
    } for e, r in sorted(resid, key=lambda x: x[1]) if r < 0]

    vals = [r for _, r in resid]
    residual = {"n": len(vals)}
    if vals:
        residual.update({
            "mean": statistics.fmean(vals),
            "sd": statistics.stdev(vals) if len(vals) > 1 else 0.0,
            "min": min(vals), "max": max(vals),
            "n_negative": sum(1 for v in vals if v < 0),
            "n_below_minus_half": sum(1 for v in vals if v < -0.5),
            "histogram": dict(sorted(Counter(round(v, 1) for v in vals).items())),
        })

    # --- 3. Replay the LIVE lock-in rule over every settled market ---------- #
    # The daily max is monotone, so any market that is certain at the day's final
    # observed max was certain the moment the max crossed -- replaying at the final
    # max therefore reproduces the whole detection set, not a subset.
    # Two observation bases, reported side by side: "iem" is the independent
    # archive (what the weather did) and "db" is daily_tmax (what production's own
    # ingest recorded, spike guard and all).  They answer different questions and
    # a divergence between them is itself a finding.
    replays: dict[str, dict[str, Any]] = {}
    for basis_name in ("iem", "db"):
        rep: dict[str, Any] = {"detections": 0, "correct": 0, "wrong": 0}
        branches: dict[str, dict[str, int]] = defaultdict(lambda: {"n": 0, "wrong": 0})
        wrong_calls: list[dict[str, Any]] = []
        for e in gradable:
            observed = e.obs_max_f if basis_name == "iem" else e.db_max_f
            if observed is None or (basis_name == "iem" and e.obs_n < 20):
                continue
            for raw in e.markets:
                if raw["result"] not in ("yes", "no"):
                    continue
                call = market_of(raw).deterministic_lock_in(observed, margin_f=margin_f)
                if call is None:
                    continue
                side, _ = call
                won = ((side is Side.YES and raw["result"] == "yes")
                       or (side is Side.NO and raw["result"] == "no"))
                branch = f"{raw['strike_type']}/{side.value}"
                rep["detections"] += 1
                branches[branch]["n"] += 1
                if won:
                    rep["correct"] += 1
                else:
                    rep["wrong"] += 1
                    branches[branch]["wrong"] += 1
                    wrong_calls.append({
                        "ticker": raw["ticker"], "icao": e.icao, "date": e.climate_date,
                        "side": side.value, "strike_type": raw["strike_type"],
                        "floor": raw["floor"], "cap": raw["cap"],
                        "observed_max_f": observed, "expiration_value": e.expiration_value,
                        "cli": e.cli_high_f, "settled_result": raw["result"],
                        "loss_cents": -lockin_pnl_cents(98, False, size),
                    })
        lo, hi = wilson(rep["wrong"], max(1, rep["detections"]))
        rep["by_branch"] = dict(sorted(branches.items()))
        rep["wrong_calls"] = wrong_calls
        rep["wrong_rate"] = rep["wrong"] / rep["detections"] if rep["detections"] else 0.0
        rep["wrong_rate_ci95"] = [lo, hi]
        replays[basis_name] = rep
    replay = replays["iem"]

    # --- 4. The live ledger's own verdict ----------------------------------- #
    live: dict[str, Any] = {}
    for src in sorted({str(r["source"]) for r in lockins}):
        rows = [r for r in lockins if r["source"] == src]
        graded = [r for r in rows if r["call_correct"] is not None]
        wrong = [r for r in graded if not r["call_correct"]]
        w_lo, w_hi = wilson(len(wrong), max(1, len(graded)))
        live[src] = {
            "detections": len(rows), "graded": len(graded), "wrong": len(wrong),
            "rate": len(wrong) / len(graded) if graded else 0.0,
            "rate_ci95": [w_lo, w_hi],
            "wrong_rows": [{
                "ticker": r["ticker"], "date": r["climate_date"], "side": r["side"],
                "strike_type": r["strike_type"], "boundary_f": r["boundary_f"],
                "observed_tmax_f": r["observed_tmax_f"], "margin_f": r["margin_f"],
                "settled_result": r["settled_result"],
            } for r in sorted(wrong, key=lambda r: str(r["climate_date"]))],
        }

    # --- 5. Economics ------------------------------------------------------- #
    # The one real live fill: 189 contracts, VWAP 97.93c, net +$3.64.
    win_cents = lockin_pnl_cents(98, True, size)
    loss_cents = lockin_pnl_cents(98, False, size)
    p = replay["wrong_rate"]
    econ = {
        "size": size,
        "win_net_usd": win_cents / 100.0,
        "loss_net_usd": loss_cents / 100.0,
        "breakeven_wrong_rate": win_cents / (win_cents - loss_cents),
        "ev_usd_at_replay_rate": ((1 - p) * win_cents + p * loss_cents) / 100.0,
        "ev_usd_at_ci_upper": ((1 - replay["wrong_rate_ci95"][1]) * win_cents
                               + replay["wrong_rate_ci95"][1] * loss_cents) / 100.0,
    }

    dates = sorted({e.climate_date for e in events})
    return {
        "window": {"from": dates[0] if dates else None, "to": dates[-1] if dates else None,
                   "events": len(events), "api_events": len(api),
                   "db_only_events": len(events) - len(api), "gradable": len(gradable)},
        "margin_f": margin_f,
        "basis_exact": {"n": len(exact), "hits": len(exact_hits),
                        "rate": len(exact_hits) / len(exact) if exact else 0.0,
                        "misses": basis_misses},
        "basis_bucket": {"n": len(bucket), "hits": len(bucket_hits),
                         "rate": len(bucket_hits) / len(bucket) if bucket else 0.0,
                         "misses": bucket_misses},
        "residual_vs_observed": residual,
        "settled_below_observed": negatives,
        "lockin_replay": replay,
        "lockin_replay_db": replays["db"],
        "lockin_live": live,
        "economics": econ,
    }


# --------------------------------------------------------------------------- #
# Rendering
# --------------------------------------------------------------------------- #
def _render(r: dict[str, Any]) -> str:
    w = r["window"]
    out = ["=" * 78, "SETTLEMENT AUDIT -- is Strategy 1's certainty proof actually certain?",
           "=" * 78,
           f"  window {w['from']} .. {w['to']}   events {w['events']} "
           f"(api {w['api_events']}, ledger-only {w['db_only_events']})   "
           f"gradable {w['gradable']}",
           f"  lock-in margin {r['margin_f']:.2f} degF"]

    be, bb = r["basis_exact"], r["basis_bucket"]
    out += ["", "  1. DOES KALSHI SETTLE ON THE NWS CLI?  (scored on Kalshi's own payouts)",
            f"     exact   expiration_value == CLI      "
            f"{be['hits']:4d}/{be['n']:<4d} = {100*be['rate']:6.2f}%",
            f"     bucket  CLI inside the paid bucket   "
            f"{bb['hits']:4d}/{bb['n']:<4d} = {100*bb['rate']:6.2f}%"]

    if be["misses"]:
        out += ["", "     EVERY EXACT DISAGREEMENT, individually:"]
        for m in sorted(be["misses"], key=lambda x: x["date"]):
            obs = "   n/a" if m["obs_max_f"] is None else f"{m['obs_max_f']:6.2f}"
            out.append(
                f"       {m['date']}  {m['icao']}  settled {m['expiration_value']:.0f}  "
                f"cli {m['cli']:.0f}  delta {m['delta_vs_cli']:+.0f}  "
                f"obs_max {obs}  paid {m['settled_range']}  [{m['direction']}]")
    else:
        out.append("     no exact disagreement in the window")

    res = r["residual_vs_observed"]
    out += ["", "  2. SIGNED RESIDUAL  R = settlement - our observed max  (the Strategy-1 axis).",
            "     NEGATIVE is the only direction that can break a certainty proof:"]
    if res.get("n"):
        out += [f"     n {res['n']}   mean {res['mean']:+.3f}   sd {res['sd']:.3f}   "
                f"range [{res['min']:+.2f}, {res['max']:+.2f}]",
                f"     R < 0 on {res['n_negative']}/{res['n']} events   "
                f"R < -0.5 on {res['n_below_minus_half']}/{res['n']}",
                f"     histogram {res['histogram']}"]

    neg = r["settled_below_observed"]
    out += ["", f"  3. EVERY EVENT WHERE KALSHI SETTLED BELOW OUR OBSERVED MAX ({len(neg)}):"]
    if neg:
        out.append(f"       {'date':10s} {'stn':5s} {'settled':>7s} {'cli':>4s} "
                   f"{'obs':>7s} {'R':>6s}  paid")
        for m in neg:
            out.append(f"       {m['date']:10s} {m['icao']:5s} "
                       f"{m['expiration_value']:7.0f} {m['cli'] if m['cli'] is not None else float('nan'):4.0f} "
                       f"{m['obs_max_f']:7.2f} {m['residual_f']:+6.2f}  {m['settled_range']}")
    else:
        out.append("       none")

    rep = r["lockin_replay"]
    out += ["", "  4. LIVE LOCK-IN RULE REPLAYED over every settled market in the window.",
            "     WeatherMarket.deterministic_lock_in is called directly, so this is the",
            "     production rule, not a re-derivation. Graded on Kalshi's own result:",
            f"     detections {rep['detections']}   correct {rep['correct']}   "
            f"WRONG {rep['wrong']}   rate {100*rep['wrong_rate']:.3f}%  "
            f"[95% CI {100*rep['wrong_rate_ci95'][0]:.3f}-{100*rep['wrong_rate_ci95'][1]:.3f}%]"]
    for branch, b in rep["by_branch"].items():
        out.append(f"       {branch:18s} n {b['n']:5d}   wrong {b['wrong']}")
    for wc in rep["wrong_calls"]:
        out.append(f"       WRONG  {wc['date']} {wc['ticker']}  buy {wc['side'].upper()}  "
                   f"obs {wc['observed_max_f']:.2f}  settled {wc['expiration_value']}  "
                   f"paid {wc['settled_result']}  -${wc['loss_cents']/100:.2f}")

    rdb = r["lockin_replay_db"]
    out += ["", "     Same replay against daily_tmax -- what production's OWN ingest recorded",
            "     (spike guard included), rather than the independent archive:",
            f"     detections {rdb['detections']}   correct {rdb['correct']}   "
            f"WRONG {rdb['wrong']}   rate {100*rdb['wrong_rate']:.3f}%  "
            f"[95% CI {100*rdb['wrong_rate_ci95'][0]:.3f}-{100*rdb['wrong_rate_ci95'][1]:.3f}%]"]
    for wc in rdb["wrong_calls"]:
        out.append(f"       WRONG  {wc['date']} {wc['ticker']}  buy {wc['side'].upper()}  "
                   f"obs {wc['observed_max_f']:.2f}  settled {wc['expiration_value']}  "
                   f"paid {wc['settled_result']}  -${wc['loss_cents']/100:.2f}")

    out += ["", "  5. THE LIVE LEDGER'S OWN VERDICT (lockin_events, graded by the reconciler).",
            "     'metar' is the channel that trades; 'nws5min' is shadow-only:"]
    for src, s in r["lockin_live"].items():
        out.append(f"     {src:9s} detections {s['detections']:5d}  graded {s['graded']:5d}  "
                   f"wrong {s['wrong']:3d}  rate {100*s['rate']:6.2f}%  "
                   f"[95% CI {100*s['rate_ci95'][0]:.2f}-{100*s['rate_ci95'][1]:.2f}%]")

    e = r["economics"]
    out += ["", f"  6. ECONOMICS at {e['size']} contracts, entry 98c (the live ceiling):",
            f"     win  net {e['win_net_usd']:+.2f}   lose net {e['loss_net_usd']:+.2f}",
            f"     breakeven wrong-rate {100*e['breakeven_wrong_rate']:.3f}%",
            f"     EV at the replayed rate      {e['ev_usd_at_replay_rate']:+.2f} / lock-in",
            f"     EV at the CI upper bound     {e['ev_usd_at_ci_upper']:+.2f} / lock-in"]
    out.append("=" * 78)
    return "\n".join(out)


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #
def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--days", type=int, default=70,
                        help="look back N days (Kalshi retains only ~70 whatever you pass)")
    parser.add_argument("--size", type=int, default=189,
                        help="contracts, for the economics panel (default 189 = the real fill)")
    parser.add_argument("--margin-f", type=float, default=LOCKIN_MARGIN_F,
                        help=f"lock-in margin to replay (default {LOCKIN_MARGIN_F})")
    parser.add_argument("--from-db", action="store_true",
                        help="also read market_results, which reaches past the API cliff")
    parser.add_argument("--db", default=None, help="path to weather_arb.sqlite3")
    parser.add_argument("--cache", default=None, help="write collected events to JSON")
    parser.add_argument("--from-cache", default=None, help="re-analyse without refetching")
    parser.add_argument("--json", action="store_true", help="emit the full result as JSON")
    args = parser.parse_args(argv)

    settings = Settings.from_env()
    db_path = args.db or os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "weather_arb.sqlite3")

    if args.from_cache:
        with open(args.from_cache) as fh:
            events = [Event(**e) for e in json.load(fh)]
    else:
        events = asyncio.run(collect(settings, max(7, args.days), args.from_db, db_path))
        if args.cache:
            with open(args.cache, "w") as fh:
                json.dump([asdict(e) for e in events], fh)

    result = analyze(events, _db_lockins(db_path), args.size, args.margin_f)
    if args.json:
        print(json.dumps(result, indent=2, default=str))
    else:
        print(_render(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
