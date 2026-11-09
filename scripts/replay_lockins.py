#!/usr/bin/env python3
"""Replay the Strategy-1 lock-in rule over stored observations and grade EVERY
detection against official settlement.

WHY THIS EXISTS. `backtest --strategy 1` counts detections but grades only the
ones that clear the 98c price ceiling -- 1 of 421 in a 30-day run -- so the
"421 detections, 0 wrong calls" safety claim was never actually measured. This
grades all of them, offline, from the local ledger, and is the artefact behind
every detection/accuracy number quoted about Strategy 1.

It is deliberately independent of `backtest.py`: it walks `station_logs`
directly rather than re-fetching the IEM archive, so it measures the rule
against the observations the LIVE engine actually saw.

Usage
-----
    .venv/bin/python scripts/replay_lockins.py                 # old (0.0) vs configured
    .venv/bin/python scripts/replay_lockins.py --margins 0 0.25 0.5 1.0
    .venv/bin/python scripts/replay_lockins.py --json out.json
    .venv/bin/python scripts/replay_lockins.py --use-daily-tmax

Two observation bases, and the difference matters:

* default -- the running max over raw accepted `station_logs`. This is what the
  engine WOULD see with a perfect glitch guard, and it includes sensor spikes
  the guard rejects (e.g. KDEN 2026-08-16, 138.2F), so it is the pessimistic
  bound on rule safety.
* `--use-daily-tmax` -- the guarded end-of-day `daily_tmax` value. Closer to
  live behaviour, but `daily_tmax` is updated IN PLACE with no history, so only
  the final value exists: it can say WHETHER a detection fires, never when.
"""
from __future__ import annotations

import argparse
import asyncio
import collections
import json
import os
import re
import sqlite3
import sys
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from weather_arb.config import Settings                              # noqa: E402
from weather_arb.models import (                                     # noqa: E402
    STRIKE_GREATER, Action, Side, Venue, WeatherMarket,
)


def legacy_lock_in(market: WeatherMarket, observed_tmax_f: float):
    """VERBATIM copy of the pre-2026-08-26 rule, kept so "before" is exact.

    Deliberately duplicated rather than expressed as ``margin_f=0.0``: the old
    rule used a STRICT ``>`` against the raw integer strike, while margin 0.0 on
    the new rule is ``>=`` against the interval bound minus a half degree. Those
    differ on exact-integer observations (e.g. running 86.00 against a cap of
    86), so reusing the knob would misreport the baseline by a handful of
    detections in each direction. This is the only place in the tree that should
    ever contain this logic.
    """
    lo, hi = market.floor_f, market.cap_f
    if hi is not None and observed_tmax_f > hi:
        return (Side.NO, Action.BUY)
    if hi is None and lo is not None:
        hit = (observed_tmax_f > lo if market.kind == STRIKE_GREATER
               else observed_tmax_f >= lo)
        if hit:
            return (Side.YES, Action.BUY)
    return None


def certainty_bound_f(market: WeatherMarket) -> float:
    """The observed max at which this bucket becomes mathematically certain.

    Upper edge of the YES region for a bounded bucket (certain NO beyond it),
    lower edge for a tail-high bucket (certain YES beyond it).
    """
    lo_c, hi_c = market.settlement_interval_f()
    return hi_c if hi_c != float("inf") else lo_c

_GEOMETRY_CACHE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                               ".market_geometry.json")
_MONTHS = {m: i + 1 for i, m in enumerate(
    ["JAN", "FEB", "MAR", "APR", "MAY", "JUN",
     "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"])}
_TICKER_RE = re.compile(r"^(KXHIGH[A-Z]+)-(\d\d)([A-Z]{3})(\d\d)-")

# Margin bands used for the accuracy breakdown. The first band is the one the
# whole defect lives in: every wrong call in the 2026-08-26 audit replay had a
# margin under 0.1F, because a tenths-of-degC METAR lands within +-0.08F of an
# integer 93.2% of the time.
_BANDS = [(0.0, 0.1), (0.1, 0.5), (0.5, 1.0), (1.0, 2.0), (2.0, 5.0), (5.0, 1e9)]

# Bands for `excess_f` (observed - the exact certainty bound). Negative means the
# rule fired BEFORE certainty -- that is the defect, and every historical wrong
# call except the two sensor spikes lands there.
_EXCESS_BANDS = [(-1e9, -0.4), (-0.4, -0.2), (-0.2, 0.0),
                 (0.0, 0.5), (0.5, 1.0), (1.0, 2.0), (2.0, 1e9)]


def _series_to_icao(settings: Settings) -> dict[str, str]:
    return {series: icao for series, icao in settings.weather_series}


def _parse_ticker(ticker: str, series_map: dict[str, str]):
    """(icao, climate_date) for a KXHIGH<CITY>-YYMMMDD-<bucket> ticker."""
    m = _TICKER_RE.match(ticker)
    if m is None or m.group(1) not in series_map:
        return None
    _series, yy, mon, dd = m.groups()
    return series_map[m.group(1)], f"20{yy}-{_MONTHS[mon]:02d}-{int(dd):02d}"


def load_geometry(db_path: str, tickers: set[str]) -> dict[str, dict]:
    """floor/cap/strike_type per ticker, from the local cache.

    The cache is gitignored, so rebuild it with
    ``python -m weather_arb.backtest --strategy 1`` (which fetches settled
    markets) or point --geometry at a copy. Missing tickers are skipped and
    counted rather than guessed: inventing a strike would silently fabricate
    detections, which is exactly the class of error this script exists to catch.
    """
    if not os.path.exists(_GEOMETRY_CACHE):
        raise SystemExit(
            f"geometry cache not found: {_GEOMETRY_CACHE}\n"
            "It is gitignored. Regenerate it from Kalshi's public API, or copy "
            "one in, before replaying."
        )
    with open(_GEOMETRY_CACHE) as fh:
        geo = json.load(fh)
    return {t: g for t, g in geo.items() if t in tickers}


def load_observations(db_path: str, settings: Settings):
    """(icao, climate_date) -> time-ordered [(obs_utc, ingest_utc, temp_f)]."""
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    offsets = {s.icao: s.standard_utc_offset_hours for s in settings.stations}
    days: dict[tuple[str, str], list] = collections.defaultdict(list)
    rows = conn.execute(
        "SELECT icao, observed_at_utc, inserted_at_utc, temp_f FROM station_logs "
        "WHERE accepted = 1 AND temp_f IS NOT NULL ORDER BY observed_at_utc"
    ).fetchall()
    for icao, obs, ins, temp in rows:
        off = offsets.get(icao)
        if off is None:
            continue
        o = datetime.fromisoformat(obs)
        cdate = (o + timedelta(hours=off)).date().isoformat()
        days[(icao, cdate)].append((o, datetime.fromisoformat(ins), float(temp)))
    conn.close()
    return days


def load_daily_tmax(db_path: str) -> dict[tuple[str, str], float]:
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    out = {(i, d): float(t) for i, d, t in conn.execute(
        "SELECT icao, climate_date, tmax_f FROM daily_tmax")}
    conn.close()
    return out


def load_results(db_path: str) -> dict[str, str]:
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    out = {t: r for t, r in conn.execute(
        "SELECT ticker, result FROM market_results WHERE result IN ('yes','no')")}
    conn.close()
    return out


def replay(*, margin_f, days, tmax_final, results, geometry,
           series_map, use_daily_tmax: bool) -> list[dict]:
    """Every detection the rule makes, graded against settlement.

    ``margin_f`` is a float, or the string ``"legacy"`` to replay the exact
    pre-fix rule.
    """
    legacy = margin_f == "legacy"
    out: list[dict] = []
    for ticker, result in results.items():
        parsed = _parse_ticker(ticker, series_map)
        geo = geometry.get(ticker)
        if parsed is None or geo is None:
            continue
        icao, cdate = parsed
        market = WeatherMarket(
            ticker=ticker, venue=Venue.KALSHI, icao=icao,
            floor_strike_f=geo.get("floor"), cap_strike_f=geo.get("cap"),
            strike_type=str(geo.get("strike_type", "")),
        )
        boundary = market.cap_f if market.cap_f is not None else market.floor_f
        if boundary is None:
            continue

        fired = None
        if use_daily_tmax:
            final = tmax_final.get((icao, cdate))
            if final is None:
                continue
            det = (legacy_lock_in(market, final) if legacy
                   else market.deterministic_lock_in(final, margin_f=margin_f))
            if det is not None:
                fired = (det[0].value, None, None, final)
        else:
            series = days.get((icao, cdate))
            if not series:
                continue
            running = None
            for obs_utc, ins_utc, temp in series:
                running = temp if running is None else max(running, temp)
                det = (legacy_lock_in(market, running) if legacy
                       else market.deterministic_lock_in(running, margin_f=margin_f))
                if det is not None:
                    fired = (det[0].value, obs_utc, ins_utc, running)
                    break
        if fired is None:
            continue

        side, obs_utc, ins_utc, running = fired
        bound = certainty_bound_f(market)
        out.append({
            "ticker": ticker, "icao": icao, "climate_date": cdate,
            "side": side, "result": result, "correct": side == result,
            "running_max_f": running, "boundary_f": boundary,
            # Strike-relative (the audit's definition; used for the P8 buckets).
            "margin_f": running - boundary,
            # Shape-normalised: how far past MATHEMATICAL certainty we were.
            # Negative == fired early == the defect. Comparable across
            # between/greater/less, which strike-relative margin is not (a
            # `less` tail's YES region ends a degree below its cap strike).
            "excess_f": running - bound,
            "certainty_bound_f": bound,
            "strike_type": market.kind,
            "obs_utc": obs_utc.isoformat() if obs_utc else None,
            "ingest_utc": ins_utc.isoformat() if ins_utc else None,
            "ingest_lag_s": (ins_utc - obs_utc).total_seconds()
                            if obs_utc and ins_utc else None,
        })
    return out


def summarise(rows: list[dict]) -> dict:
    wrong = [r for r in rows if not r["correct"]]
    def _bands(bands, key):
        out = []
        for lo, hi in bands:
            sel = [r for r in rows if lo <= r[key] < hi]
            if sel:
                w = sum(1 for r in sel if not r["correct"])
                lbl = ("(-inf, %g)" % hi if lo <= -1e8 else
                       "[%g, inf)" % lo if hi >= 1e8 else "[%g, %g)" % (lo, hi))
                out.append({"band": lbl, "n": len(sel), "wrong": w,
                            "err_pct": 100.0 * w / len(sel)})
        return out
    by_band = _bands(_BANDS, "margin_f")
    by_excess = _bands(_EXCESS_BANDS, "excess_f")
    per_station = {}
    for r in rows:
        s = per_station.setdefault(r["icao"], {"n": 0, "wrong": 0})
        s["n"] += 1
        s["wrong"] += 0 if r["correct"] else 1
    per_side = {}
    for r in rows:
        s = per_side.setdefault(r["side"], {"n": 0, "wrong": 0})
        s["n"] += 1
        s["wrong"] += 0 if r["correct"] else 1
    return {
        "detections": len(rows), "correct": len(rows) - len(wrong),
        "wrong": len(wrong),
        "accuracy_pct": 100.0 * (len(rows) - len(wrong)) / len(rows) if rows else None,
        "by_margin_band": by_band, "by_excess_band": by_excess,
        "by_station": per_station, "by_side": per_side,
        "wrong_rows": wrong,
    }


def render(label: str, s: dict) -> str:
    L = [f"  {label}",
         f"    detections {s['detections']:>5d}   correct {s['correct']:>5d}   "
         f"WRONG {s['wrong']:>3d}   accuracy "
         + (f"{s['accuracy_pct']:.2f}%" if s['accuracy_pct'] is not None else "n/a")]
    L.append("    by margin band (observed - strike):")
    for b in s["by_margin_band"]:
        L.append(f"      {b['band']:>14s}  n={b['n']:>4d}  wrong={b['wrong']:>3d}"
                 f"  ({b['err_pct']:.1f}%)")
    L.append("    by excess band (observed - certainty bound; <0 == fired early):")
    for b in s["by_excess_band"]:
        L.append(f"      {b['band']:>14s}  n={b['n']:>4d}  wrong={b['wrong']:>3d}"
                 f"  ({b['err_pct']:.1f}%)")
    L.append("    by station:")
    for k in sorted(s["by_station"]):
        v = s["by_station"][k]
        L.append(f"      {k}  n={v['n']:>4d}  wrong={v['wrong']:>3d}")
    L.append("    by side:")
    for k in sorted(s["by_side"]):
        v = s["by_side"][k]
        L.append(f"      {k:>3s}  n={v['n']:>4d}  wrong={v['wrong']:>3d}")
    return "\n".join(L)


def main() -> int:
    settings = Settings.from_env()
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", default=settings.db_path)
    ap.add_argument("--margins", nargs="*", default=None,
                    help='margins to replay; "legacy" replays the exact pre-fix '
                         'rule (default: legacy and the configured margin)')
    ap.add_argument("--use-daily-tmax", action="store_true",
                    help="grade the guarded final daily_tmax instead of raw station_logs")
    ap.add_argument("--json", help="write the full per-detection records here")
    ap.add_argument("--show-wrong", type=int, default=20)
    args = ap.parse_args()

    margins = (["legacy", settings.risk.lockin_margin_f] if args.margins is None
               else [m if m == "legacy" else float(m) for m in args.margins])

    series_map = _series_to_icao(settings)
    results = load_results(args.db)
    geometry = load_geometry(args.db, set(results))
    days = load_observations(args.db, settings)
    tmax_final = load_daily_tmax(args.db)

    print("=" * 78)
    print("  Strategy-1 lock-in replay -- EVERY detection graded against settlement")
    print("=" * 78)
    print(f"  ledger            : {args.db}")
    print(f"  settled markets   : {len(results)}   with geometry: {len(geometry)}")
    print(f"  station-days      : {len(days)}")
    print(f"  observation basis : "
          f"{'daily_tmax (guarded, final value only)' if args.use_daily_tmax else 'station_logs running max (raw)'}")
    print()

    payload = {}
    summaries = {}
    for margin in margins:
        rows = replay(margin_f=margin, days=days, tmax_final=tmax_final,
                      results=results, geometry=geometry, series_map=series_map,
                      use_daily_tmax=args.use_daily_tmax)
        s = summarise(rows)
        summaries[margin] = s
        payload[str(margin)] = rows
        tag = ("LEGACY RULE (pre-2026-08-26, bare > against the integer strike)"
               if margin == "legacy" else f"margin {margin} degF")
        print(render(tag, s))
        if s["wrong"] and args.show_wrong:
            print(f"    wrong calls (first {args.show_wrong}):")
            for r in s["wrong_rows"][:args.show_wrong]:
                print(f"      {r['climate_date']}  {r['ticker']:<26s} said {r['side'].upper():<3s}"
                      f" settled {r['result'].upper():<3s}  running {r['running_max_f']:.2f}"
                      f"  bound {r['certainty_bound_f']:.1f}  excess {r['excess_f']:+.2f}")
        print()

    if len(margins) >= 2:
        lo, hi = margins[0], margins[-1]
        a, b = summaries[lo], summaries[hi]
        print("-" * 78)
        print(f"  DELTA  (margin {lo} -> {hi})")
        print(f"    detections : {a['detections']:>5d} -> {b['detections']:>5d}"
              f"   retained {100.0 * b['detections'] / a['detections']:.1f}%"
              if a["detections"] else "    detections : n/a")
        print(f"    wrong calls: {a['wrong']:>5d} -> {b['wrong']:>5d}")
        print("-" * 78)

    if args.json:
        with open(args.json, "w") as fh:
            json.dump(payload, fh, indent=1, default=str)
        print(f"  wrote {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
