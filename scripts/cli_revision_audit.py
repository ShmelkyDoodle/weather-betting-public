#!/usr/bin/env python3
"""Audit Kalshi's settled value against the NWS CLI as it reads TODAY, per city-day.

WHY THIS EXISTS.  On 2026-08-29 Kalshi settled Miami at [90,91] while the CLI, our
observations and Polymarket's payout all said 85.  It was the only such day in 282,
which is exactly the kind of lone anomaly that gets waved away as a data glitch.  It
is not one: the CLI for that day was **issued twice**, and Kalshi settled on the
first, superseded issuance.

    202608300824-KMFL-CDUS42-CLIMIA  04:24 AM EDT   MAXIMUM  90   3:25 PM
    202608300910-KMFL-CDUS42-CLIMIA  05:10 AM EDT   MAXIMUM  85   5:11 AM   <- stands

Kalshi's own market record carries ``expiration_value: 90.00`` and
``settlement_ts: 2026-08-30T12:00:11Z`` -- i.e. it settled on the withdrawn number
**2h50m after** the correction was on the wire.  No reading anywhere near 90 exists
in the observation record for that day (max 86.0F, and 80.6F at the 3:25 PM the
first product named), so the original figure was not a QC'd-out real observation --
it was wrong when published.

WHAT THIS MEASURES.  ``expiration_value`` is the number Kalshi settled on.  Compare
it to the CLI high as the archive serves it now, across every settled city-day.  A
mismatch means the value Kalshi paid on is not the value the official record now
holds -- whatever the cause.  That is the settlement risk, stated without needing to
know which side erred.

WHAT IT DOES NOT MEASURE.  It cannot see a revision that Kalshi *caught*, because
then both numbers agree and the day looks clean.  So the count here is a floor on
how often the CLI is revised, and an exact count of how often a revision reached
settlement.  Distinguishing the two needs the AFOS product archive, which
``--check-products`` samples for the mismatching days only.

Read-only: Kalshi's public API and IEM's archives.  No ledger access, no trading.

Usage
-----
    PYTHONPATH=. .venv/bin/python scripts/cli_revision_audit.py --days 60
    PYTHONPATH=. .venv/bin/python scripts/cli_revision_audit.py --days 60 --json out.json
    PYTHONPATH=. .venv/bin/python scripts/cli_revision_audit.py --days 60 --check-products
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
from collections import defaultdict
from datetime import date as date_cls, datetime, timedelta, timezone
from typing import Any, Optional

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import aiohttp

from weather_arb.backtest import _settled_markets
from weather_arb.settlement_basis import SERIES_ICAO, event_date, fetch_cli_highs

_IEM_AFOS_LIST = "https://mesonet.agron.iastate.edu/api/1/nws/afos/list.json"
_IEM_NWSTEXT = "https://mesonet.agron.iastate.edu/api/1/nwstext/"
_UA = "weather-arb-research (read-only settlement audit)"


def months_spanned(start: date_cls, end: date_cls) -> list[tuple[int, int]]:
    """(year, month) pairs covering [start, end], for the CLI archive's monthly API."""
    out, y, m = [], start.year, start.month
    while (y, m) <= (end.year, end.month):
        out.append((y, m))
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return out


async def collect(days: int) -> dict[str, Any]:
    end = datetime.now(timezone.utc).date() - timedelta(days=1)
    start = end - timedelta(days=days - 1)
    rows: list[dict[str, Any]] = []
    timeout = aiohttp.ClientTimeout(total=180)
    async with aiohttp.ClientSession(timeout=timeout, headers={"User-Agent": _UA}) as session:
        cli_by_icao = {}
        for icao in sorted(set(SERIES_ICAO.values())):
            cli_by_icao[icao] = await fetch_cli_highs(session, icao, months_spanned(start, end))
        min_close = int(datetime.now(timezone.utc).timestamp()) - (days + 3) * 86400
        for series, icao in SERIES_ICAO.items():
            # One expiration_value per EVENT; every market in it carries the same one.
            seen: set[str] = set()
            for raw in await _settled_markets(session, series, min_close):
                ticker = str(raw.get("ticker") or "")
                ev = "-".join(ticker.split("-")[:2])
                if not ev or ev in seen:
                    continue
                day = event_date(ticker)
                if not day or not (start.isoformat() <= day <= end.isoformat()):
                    continue
                seen.add(ev)
                exp = raw.get("expiration_value")
                cli = cli_by_icao.get(icao, {}).get(day)
                rows.append({
                    "event": ev, "series": series, "icao": icao, "date": day,
                    "expiration_value": None if exp is None else float(exp),
                    "cli_high_now": None if cli is None else float(cli),
                    "settlement_ts": raw.get("settlement_ts"),
                })
    rows.sort(key=lambda r: (r["date"], r["icao"]))
    return {"window": [start.isoformat(), end.isoformat()], "rows": rows}


_SUMMARY_RE = re.compile(r"CLIMATE SUMMARY FOR\s+([A-Z]+)\s+(\d{1,2})\s+(\d{4})")
_CLI_MONTHS = {m: i + 1 for i, m in enumerate(
    ["JANUARY", "FEBRUARY", "MARCH", "APRIL", "MAY", "JUNE", "JULY", "AUGUST",
     "SEPTEMBER", "OCTOBER", "NOVEMBER", "DECEMBER"])}


def parse_cli_text(text: str) -> dict[str, Any]:
    """``(summary date, MAXIMUM, is-preliminary)`` from a raw CLI product.

    ⚠ The summary date is **load-bearing** and easy to skip.  Each WFO issues a
    CLI twice: a morning product finalising *yesterday*, and a late-afternoon
    product giving *today* so far ("VALID TODAY AS OF 0400 PM LOCAL TIME").  Both
    land on the same calendar day in the AFOS archive, so listing by issue date
    alone silently mixes two different climate days -- which reads as a spurious
    third "revision" with an unrelated maximum.
    """
    out: dict[str, Any] = {"summary_date": None, "maximum_f": None,
                           "preliminary": "VALID TODAY AS OF" in text.upper()}
    m = _SUMMARY_RE.search(text.upper())
    if m and m.group(1) in _CLI_MONTHS:
        out["summary_date"] = date_cls(int(m.group(3)), _CLI_MONTHS[m.group(1)],
                                       int(m.group(2))).isoformat()
    for line in text.splitlines():
        if line.strip().startswith("MAXIMUM"):
            parts = line.split()
            if len(parts) > 1 and parts[1].lstrip("-").isdigit():
                out["maximum_f"] = int(parts[1])
            break
    return out


async def check_products(session: aiohttp.ClientSession, icao: str, day: str) -> dict[str, Any]:
    """Every CLI issuance that SUMMARISES ``day``, oldest first, with each stated max.

    The final CLI for day D is published on D+1, so both D and D+1 are scanned:
    D catches the same-day preliminary, D+1 the final and any re-issue.  Products
    summarising another day are dropped, not counted as revisions.

    ⚠ A morning re-issue is NOT flagged as a correction -- same WMO header, no
    ``CCA``, only a later timestamp -- which is part of why a downstream consumer
    can settle on the superseded one.
    """
    pil = "CLI" + icao[1:]
    target = date_cls.fromisoformat(day)
    out: dict[str, Any] = {"pil": pil, "date": day, "issuances": [], "dropped_other_day": 0}
    for scan in (target, target + timedelta(days=1)):
        async with session.get(_IEM_AFOS_LIST,
                               params={"pil": pil, "date": scan.isoformat()}) as resp:
            if resp.status >= 400:
                out["error"] = f"afos list HTTP {resp.status} on {scan}"
                continue
            payload = await resp.json(content_type=None)
        entries = payload.get("data", payload if isinstance(payload, list) else [])
        for e in sorted(entries, key=lambda x: str(x.get("entered", ""))):
            pid = e.get("product_id")
            if not pid:
                continue
            async with session.get(_IEM_NWSTEXT + pid) as resp:
                text = await resp.text() if resp.status < 400 else ""
            info = parse_cli_text(text)
            await asyncio.sleep(0.3)
            if info["summary_date"] != day:
                out["dropped_other_day"] += 1
                continue
            out["issuances"].append({"product_id": pid, "entered": e.get("entered"),
                                     "maximum_f": info["maximum_f"],
                                     "preliminary": info["preliminary"]})
    out["issuances"].sort(key=lambda i: str(i["entered"]))
    finals = [i for i in out["issuances"] if not i["preliminary"]]
    out["n_final_issuances"] = len(finals)
    out["revised"] = len({i["maximum_f"] for i in finals}) > 1
    return out


def analyze(data: dict[str, Any]) -> dict[str, Any]:
    rows = data["rows"]
    gradable = [r for r in rows
                if r["expiration_value"] is not None and r["cli_high_now"] is not None]
    mism = [r for r in gradable if r["expiration_value"] != r["cli_high_now"]]
    per_city: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    for r in gradable:
        per_city[r["icao"]][1] += 1
        per_city[r["icao"]][0] += int(r["expiration_value"] != r["cli_high_now"])
    return {
        "window": data["window"],
        "n_events": len(rows),
        "n_gradable": len(gradable),
        "n_missing_expiration_value": sum(1 for r in rows if r["expiration_value"] is None),
        "n_missing_cli": sum(1 for r in rows if r["cli_high_now"] is None),
        "n_mismatch": len(mism),
        "mismatches": mism,
        "per_city": {k: {"mismatch": v[0], "n": v[1]} for k, v in sorted(per_city.items())},
    }


def render(a: dict[str, Any], products: Optional[list[dict[str, Any]]]) -> str:
    w = a["window"]
    out = ["=" * 80,
           "  KALSHI SETTLED VALUE vs the NWS CLI as the archive reads it TODAY",
           f"  window {w[0]} .. {w[1]}", "=" * 80, "",
           f"  settled events        : {a['n_events']}",
           f"  gradable              : {a['n_gradable']}"
           f"   (no expiration_value: {a['n_missing_expiration_value']},"
           f" no CLI: {a['n_missing_cli']})",
           f"  MISMATCHES            : {a['n_mismatch']}", ""]
    out.append(f"  {'station':<10}{'mismatch':>10}{'n':>6}")
    for icao, v in a["per_city"].items():
        out.append(f"  {icao:<10}{v['mismatch']:>10}{v['n']:>6}")
    if a["mismatches"]:
        out += ["", "  Where Kalshi paid on a value the record no longer holds:"]
        for r in a["mismatches"]:
            out.append(f"    {r['icao']} {r['date']}  settled on {r['expiration_value']:.0f}"
                       f"  CLI now {r['cli_high_now']:.0f}"
                       f"  (settled {r['settlement_ts']})")
    if products:
        out += ["", "  CLI ISSUANCES SUMMARISING those days (products covering another day",
                "  are dropped -- each WFO also issues a same-day preliminary. A re-issue",
                "  is NOT flagged as a correction: same WMO header, no CCA, later stamp):"]
        for p in products:
            tag = "REVISED" if p.get("revised") else "single value"
            out.append(f"    {p['pil']} for {p['date']}  -- {p.get('n_final_issuances', 0)}"
                       f" final issuance(s), {tag}"
                       f"  ({p.get('dropped_other_day', 0)} other-day products dropped)"
                       + (f"  [{p['error']}]" if p.get("error") else ""))
            for i in p.get("issuances", []):
                kind = "prelim" if i["preliminary"] else "FINAL "
                out.append(f"      {i['entered']}  {kind}  {i['product_id']}"
                           f"  MAXIMUM {i['maximum_f']}")
    out += ["", "  A mismatch means the number Kalshi PAID ON is not the number the official",
            "  record now holds. It does not by itself say which side erred -- read the",
            "  issuance list above for that. This count is a FLOOR on how often the CLI is",
            "  revised (a revision Kalshi caught leaves both numbers agreeing and is",
            "  invisible here) and an EXACT count of how often one reached settlement.",
            "=" * 80]
    return "\n".join(out)


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--days", type=int, default=60, help="look back N days (default 60)")
    ap.add_argument("--check-products", action="store_true",
                    help="also pull every CLI issuance for the mismatching days")
    ap.add_argument("--json", default=None, metavar="PATH", help="write the full result here")
    args = ap.parse_args(argv)

    data = asyncio.run(collect(max(7, args.days)))
    a = analyze(data)
    products = None
    if args.check_products and a["mismatches"]:
        async def _go():
            timeout = aiohttp.ClientTimeout(total=180)
            async with aiohttp.ClientSession(timeout=timeout,
                                             headers={"User-Agent": _UA}) as s:
                return [await check_products(s, r["icao"], r["date"]) for r in a["mismatches"]]
        products = asyncio.run(_go())
        a["cli_issuances"] = products
    print(render(a, products))
    if args.json:
        with open(args.json, "w", encoding="utf-8") as fh:
            json.dump({"analysis": a, "rows": data["rows"]}, fh, indent=2)
        print(f"\nwrote {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
