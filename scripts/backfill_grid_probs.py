#!/usr/bin/env python
"""One-off: recompute `model_prediction_history` probabilities on the corrected bucket grid.

Every row in `model_prediction_history` was priced by integrating the forecast
density over Kalshi's RAW INTEGER strikes, which covers half the settlement space
a bucket really owns (see WeatherMarket.settlement_interval_f). `backtest
--strategy 2` replays these stored probabilities, so until they are corrected the
backtest cannot see the 2026-08-23 grid fix at all.

This backfills a corrected probability for each row **without touching `prob`**.

WHY A NEW COLUMN INSTEAD OF UPDATING `prob` IN PLACE
----------------------------------------------------
`model_prediction_history` is the only surviving record of what the live system
actually predicted. Overwriting it would destroy the pre-fix baseline -- which is
precisely the thing the corrected numbers have to be measured *against* -- and a
transaction only protects against a crash mid-run, not against a logic bug found
next week. Writing `prob_grid_fixed` alongside `prob` is recoverable from either,
makes the run idempotent and resumable, and lets one backtest diff the two.

WHY CHUNKED COMMITS INSTEAD OF ONE TRANSACTION
-----------------------------------------------
The tracker is normally live against this same file with `busy_timeout=5000`
(db.py). One transaction spanning ~1M row updates holds SQLite's single write
lock for far longer than 5 s and would make the running tracker's inserts fail.
Chunked commits keep each lock well under that. Durability is covered better than
a transaction would anyway: we take a consistent `VACUUM INTO` snapshot first, and
a partially-applied run leaves NULLs, which are simply picked up on the next run.

RECONSTRUCTING THE TRUNCATION BOUND
------------------------------------
`_estimate` conditions on `X >= observed_tmax_f`, read from `daily_tmax` -- which
is updated in place and keeps no history, so the bound cannot be looked up. It is
instead **inverted from the stored probability**, in closed form. For one market
with stored p, mean mu, sigma s and old bounds [L, H] (writing u = Phi((b-mu)/s)):

    b <= L  (bucket above the bound):  p = N / (1 - u)      -> u = 1 - N/p
    L < b < H (bound inside bucket):   p = (H' - u)/(1 - u) -> u = (H' - p)/(1 - p)

where N = Phi_H - Phi_L and H' = Phi_H. Each market yields at most two candidate
bounds; the true bound is shared by every market in the (event, model, minute)
snapshot, so we score all candidates against ALL markets in the group and keep the
best. "No truncation at all" (observed_tmax_f was None) is scored as a candidate too.

`blend` is the exception: it is a probability-space average of two other models,
not a truncated normal, so no bound reproduces it. It is recomposed from the
corrected components of the same snapshot instead.

That makes the reconstruction self-validating: we re-derive each row's OLD
probability from the chosen bound and require it to reproduce the stored value.
Groups that fail are left NULL and reported rather than guessed at.

AFTER THE TRACKER RESTART
-------------------------
Once the engine itself prices on the corrected grid, new rows are born correct:
their `prob` is already right and their `prob_grid_fixed` would be NULL, so
`backtest --grid-fixed` would silently skip them. Re-run with
`--corrected-since <restart time>` to copy `prob` into `prob_grid_fixed` for those
rows (and to keep the inversion away from them -- inverting a row that was never
priced on the old grid would at best fail validation and at worst fit noise).

    python scripts/backfill_grid_probs.py [--dry-run] [--no-backup] [--db PATH]
    python scripts/backfill_grid_probs.py --corrected-since 2026-08-23T18:00:00+00:00
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import shutil
import sqlite3
import sys
import time
from collections import defaultdict
from statistics import NormalDist
from typing import Any, Iterator, Optional

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import aiohttp

from weather_arb.backtest import _kalshi_get
from weather_arb.forecast_ingest import _truncated_normal_between
from weather_arb.models import Venue, WeatherMarket
from weather_arb.settlement_basis import SERIES_ICAO

# A reconstructed bound must reproduce every stored probability in its group to
# this tolerance, else the group is skipped. Stored probs are full float64.
VALIDATE_TOL = 1e-7
CHUNK = 20_000          # rows per commit -- keeps each write lock well under 5 s
PROGRESS_EVERY = 10_000  # groups


# --------------------------------------------------------------------------- #
# Market geometry
# --------------------------------------------------------------------------- #
async def _fetch_geometry(tickers: set[str], cache_path: str) -> dict[str, dict[str, Any]]:
    """floor/cap/strike_type per ticker, from Kalshi (settled AND open), cached.

    The ticker suffix cannot be parsed for this: `T96` is a *low* tail on one
    event and a *high* tail on another, so only the API's `strike_type` settles it.
    """
    cached: dict[str, dict[str, Any]] = {}
    if os.path.exists(cache_path):
        with open(cache_path) as fh:
            cached = json.load(fh)
    if tickers <= set(cached):
        print(f"  geometry: {len(tickers)} tickers, all cached")
        return cached

    print(f"  geometry: fetching (have {len(set(cached) & tickers)}/{len(tickers)})")
    async with aiohttp.ClientSession() as session:
        for series in SERIES_ICAO:
            cursor: Optional[str] = None
            for _ in range(40):
                params: dict[str, object] = {"series_ticker": series, "limit": 200}
                if cursor:
                    params["cursor"] = cursor
                data = await _kalshi_get(
                    session, "https://api.elections.kalshi.com/trade-api/v2/markets", params)
                batch = data.get("markets", [])
                for raw in batch:
                    tk = raw.get("ticker")
                    if not tk:
                        continue
                    floor, cap = raw.get("floor_strike"), raw.get("cap_strike")
                    cached[str(tk)] = {
                        "floor": None if floor is None else float(floor),
                        "cap": None if cap is None else float(cap),
                        "strike_type": str(raw.get("strike_type", "")),
                    }
                cursor = data.get("cursor") or None
                if not cursor or not batch:
                    break
            print(f"    {series}: {len(cached)} known", flush=True)
    with open(cache_path, "w") as fh:
        json.dump(cached, fh)
    return cached


def _market(ticker: str, geom: dict[str, Any]) -> WeatherMarket:
    return WeatherMarket(
        ticker=ticker, venue=Venue.KALSHI, icao="",
        floor_strike_f=geom["floor"], cap_strike_f=geom["cap"],
        strike_type=geom["strike_type"],
    )


def _old_bounds(m: WeatherMarket) -> tuple[float, float]:
    """The pre-2026-08-23 grid: raw integer strikes."""
    return (m.floor_f if m.floor_f is not None else -math.inf,
            m.cap_f if m.cap_f is not None else math.inf)


# --------------------------------------------------------------------------- #
# Truncation-bound inversion
# --------------------------------------------------------------------------- #
def _phi(dist: NormalDist, x: float) -> float:
    return 0.0 if x == -math.inf else 1.0 if x == math.inf else dist.cdf(x)


def _candidates(rows: list[dict[str, Any]]) -> list[Optional[float]]:
    """Closed-form candidate truncation bounds implied by each row."""
    out: list[Optional[float]] = [None]          # "no observation yet"
    for r in rows:
        mu, s, p = r["mu"], r["sigma"], r["prob"]
        if s <= 0:
            continue
        dist = NormalDist(mu, s)
        lo, hi = r["old_lo"], r["old_hi"]
        phi_l, phi_h = _phi(dist, lo), _phi(dist, hi)
        n = phi_h - phi_l
        for u in (
            (1.0 - n / p) if p > 1e-12 else None,          # bound below the bucket
            ((phi_h - p) / (1.0 - p)) if p < 1.0 - 1e-12 else None,  # bound inside it
        ):
            if u is None or not (1e-12 < u < 1.0 - 1e-12):
                continue
            try:
                out.append(mu + s * NormalDist().inv_cdf(u))
            except (ValueError, OverflowError):
                continue
    return out


def _sse(rows: list[dict[str, Any]], bound: Optional[float]) -> float:
    total = 0.0
    for r in rows:
        got = _truncated_normal_between(r["mu"], r["sigma"], r["old_lo"], r["old_hi"], bound)
        total += (got - r["prob"]) ** 2
    return total


def solve_bound(rows: list[dict[str, Any]]) -> tuple[Optional[float], float, bool]:
    """``(bound, max_abs_error, ok)`` for one (event, model, minute) snapshot.

    The bound is shared by every market in the snapshot, so candidates derived
    from any one row are scored against all of them.
    """
    best, best_sse = None, math.inf
    for cand in _candidates(rows):
        # observed_tmax_f can never exceed the stored mean: _estimate sets
        # mu = max(model_mu, observed), so a candidate above mu is spurious.
        if cand is not None and cand > max(r["mu"] for r in rows) + 1e-9:
            continue
        sse = _sse(rows, cand)
        if sse < best_sse:
            best, best_sse = cand, sse
    if best_sse is math.inf:
        return None, math.inf, False
    err = max(
        abs(_truncated_normal_between(r["mu"], r["sigma"], r["old_lo"], r["old_hi"], best)
            - r["prob"])
        for r in rows
    )
    return best, err, err <= VALIDATE_TOL


# --------------------------------------------------------------------------- #
# Streaming over groups
# --------------------------------------------------------------------------- #
# `blend` is composed from OTHER models of the same snapshot, so a snapshot has to
# be assembled across models -- hence (event, minute), with model handled inside.
GROUP_SQL = """
SELECT id, ticker, model, prob, forecast_high_f, sigma_f, created_at_utc,
       substr(ticker, 1, instr(ticker,'-')+7) AS event,
       substr(created_at_utc, 1, 16)          AS minute
FROM model_prediction_history
WHERE forecast_high_f IS NOT NULL AND sigma_f IS NOT NULL
  AND (? IS NULL OR created_at_utc < ?)
ORDER BY event, minute
"""

# forecast_ingest: blend = 0.5*climo + 0.5*(ensemble if present else current).
# Its stored forecast_high_f/sigma_f are NOT the parameters of any normal, so the
# bound cannot be inverted from a blend row -- it must be recomposed instead.
COMPOSED = "blend"


def _groups(conn: sqlite3.Connection, geom: dict[str, Any],
            corrected_since: Optional[str] = None
            ) -> Iterator[tuple[tuple[str, str], dict[str, list[dict[str, Any]]], int]]:
    """Yield ``(key, rows_by_model, skipped_no_geometry)`` one snapshot at a time."""
    cur = conn.execute(GROUP_SQL, (corrected_since, corrected_since))
    cur.arraysize = 5000
    key: Optional[tuple[str, str]] = None
    batch: dict[str, list[dict[str, Any]]] = defaultdict(list)
    missing = 0
    for (rid, ticker, model, prob, mu, sigma, _created, event, minute) in cur:
        k = (event, minute)
        if key is not None and k != key:
            yield key, batch, missing
            batch, missing = defaultdict(list), 0
        key = k
        g = geom.get(ticker)
        if g is None or (g["floor"] is None and g["cap"] is None):
            missing += 1
            continue
        m = _market(ticker, g)
        lo, hi = _old_bounds(m)
        batch[model].append({"id": rid, "ticker": ticker, "market": m, "prob": prob,
                             "mu": mu, "sigma": sigma, "old_lo": lo, "old_hi": hi})
    if key is not None:
        yield key, batch, missing


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #
def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--db", default="weather_arb.sqlite3")
    ap.add_argument("--dry-run", action="store_true",
                    help="compute and report, write nothing")
    ap.add_argument("--no-backup", action="store_true",
                    help="skip the VACUUM INTO snapshot (not recommended)")
    ap.add_argument("--cache", default="scripts/.market_geometry.json")
    ap.add_argument("--corrected-since", metavar="ISO8601",
                    help="rows created at/after this UTC time were already priced on "
                         "the corrected grid (i.e. after the tracker restart): copy "
                         "prob into prob_grid_fixed and never invert them")
    args = ap.parse_args(argv)

    t0 = time.time()
    conn = sqlite3.connect(args.db, timeout=30.0)
    conn.execute("PRAGMA busy_timeout=30000;")

    total, = conn.execute("SELECT count(*) FROM model_prediction_history").fetchone()
    print(f"model_prediction_history: {total:,} rows")

    # -- geometry -----------------------------------------------------------
    tickers = {t for (t,) in conn.execute(
        "SELECT DISTINCT ticker FROM model_prediction_history")}
    geom = asyncio.run(_fetch_geometry(tickers, args.cache))
    covered = len(tickers & set(geom))
    print(f"  geometry known for {covered}/{len(tickers)} tickers")

    # -- backup -------------------------------------------------------------
    if not args.dry_run and not args.no_backup:
        snap = f"{args.db}.pre-gridfix-{time.strftime('%Y%m%dT%H%M%S')}"
        print(f"  snapshot -> {snap} ...", end="", flush=True)
        conn.execute("VACUUM INTO ?", (snap,))
        print(f" {os.path.getsize(snap)/1e6:.0f} MB")

    # -- additive migration -------------------------------------------------
    cols = {r[1] for r in conn.execute("PRAGMA table_info(model_prediction_history)")}
    if "prob_grid_fixed" not in cols and not args.dry_run:
        conn.execute("ALTER TABLE model_prediction_history ADD COLUMN prob_grid_fixed REAL")
        conn.commit()
        print("  added column prob_grid_fixed")
    elif "prob_grid_fixed" in cols:
        done, = conn.execute(
            "SELECT count(*) FROM model_prediction_history "
            "WHERE prob_grid_fixed IS NOT NULL").fetchone()
        print(f"  column exists; {done:,} rows already backfilled (resumable)")

    # -- rows already priced on the corrected grid ---------------------------
    n_passthrough = 0
    if args.corrected_since:
        n_passthrough, = conn.execute(
            "SELECT count(*) FROM model_prediction_history "
            "WHERE created_at_utc >= ? AND prob_grid_fixed IS NULL",
            (args.corrected_since,)).fetchone()
        print(f"  {n_passthrough:,} rows at/after {args.corrected_since} were priced "
              f"on the corrected grid already -> copying prob across")
        if not args.dry_run and n_passthrough:
            conn.execute(
                "UPDATE model_prediction_history SET prob_grid_fixed = prob "
                "WHERE created_at_utc >= ? AND prob_grid_fixed IS NULL",
                (args.corrected_since,))
            conn.commit()

    # -- compute ------------------------------------------------------------
    print("\nreconstructing truncation bounds and repricing ...")
    pending: list[tuple[float, int]] = []
    n_groups = n_ok = n_bad = n_rows = n_missing = n_composed = 0
    no_trunc = 0
    errs: list[float] = []
    deltas: list[float] = []
    write = conn.cursor()

    for key, by_model, missing in _groups(conn, geom, args.corrected_since):
        n_missing += missing
        if not by_model:
            continue
        # corrected[model][ticker] -> prob, so `blend` can be recomposed after.
        corrected: dict[str, dict[str, float]] = {}
        for model, rows in by_model.items():
            if model == COMPOSED or not rows:
                continue
            n_groups += 1
            bound, err, ok = solve_bound(rows)
            if not ok:
                n_bad += 1
                continue
            n_ok += 1
            errs.append(err)
            if bound is None:
                no_trunc += 1
            got: dict[str, float] = {}
            for r in rows:
                lo, hi = r["market"].settlement_interval_f()
                new = _truncated_normal_between(r["mu"], r["sigma"], lo, hi, bound)
                got[r["ticker"]] = new
                pending.append((new, r["id"]))
                deltas.append(new - r["prob"])
                n_rows += 1
            corrected[model] = got

        # blend = 0.5*climo + 0.5*(ensemble if it existed, else current), recomposed
        # from the CORRECTED components rather than inverted.
        blend_rows = by_model.get(COMPOSED) or []
        if blend_rows:
            n_groups += 1
            base = "ensemble" if "ensemble" in by_model else "current"
            climo, base_p = corrected.get("climo"), corrected.get(base)
            if climo is None or base_p is None:
                n_bad += 1
            else:
                built = 0
                for r in blend_rows:
                    a, b = climo.get(r["ticker"]), base_p.get(r["ticker"])
                    if a is None or b is None:
                        continue
                    new = 0.5 * a + 0.5 * b
                    pending.append((new, r["id"]))
                    deltas.append(new - r["prob"])
                    n_rows += 1
                    built += 1
                if built:
                    n_ok += 1
                    n_composed += built
                else:
                    n_bad += 1

        if len(pending) >= CHUNK and not args.dry_run:
            write.executemany(
                "UPDATE model_prediction_history SET prob_grid_fixed=? WHERE id=?", pending)
            conn.commit()                      # short lock; tracker can interleave
            pending.clear()
        if n_groups % PROGRESS_EVERY == 0:
            el = time.time() - t0
            pct = 100.0 * n_rows / max(total, 1)
            print(f"  {n_groups:>7,} groups | {n_rows:>9,} rows ({pct:5.1f}%) | "
                  f"ok {n_ok:,} bad {n_bad:,} | {el:6.0f}s", flush=True)

    if pending and not args.dry_run:
        write.executemany(
            "UPDATE model_prediction_history SET prob_grid_fixed=? WHERE id=?", pending)
        conn.commit()

    # -- report -------------------------------------------------------------
    def pct(v: list[float], q: float) -> float:
        s = sorted(v)
        return s[min(len(s) - 1, int(q * len(s)))] if s else float("nan")

    print("\n" + "=" * 70)
    print("BACKFILL SUMMARY" + ("  (DRY RUN -- nothing written)" if args.dry_run else ""))
    print("=" * 70)
    print(f"  snapshots (event,model,minute) : {n_groups:,}")
    print(f"    bound reconstructed + validated: {n_ok:,} ({100*n_ok/max(n_groups,1):.2f}%)")
    print(f"    failed validation -> left NULL : {n_bad:,} ({100*n_bad/max(n_groups,1):.2f}%)")
    print(f"    of the good ones, untruncated  : {no_trunc:,}")
    print(f"  blend rows recomposed (not inverted): {n_composed:,}")
    print(f"  rows repriced                   : {n_rows:,} / {total:,}")
    if args.corrected_since:
        print(f"  rows copied through (born correct): {n_passthrough:,}")
    print(f"  rows skipped (no geometry)      : {n_missing:,}")
    if errs:
        print(f"\n  validation |old_recomputed - stored|: "
              f"max {max(errs):.2e}  p99 {pct(errs,0.99):.2e}  median {pct(errs,0.5):.2e}")
    if deltas:
        pos = sum(1 for d in deltas if d > 0)
        print(f"\n  prob change (corrected - stored), n={len(deltas):,}:")
        print(f"    mean {sum(deltas)/len(deltas):+.4f}   increased {100*pos/len(deltas):.1f}% of rows")
        for q in (0.01, 0.25, 0.50, 0.75, 0.99):
            print(f"    p{int(q*100):02d} {pct(deltas,q):+.4f}")
    print(f"\n  elapsed {time.time()-t0:.0f}s")
    print("=" * 70)
    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
