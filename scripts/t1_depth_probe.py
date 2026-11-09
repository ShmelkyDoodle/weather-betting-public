#!/usr/bin/env python3
"""Log the Kalshi order book at **t1** -- the moment a bucket outcome becomes
provable from sub-hourly observations, BEFORE the hourly METAR tells the market.

WHY THIS EXISTS, and why the existing probe cannot answer it.  The engine's
in-tracker `lock-in L2 depth` telemetry fires when `_evaluate_lock_in` first sees
certainty -- which is when the **hourly METAR** lands, i.e. t2.  By then the
market knows too, and the first live batch found exactly that: 10 of 10
detections rejected `no_quote`, depth 0 at signal against a control median of
2,443 contracts.  On a decided market the sure side's book empties.  CLAUDE.md
states the limitation plainly: that instrument "measures depth at the moment the
edge is already gone."

The whole thesis is that a **t1** book looks different, because at t1 the market
still believes the outcome is uncertain and should still be quoting two-sided.
That has never been observed, because no t1-capable feed was thought to exist.

One does.  Measured 2026-09-08 (see CROSS-VENUE-STEP1.md):

    feed                    cadence  precision      latency   proves settled max
    weather.gov/Synoptic     5 min   whole degC      8.8 min   0 / 60 days
    aviationweather.gov      hourly  full T-group    ~1 min    n/a
    IEM asos.py              5 min   full T-group   18.0 min   56 / 60 days

IEM `asos.py` (NOT `asos1min.py`, the endpoint recorded elsewhere as ~2 days
stale) carries the 5-minute MADIS reports with their `RMK T`-groups.  Median lead
over the hourly METAR is 33 min; net of 18 min publication latency the usable
window is ~15 min.

WHAT IT RECORDS.  For each market, on the first cycle where the outcome becomes
certain, one JSONL row with the proving observation, the book at that instant,
and -- the load-bearing field -- `hourly_would_know`: whether the routine-METAR
running max ALSO proves it.  **A row with `hourly_would_know: false` is a genuine
t1 sample**, taken while the market cannot yet know.  Rows with `true` are t2
samples and reproduce the existing telemetry; they are kept as the control.

⚠ THE PRIOR IS BAD, AND THE FIRST SWEEP SHOWED WHY.  "The hourly METAR has not
yet PROVED it" is not the same as "the market does not KNOW."  Markets price
probability, not proof.  When Austin's running max is 100.4 F, the [98,99] bucket
is worth ~0 to every participant long before anyone can formally prove it, so the
sure side is quoted at 99-100c or not at all.  On the first live sweep all 10
transitions -- including the one genuine T1 sample -- showed **zero depth** on the
sure side.  n=1 for T1 proves nothing, but do not expect this probe to rescue the
latency edge: it is at least as likely to bury it, and that is a useful outcome.

WHAT IT DOES NOT DO.  It never trades, never authenticates (Kalshi's orderbook
endpoint is public), never touches `weather_arb.sqlite3`, and never writes to the
ledger.  It does not modify, restart or reconfigure the tracker, and it installs
no cron job or launch agent -- run it in the foreground, or background it
yourself.  It is safe to run alongside the frozen dry-run experiment; the only
shared resource is Kalshi's public API, so the default cadence is deliberately
slow and the depth pull is latched once per market per climate day.

Usage
-----
    PYTHONPATH=. .venv/bin/python scripts/t1_depth_probe.py --once
    PYTHONPATH=. .venv/bin/python scripts/t1_depth_probe.py --out t1.jsonl
    PYTHONPATH=. .venv/bin/python scripts/t1_depth_probe.py --analyze t1.jsonl

Analyse with `--analyze`, which splits t1 from t2 samples and reports depth on
each.  The question it answers: **is there anything to buy before the market
knows?**
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
from weather_arb.backtest import _kalshi_get
from weather_arb.config import Settings
from weather_arb.market_client import KalshiClient
from weather_arb.market_discovery import _parse_market
from weather_arb.models import Side, Venue, WeatherMarket
from weather_arb.polymarket_basis import metar_temp_f
from weather_arb.settlement_basis import SERIES_ICAO

_IEM_ASOS = "https://mesonet.agron.iastate.edu/cgi-bin/request/asos.py"
_UA = "weather-arb-research (read-only t1 depth probe)"

# ICAO -> (IEM id, IEM network, fixed LST offset).  Kalshi's climate day is fixed
# Local Standard Time with no DST -- see timeutils; do not "fix" this to civil time.
STATION: dict[str, tuple[str, str, int]] = {
    "KNYC": ("NYC", "NY_ASOS", -5), "KMDW": ("MDW", "IL_ASOS", -6),
    "KMIA": ("MIA", "FL_ASOS", -5), "KAUS": ("AUS", "TX_ASOS", -6),
    "KDEN": ("DEN", "CO_ASOS", -7), "KLAX": ("LAX", "CA_ASOS", -8),
    "KPHL": ("PHL", "PA_ASOS", -5),
}
# Stations with a real 5-minute sub-hourly feed (measured 2026-09-08, 2 days):
# KMDW 509 rows, KMIA/KAUS/KLAX/KPHL ~5-min, KNYC only ~12/day, KDEN ZERO.
# KDEN cannot produce a t1 sample at all; it is polled only so its absence is
# recorded rather than silently assumed.
SUBHOURLY_OK = frozenset({"KMDW", "KMIA", "KAUS", "KLAX", "KPHL"})


def is_routine(metar: str) -> bool:
    """A routine hourly METAR -- what the market reads -- vs a sub-hourly report."""
    return "RMK" in metar and "SLP" in metar and "AUTO" not in metar.split("RMK")[0]


# Quantisation of the reported temperature, in degF, by channel.
#
# ⚠⚠ THE TRAP THAT MAKES THIS FILE NECESSARY.  The 5-minute MADIS reports carry a
# `T`-group, which LOOKS like tenths-of-degC precision.  It is not: measured over
# 17,007 reports at 4 stations, **100.0%** of sub-hourly T-groups end in `.0`,
# against 8-12% for routine METARs (which is chance).  The sub-hourly sensor
# reports WHOLE degrees Celsius and pads a zero.  So a sub-hourly `T0310` means
# the true temperature is somewhere in [30.5, 31.5) degC = [86.9, 88.7) degF --
# a 1.8 degF band -- not 87.8 +- 0.1.
#
# Treating it as exact manufactures certainty that is not there.  Measured
# consequence: of 18 lock-ins that ONLY the sub-hourly channel proved, **4 were
# wrong** against official settlement (22%), while all 111 that the routine METAR
# also proved were correct.  A wrong lock-in loses the whole contract.
SUBHOURLY_QUANTISATION_F = 0.9      # +-0.5 degC
ROUTINE_QUANTISATION_F = 0.05       # genuine tenths of degC


def observed_lower_bound_f(value_f: float, routine: bool) -> float:
    """The lowest true temperature consistent with a reported reading.

    This is what may be fed to :meth:`WeatherMarket.deterministic_lock_in`, whose
    own margin then handles CLI rounding on top.  Passing the raw reading instead
    is the error described above.
    """
    return value_f - (ROUTINE_QUANTISATION_F if routine else SUBHOURLY_QUANTISATION_F)


async def fetch_observations(session: aiohttp.ClientSession, icao: str, day: str
                             ) -> list[tuple[datetime, float, bool]]:
    """``(valid_utc, temp_f, routine)`` for the UTC days spanning one climate day.

    Temperature comes from the METAR ``T``-group only.  The whole-degree body
    group is deliberately NOT used as a fallback: it quantises to 1.8 F, which was
    measured to prove the settled max on 0 of 60 station-days.  A row without a
    T-group carries no usable certainty and is dropped.
    """
    d = datetime.strptime(day, "%Y-%m-%d").date()
    iem_id, network, _ = STATION[icao]
    lo, hi = d, d + timedelta(days=1)
    params = {"station": iem_id, "network": network, "data": "metar", "tz": "UTC",
              "format": "onlycomma", "latlon": "no", "missing": "empty",
              "year1": lo.year, "month1": lo.month, "day1": lo.day,
              "year2": hi.year, "month2": hi.month, "day2": hi.day}
    async with session.get(_IEM_ASOS, params=params) as resp:
        if resp.status >= 400:
            return []
        text = await resp.text()
    out: list[tuple[datetime, float, bool]] = []
    for row in csv.DictReader(io.StringIO(text)):
        m = (row.get("metar") or "").strip()
        if not m:
            continue
        val, src = metar_temp_f(m)
        if val is None or src != "t_group":
            continue
        try:
            ts = datetime.strptime(row["valid"], "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc)
        except (ValueError, KeyError):
            continue
        out.append((ts, val, is_routine(m)))
    out.sort(key=lambda r: r[0])
    return out


def running_max(obs: list[tuple[datetime, float, bool]], day: str, offset: int,
                routine_only: bool) -> tuple[Optional[float], Optional[datetime]]:
    """Max PROVABLE lower bound over the LST climate day, and its timestamp."""
    best: Optional[float] = None
    at: Optional[datetime] = None
    for ts, val, routine in obs:
        if routine_only and not routine:
            continue
        if timeutils.climate_date_str(ts, offset) != day:
            continue
        # Compare PROVABLE lower bounds, not raw readings -- see the note on
        # SUBHOURLY_QUANTISATION_F. A raw sub-hourly reading is up to 0.9F above
        # the coldest temperature consistent with it.
        lb = observed_lower_bound_f(val, routine)
        if best is None or lb > best:
            best, at = lb, ts
    return best, at


async def live_markets(session: aiohttp.ClientSession, settings: Settings,
                       series: str, day: str) -> list[WeatherMarket]:
    """Today's open markets for one series, as WeatherMarket.

    Parsed by ``market_discovery._parse_market`` -- the SAME parser the live
    engine discovers markets with -- so the strike geometry this probe reasons
    about cannot drift from production.  Re-deriving floor/cap here is exactly
    the class of measurement bug that made ``backtest --strategy 1`` wrong for a
    month (it ignored ``strike_type`` and fired certain-YES a degree early).
    """
    icao = SERIES_ICAO[series]
    url = f"{settings.kalshi.rest_base_url}/markets"
    payload = await _kalshi_get(session, url, {"series_ticker": series,
                                               "status": "open", "limit": 200})
    out: list[WeatherMarket] = []
    for raw in payload.get("markets") or []:
        if not isinstance(raw, dict):
            continue
        m = _parse_market(raw, icao)
        if m is not None and m.event_date == day:
            out.append(m)
    return out


async def one_cycle(client: KalshiClient, session: aiohttp.ClientSession,
                    settings: Settings, latch: set[tuple[str, str]],
                    out_path: str, verbose: bool) -> int:
    """One sweep.  Returns how many new certainty transitions were recorded."""
    now = datetime.now(timezone.utc)
    written = 0
    for series, icao in SERIES_ICAO.items():
        offset = STATION[icao][2]
        day = timeutils.climate_date_str(now, offset)
        obs = await fetch_observations(session, icao, day)
        sub_max, sub_at = running_max(obs, day, offset, routine_only=False)
        rt_max, rt_at = running_max(obs, day, offset, routine_only=True)
        if sub_max is None:
            if verbose:
                print(f"  {icao} {day}: no observations yet")
            continue
        try:
            markets = await live_markets(session, settings, series, day)
        except Exception as exc:                     # never let one series kill the sweep
            print(f"  {icao}: market fetch failed: {exc}")
            continue
        if verbose:
            print(f"  {icao} {day}: sub_max={sub_max:.2f} hourly_max="
                  f"{rt_max if rt_max is None else round(rt_max, 2)} "
                  f"markets={len(markets)}")
        if not markets:
            print(f"  {icao}: no open markets returned (rate limited?)")
        for m in markets:
            key = (m.ticker, day)
            if key in latch:
                continue
            certain = m.deterministic_lock_in(sub_max)
            if certain is None:
                continue
            side, action = certain
            latch.add(key)
            # Would the HOURLY-only max prove it too?  False => the market cannot
            # yet know, and this is a genuine t1 sample.  This single field is what
            # separates the new measurement from the existing t2 telemetry.
            hourly_knows = (rt_max is not None
                            and m.deterministic_lock_in(rt_max) is not None)
            rec: dict[str, Any] = {
                "logged_utc": now.isoformat(), "ticker": m.ticker, "icao": icao,
                "climate_date": day, "strike_type": m.strike_type,
                "floor": m.floor_strike_f, "cap": m.cap_strike_f,
                "certain_side": side.value, "action": action.value,
                "subhourly_max_f": sub_max,
                "subhourly_max_at": sub_at.isoformat() if sub_at else None,
                "hourly_max_f": rt_max,
                "hourly_max_at": rt_at.isoformat() if rt_at else None,
                "hourly_would_know": bool(hourly_knows),
                "is_t1_sample": not bool(hourly_knows),
                "station_has_subhourly": icao in SUBHOURLY_OK,
                "observation_age_min": (
                    round((now - sub_at).total_seconds() / 60, 1) if sub_at else None),
            }
            try:
                book = await client.get_orderbook(m.ticker, depth=32)
                sweep = book.get_l2_sweep_metrics(side, target_size=100)
                capped = book.get_l2_sweep_metrics(side, target_size=100,
                                                   max_price_cents=98)
                rec["book"] = {k: sweep.get(k) for k in
                               ("top_levels", "vwap_cents", "book_cleared",
                                "fillable_size", "depth_size", "top_ask_cents")}
                rec["book_under_98c"] = {k: capped.get(k) for k in
                                         ("vwap_cents", "fillable_size", "depth_size")}
            except Exception as exc:
                rec["book_error"] = str(exc)
            # CONTROL, recorded once per event per day: the depth on every OTHER
            # market in the same event at the same instant.  Without it a bare
            # "depth 0" is uninterpretable -- it could mean the venue is dead.
            # Verified 2026-09-08 that it is not: undecided markets in the same
            # ladder quote two-sided with 446-1767 contracts resting.
            ekey = (m.ticker.rsplit("-", 1)[0], day)
            if ekey not in latch:
                latch.add(ekey)
                ladder: list[dict[str, Any]] = []
                for other in markets:
                    try:
                        ob = await client.get_orderbook(other.ticker, depth=32)
                        sw = ob.get_l2_sweep_metrics(side, target_size=100)
                        ladder.append({
                            "ticker": other.ticker,
                            "certain": other.deterministic_lock_in(sub_max) is not None,
                            "depth_size": sw.get("depth_size"),
                            "top_ask_cents": sw.get("top_ask_cents"),
                        })
                    except Exception as exc:
                        ladder.append({"ticker": other.ticker, "error": str(exc)})
                rec["event_ladder_control"] = ladder
            with open(out_path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(rec) + "\n")
            written += 1
            tag = "T1 (market blind)" if not hourly_knows else "t2 (market knows)"
            depth = (rec.get("book") or {}).get("depth_size")
            print(f"  [{tag}] {m.ticker} buy {side.value} "
                  f"max={sub_max:.2f}F depth={depth} "
                  f"top={(rec.get('book') or {}).get('top_ask_cents')}c")
    return written


def analyze(path: str) -> str:
    rows = [json.loads(l) for l in open(path, encoding="utf-8") if l.strip()]
    t1 = [r for r in rows if r.get("is_t1_sample")]
    t2 = [r for r in rows if not r.get("is_t1_sample")]
    out = ["=" * 76, "  T1 DEPTH PROBE -- is there anything to buy before the market knows?",
           "=" * 76, "",
           f"  samples: {len(rows)}   t1 (market blind): {len(t1)}   t2 (market knows): {len(t2)}", ""]

    def panel(label: str, xs: list[dict[str, Any]]) -> None:
        out.append(f"  {label} (n={len(xs)})")
        if not xs:
            out.append("    (none yet)")
            return
        depth = [(r.get("book") or {}).get("depth_size") for r in xs]
        depth = [d for d in depth if isinstance(d, (int, float))]
        tops = [(r.get("book") or {}).get("top_ask_cents") for r in xs]
        quoted = [t for t in tops if isinstance(t, (int, float))]
        fill = [(r.get("book_under_98c") or {}).get("fillable_size") for r in xs]
        fill = [f for f in fill if isinstance(f, (int, float))]
        out.append(f"    no quote at all        : {len(xs) - len(quoted)} / {len(xs)}")
        if depth:
            out.append(f"    depth on the sure side : median {statistics.median(depth):.0f}"
                       f"  min {min(depth):.0f}  max {max(depth):.0f}")
        if quoted:
            out.append(f"    top ask                : median {statistics.median(quoted):.0f}c"
                       f"  (100c = fully priced, no edge)")
        if fill:
            out.append(f"    fillable under the 98c ceiling: median {statistics.median(fill):.0f}"
                       f"  of a 100-lot;  zero on {sum(1 for f in fill if f == 0)}/{len(fill)}")
    panel("T1 -- proved by sub-hourly, hourly METAR does NOT yet prove it", t1)
    out.append("")
    panel("t2 -- hourly METAR already proves it (reproduces existing telemetry)", t2)
    # Same-instant control: undecided markets in the same event ladder.
    und, dec = [], []
    for r in rows:
        for e in r.get("event_ladder_control") or []:
            d = e.get("depth_size")
            if not isinstance(d, (int, float)):
                continue
            (dec if e.get("certain") else und).append(d)
    out.append("")
    out.append(f"  SAME-INSTANT CONTROL (other markets in the same event ladder)")
    if und or dec:
        for lbl, xs in (("still undecided", und), ("already certain", dec)):
            if xs:
                out.append(f"    {lbl:<18} n={len(xs):<4} median depth "
                           f"{statistics.median(xs):.0f}   zero on "
                           f"{sum(1 for x in xs if x == 0)}/{len(xs)}")
        out.append("    ^ if 'undecided' has depth and 'already certain' does not, the book")
        out.append("      is alive and it is CERTAINTY that empties it -- not a dead venue.")
    else:
        out.append("    (none yet)")
    out += ["", "  READ IT THIS WAY. The t2 panel is expected to show an empty book -- that",
            "  is the already-known result. The question is whether T1 differs. If T1 also",
            "  shows no quotes or zero fillable size, the latency edge is unreachable and",
            "  no faster feed fixes it. Note n accrues slowly: a lock-in is rare, and only",
            "  5 of 7 cities have a sub-hourly feed at all (KDEN has none).", "=" * 76]
    return "\n".join(out)


async def run(args: argparse.Namespace) -> int:
    settings = Settings.from_env()
    client = KalshiClient(settings)
    latch: set[tuple[str, str]] = set()
    timeout = aiohttp.ClientTimeout(total=120)
    n = 0
    try:
        async with aiohttp.ClientSession(timeout=timeout,
                                         headers={"User-Agent": _UA}) as session:
            while True:
                stamp = datetime.now(timezone.utc).strftime("%H:%M:%SZ")
                print(f"[{stamp}] sweep")
                n += await one_cycle(client, session, settings, latch,
                                     args.out, args.verbose)
                if args.once:
                    break
                await asyncio.sleep(args.interval)
    except KeyboardInterrupt:
        print("\ninterrupted")
    finally:
        await client.close()
    print(f"recorded {n} new certainty transitions -> {args.out}")
    return 0


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--out", default="t1_depth.jsonl", help="JSONL output path")
    ap.add_argument("--interval", type=float, default=300.0,
                    help="seconds between sweeps (default 300, matching the 5-min feed)")
    ap.add_argument("--once", action="store_true", help="one sweep, then exit")
    ap.add_argument("--verbose", action="store_true")
    ap.add_argument("--analyze", metavar="PATH", default=None,
                    help="analyse an existing JSONL instead of collecting")
    args = ap.parse_args(argv)
    if args.analyze:
        print(analyze(args.analyze))
        return 0
    return asyncio.run(run(args))


if __name__ == "__main__":
    raise SystemExit(main())
