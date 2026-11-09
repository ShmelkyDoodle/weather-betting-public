#!/usr/bin/env python3
"""What does the sure side COST at t1 -- before the hourly METAR tells the market?

THE QUESTION THIS CLOSES.  `scripts/t1_depth_probe.py` measures *depth* at t1 and
must accrue live samples slowly.  But depth only matters if there is a *price*
worth taking.  Price, unlike depth, is in Kalshi's candlestick history, so this
can be answered over months instead of a week -- and if the sure side is already
quoted ~100c at t1, the depth question is moot and the latency edge is dead.

WHAT t1 MEANS HERE, and why it is not the archive-flattered version.  t1 is the
first SUB-HOURLY observation whose running max makes the outcome certain under
`WeatherMarket.deterministic_lock_in` -- the production rule, not a re-derivation.
Two corrections the earlier $1,183 estimate did not make:

* **Latency is charged.**  IEM `asos.py` publishes at a measured median of
  ~18 min (first-sighting, 2026-09-08).  We could not have acted at t1's *valid*
  time, so the price is read at `t1 + --latency-min`, not at t1.
* **Only the T-group counts.**  Whole-degree body groups quantise to 1.8 F and
  proved the settled max on 0 of 60 station-days, so rows without a T-group carry
  no usable certainty and are dropped.

t2 is the first ROUTINE hourly METAR proving the same thing -- when the market
learns.  The edge, if any, lives between the two.

⚠ TRAPS THIS REPO HAS ALREADY HIT, all avoided here explicitly:
* `period_interval` is in **minutes**, not seconds.  Passing 60 yields HOURLY
  bars whose close is up to 59 min AFTER the decision -- look-ahead bias worth
  ~$0.81/trade.  `_candle_series(period_minutes=1)` is used.
* `_candle_index_at` returns the LAST candle when the timestamp is past the
  series, which would silently price a stale quote.  Every lookup here is
  rejected unless a candle lands within `--tolerance-min` of the request.
* "Priced at 100c" and "no bid at all" are indistinguishable in a naive read and
  economically opposite.  They are counted separately.
* Kalshi's volume fields are `volume_fp`, not `volume` (handled in
  `_candle_series`).

Read-only: Kalshi's public candlestick + settled-market endpoints and IEM.  No
ledger access, no trading, no tracker change.

    PYTHONPATH=. .venv/bin/python scripts/t1_price_history.py --days 45
    PYTHONPATH=. .venv/bin/python scripts/t1_price_history.py --days 45 --json out.json
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import io
import json
import os
import statistics
import sys
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import aiohttp

from weather_arb import timeutils
from weather_arb.backtest import _candle_series, _candle_index_at, _settled_markets
from weather_arb.models import Side, Venue, WeatherMarket
from weather_arb.polymarket_basis import metar_temp_f
from weather_arb.settlement_basis import SERIES_ICAO
from t1_depth_probe import observed_lower_bound_f

_IEM_ASOS = "https://mesonet.agron.iastate.edu/cgi-bin/request/asos.py"
_UA = "weather-arb-research (read-only t1 price history)"

# Only stations with a real 5-minute feed can produce a t1 at all (measured
# 2026-09-08): KNYC is ~12 rows/day and KDEN has none.
STATION: dict[str, tuple[str, str, int]] = {
    "KMDW": ("MDW", "IL_ASOS", -6), "KMIA": ("MIA", "FL_ASOS", -5),
    "KAUS": ("AUS", "TX_ASOS", -6), "KLAX": ("LAX", "CA_ASOS", -8),
    "KPHL": ("PHL", "PA_ASOS", -5),
}
SERIES = {s: i for s, i in SERIES_ICAO.items() if i in STATION}


def is_routine(metar: str) -> bool:
    return "RMK" in metar and "SLP" in metar and "AUTO" not in metar.split("RMK")[0]


async def fetch_obs(session: aiohttp.ClientSession, icao: str,
                    lo: datetime, hi: datetime) -> list[tuple[datetime, float, bool]]:
    iem_id, network, _ = STATION[icao]
    params = {"station": iem_id, "network": network, "data": "metar", "tz": "UTC",
              "format": "onlycomma", "latlon": "no", "missing": "empty",
              "year1": lo.year, "month1": lo.month, "day1": lo.day,
              "year2": hi.year, "month2": hi.month, "day2": hi.day}
    async with session.get(_IEM_ASOS, params=params) as resp:
        if resp.status >= 400:
            return []
        text = await resp.text()
    out = []
    for row in csv.DictReader(io.StringIO(text)):
        m = (row.get("metar") or "").strip()
        if not m:
            continue
        val, src = metar_temp_f(m)
        if val is None or src != "t_group":     # T-group only; see module docstring
            continue
        try:
            ts = datetime.strptime(row["valid"], "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc)
        except (ValueError, KeyError):
            continue
        out.append((ts, val, is_routine(m)))
    out.sort(key=lambda r: r[0])
    return out


def certainty_times(market: WeatherMarket, obs: list[tuple[datetime, float, bool]]
                    ) -> tuple[Optional[datetime], Optional[datetime], Optional[Side]]:
    """(t1 sub-hourly, t2 routine-only, certain side) using the production rule."""
    t1 = t2 = None
    side: Optional[Side] = None
    run_all = run_rt = None
    for ts, val, routine in obs:
        # Provable lower bound, NOT the raw reading. The sub-hourly T-group is
        # whole degC padded to look like tenths (100.0% end in .0, n=17,007), so
        # a raw comparison invents certainty: 4 of 18 sub-hourly-only lock-ins
        # were wrong against settlement before this was applied.
        lb = observed_lower_bound_f(val, routine)
        run_all = lb if run_all is None else max(run_all, lb)
        if t1 is None:
            hit = market.deterministic_lock_in(run_all)
            if hit is not None:
                t1, side = ts, hit[0]
        if routine:
            run_rt = lb if run_rt is None else max(run_rt, lb)
            if t2 is None and market.deterministic_lock_in(run_rt) is not None:
                t2 = ts
        if t1 is not None and t2 is not None:
            break
    return t1, t2, side


def price_at(candles, when: datetime, side: Side, tol_min: float
             ) -> tuple[Optional[int], str]:
    """Sure-side ask at ``when``, or a reason string.

    Rejects a candle further than ``tol_min`` from the request: `_candle_index_at`
    clamps to the last candle past the series end, which would silently return a
    stale quote as if it were live.
    """
    if not candles:
        return None, "no_candles"
    ts = int(when.timestamp())
    i = _candle_index_at(candles, ts)
    if i is None:
        return None, "no_candles"
    c = candles[i]
    if abs(c.ts - ts) > tol_min * 60:
        return None, "no_candle_near"
    ask = c.yes_ask if side is Side.YES else c.no_ask
    return (ask, "ok") if ask is not None else (None, "no_quote")


async def collect(days: int, latency_min: float, tol_min: float) -> list[dict[str, Any]]:
    end = datetime.now(timezone.utc).date() - timedelta(days=1)
    start = end - timedelta(days=days - 1)
    rows: list[dict[str, Any]] = []
    timeout = aiohttp.ClientTimeout(total=180)
    async with aiohttp.ClientSession(timeout=timeout, headers={"User-Agent": _UA}) as session:
        min_close = int(datetime.now(timezone.utc).timestamp()) - (days + 3) * 86400
        for series, icao in SERIES.items():
            offset = STATION[icao][2]
            obs = await fetch_obs(session, icao,
                                  datetime.combine(start, datetime.min.time()),
                                  datetime.combine(end + timedelta(days=2), datetime.min.time()))
            by_day: dict[str, list] = {}
            for ts, val, rt in obs:
                by_day.setdefault(timeutils.climate_date_str(ts, offset), []).append((ts, val, rt))
            settled = await _settled_markets(session, series, min_close)
            for raw in settled:
                ticker = str(raw.get("ticker") or "")
                parts = ticker.split("-")
                if len(parts) < 3:
                    continue
                floor, cap = raw.get("floor_strike"), raw.get("cap_strike")
                if floor is None and cap is None:
                    continue
                m = WeatherMarket(
                    ticker=ticker, venue=Venue.KALSHI, icao=icao,
                    floor_strike_f=None if floor is None else float(floor),
                    cap_strike_f=None if cap is None else float(cap),
                    strike_type=str(raw.get("strike_type") or ""))
                day = _event_day(ticker)
                dayobs = by_day.get(day or "")
                if not dayobs or len(dayobs) < 100:      # need real sub-hourly coverage
                    continue
                t1, t2, side = certainty_times(m, dayobs)
                if t1 is None or side is None:
                    continue
                act = t1 + timedelta(minutes=latency_min)
                if t2 is not None and act >= t2:
                    lead = 0.0                            # latency ate the whole lead
                else:
                    lead = ((t2 - act).total_seconds() / 60) if t2 else float("inf")
                cs = await _candle_series(session, series, ticker,
                                          int(act.timestamp()),
                                          int((t2 or act).timestamp()), period_minutes=1)
                p1, why1 = price_at(cs, act, side, tol_min)
                p2, why2 = (price_at(cs, t2, side, tol_min) if t2 else (None, "no_t2"))
                rows.append({
                    "ticker": ticker, "icao": icao, "date": day,
                    "side": side.value, "result": raw.get("result"),
                    "t1_valid": t1.isoformat(), "t1_actionable": act.isoformat(),
                    "t2": t2.isoformat() if t2 else None,
                    "lead_min": None if lead == float("inf") else round(lead, 1),
                    "hourly_never_proves": t2 is None,
                    "price_t1": p1, "why_t1": why1,
                    "price_t2": p2, "why_t2": why2,
                })
                await asyncio.sleep(0.15)
    return rows


def _event_day(ticker: str) -> Optional[str]:
    from weather_arb.settlement_basis import event_date
    return event_date(ticker)


def analyze(rows: list[dict[str, Any]], ceiling: int) -> str:
    out = ["=" * 78, "  WHAT THE SURE SIDE COSTS AT t1 (latency charged) vs t2 (market knows)",
           "=" * 78, ""]
    n = len(rows)
    out.append(f"  certainty events with a sub-hourly t1: {n}")
    if not n:
        out.append("  (nothing to report)")
        return "\n".join(out)

    # Safety: the production rule's call must match the official settlement.
    wrong = [r for r in rows if r.get("result") in ("yes", "no")
             and ((r["side"] == "yes") != (r["result"] == "yes"))]
    out.append(f"  wrong calls vs official settlement    : {len(wrong)} / "
               f"{sum(1 for r in rows if r.get('result') in ('yes','no'))}")
    lead = [r["lead_min"] for r in rows if r["lead_min"] is not None]
    if lead:
        out.append(f"  usable lead after {ceiling and ''}latency        : median "
                   f"{statistics.median(lead):.0f} min   zero on "
                   f"{sum(1 for x in lead if x <= 0)}/{len(lead)}")
    out.append(f"  hourly METAR NEVER proves it         : "
               f"{sum(1 for r in rows if r['hourly_never_proves'])} / {n}")
    out.append("")

    def panel(label: str, key: str, why: str) -> None:
        prices = [r[key] for r in rows if isinstance(r[key], int)]
        noq = sum(1 for r in rows if r[why] == "no_quote")
        nocand = sum(1 for r in rows if r[why] in ("no_candles", "no_candle_near"))
        out.append(f"  {label}")
        out.append(f"    quoted            : {len(prices)} / {n}"
                   f"   no quote at all: {noq}   no candle: {nocand}")
        if prices:
            ps = sorted(prices)
            cheap = [p for p in ps if p < ceiling]
            out.append(f"    price of the sure side: median {statistics.median(ps):.0f}c"
                       f"   mean {statistics.mean(ps):.1f}c"
                       f"   p25 {ps[len(ps)//4]}c   p75 {ps[3*len(ps)//4]}c")
            out.append(f"    at exactly 100c   : {sum(1 for p in ps if p >= 100)} / {len(ps)}"
                       f"   ({100*sum(1 for p in ps if p>=100)/len(ps):.0f}%)")
            out.append(f"    below the {ceiling}c ceiling (tradeable): {len(cheap)} / {len(ps)}"
                       f"   ({100*len(cheap)/len(ps):.0f}%)"
                       + (f"   median {statistics.median(cheap):.0f}c" if cheap else ""))
    panel("AT t1 + latency  (before the hourly METAR -- our claimed edge)", "price_t1", "why_t1")
    out.append("")
    panel("AT t2            (hourly METAR lands -- the market knows)", "price_t2", "why_t2")
    out += ["", "  HOW TO READ IT. If the t1 panel is mostly 100c or 'no quote', there is",
            "  nothing to buy before the market knows and the latency edge is DEAD --",
            "  no faster feed fixes it, and the depth probe is moot. A large 'below the",
            "  ceiling' share at t1 that collapses by t2 is the edge actually existing.",
            "  Note this is TOP-OF-BOOK from candles: it is an upper bound on what is",
            "  really obtainable, since it ignores how little may rest at that price.",
            "=" * 78]
    return "\n".join(out)


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--days", type=int, default=45)
    ap.add_argument("--latency-min", type=float, default=18.0,
                    help="feed publication latency to charge (default 18, measured)")
    ap.add_argument("--tolerance-min", type=float, default=10.0,
                    help="reject a candle further than this from the requested time")
    ap.add_argument("--ceiling", type=int, default=98,
                    help="lock-in price ceiling in cents (default 98, the live value)")
    ap.add_argument("--json", default=None)
    args = ap.parse_args(argv)
    rows = asyncio.run(collect(max(7, args.days), args.latency_min, args.tolerance_min))
    print(analyze(rows, args.ceiling))
    if args.json:
        with open(args.json, "w", encoding="utf-8") as fh:
            json.dump(rows, fh, indent=2)
        print(f"\nwrote {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
