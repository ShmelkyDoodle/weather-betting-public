"""Verify each Kalshi series settles on the station we observe.

The entire experiment is only valid if our running ``T_max`` for a station is
the same temperature Kalshi settles the contract against.  Kalshi can, in
principle, change a series' reference station -- so this module checks the
configured ``series -> ICAO`` mapping against each series' **live
``rules_primary`` text** (the actual contract language) and flags any drift.

Two entry points use the same classifier:

* the CLI (``python -m weather_arb.verify_mappings``) for on-demand / scheduled
  checks -- exits non-zero if anything can't be confirmed, so a daily job alerts;
* the running app's market discovery, which calls :func:`classify_rules` daily
  and **auto-disables** any series whose station no longer matches, so live
  trading never drifts out of sync even if nobody runs the CLI.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from dataclasses import asdict, dataclass
from typing import Optional

from .config import Settings
from .market_client import KalshiClient, MarketClientError

# Distinctive location phrases that must appear in a series' rules text for a
# given settlement station. Keep these to the *station-specific* token (e.g.
# "midway", not "chicago") so a station change within a city is still detected.
STATION_ALIASES: dict[str, tuple[str, ...]] = {
    "KNYC": ("central park",),
    "KMDW": ("midway",),
    "KORD": ("o'hare", "ohare", "o’hare"),
    "KMIA": ("miami",),
    "KAUS": ("austin", "bergstrom"),
    "KDEN": ("denver",),
    "KLAX": ("los angeles", "lax"),
    "KPHL": ("philadelphia",),
    "KBOS": ("boston", "logan"),
    "KPHX": ("phoenix", "sky harbor"),
    "KATL": ("atlanta", "hartsfield"),
    "KDCA": ("reagan", "washington national"),
}

# The NWS Climate Report (CLI) product code Kalshi names the station by, e.g.
# "...recorded at Chicago (CLIMDW)...". This is the *authoritative* station
# identifier in the rules text -- Kalshi dropped the plain-English station name
# ("Central Park", "Midway") on 2026-08-14 and kept only this code, which is what
# made the old city-name-only matcher fall through to "review" for NY and CHI.
# Matched before the city aliases because it is unambiguous within a city.
STATION_CLI_CODES: dict[str, tuple[str, ...]] = {
    "KNYC": ("clinyc",),
    "KMDW": ("climdw",),
    "KORD": ("cliord",),
    "KMIA": ("climia",),
    "KAUS": ("cliaus",),
    "KDEN": ("cliden",),
    "KLAX": ("clilax",),
    "KPHL": ("cliphl",),
    "KBOS": ("clibos",),
    "KPHX": ("cliphx",),
    "KATL": ("cliatl",),
    "KDCA": ("clidca",),
}

# Who publishes the number Kalshi settles against. Verified 2026-08-21: all seven
# high-temp series read "according to The Weather Company" (TWC = Weather
# Underground/IBM), NOT the NWS CLI they used to. The authority determines which
# observation series is the settlement basis, so a silent change here is exactly
# as dangerous as a station change -- surface it rather than ignoring it.
SETTLEMENT_AUTHORITIES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("The Weather Company", ("the weather company", "weather company")),
    ("NWS", ("national weather service", "nws climate", "climate report")),
    ("NCEI", ("national centers for environmental information", "ncei")),
)
# What we have verified our data pipeline actually matches (see README / CLAUDE.md:
# TWC daily max == hourly-METAR max, 147/147 station-days).
EXPECTED_AUTHORITY = "The Weather Company"


def detect_authority(rules_text: str) -> Optional[str]:
    """The settlement authority named in the rules text, or ``None`` if unknown."""
    text = rules_text.lower()
    for name, keywords in SETTLEMENT_AUTHORITIES:
        if any(kw in text for kw in keywords):
            return name
    return None

STATUS_OK = "ok"
STATUS_MISMATCH = "mismatch"
STATUS_REVIEW = "review"
STATUS_NO_MARKETS = "no_markets"
STATUS_ERROR = "error"


@dataclass(frozen=True, slots=True)
class VerifyResult:
    series: str
    icao: str
    status: str
    detail: Optional[str] = None
    rules_excerpt: Optional[str] = None
    authority: Optional[str] = None

    @property
    def authority_ok(self) -> bool:
        """The rules still name the settlement authority our data pipeline matches.

        ``None`` (unrecognised wording) is treated as NOT ok: an authority we
        cannot identify is exactly the situation that went unnoticed for a week
        in Aug 2026 when Kalshi moved NWS CLI -> The Weather Company."""
        if self.status == STATUS_NO_MARKETS:
            return True
        return self.authority == EXPECTED_AUTHORITY

    @property
    def is_confirmed_ok(self) -> bool:
        return self.status in (STATUS_OK, STATUS_NO_MARKETS) and self.authority_ok


def classify_rules(rules_text: str, expected_icao: str) -> tuple[str, Optional[str]]:
    """Classify a market's rules text against the expected settlement station.

    Returns ``(status, detail)`` where status is:

    * ``"ok"``       -- the expected station's CLI code or phrase is present;
    * ``"mismatch"`` -- a *different* known station's code/phrase is present
      instead (a real, actionable drift -- e.g. Chicago moved Midway -> O'Hare);
    * ``"review"``   -- no known station identifier found (unknown wording; a
      human should look, but it is not proof of a mismatch).

    The CLI product code ("(CLIMDW)") is checked first and settles the question on
    its own: it is the identifier Kalshi actually keys the market to, and since
    2026-08-14 it is often the *only* station identifier in the text.
    """
    text = rules_text.lower()

    # (1) CLI product code -- authoritative.
    expected_codes = STATION_CLI_CODES.get(expected_icao, ())
    if expected_codes and any(code in text for code in expected_codes):
        return STATUS_OK, None
    for icao, codes in STATION_CLI_CODES.items():
        if icao == expected_icao:
            continue
        hit = next((c for c in codes if c in text), None)
        if hit is not None:
            return STATUS_MISMATCH, (f"rules reference {icao} (CLI code {hit.upper()!r}), "
                                     f"expected {expected_icao}")

    # (2) Plain-English station name -- older wording, still accepted.
    expected = STATION_ALIASES.get(expected_icao, ())
    if expected and any(kw in text for kw in expected):
        return STATUS_OK, None

    for icao, keywords in STATION_ALIASES.items():
        if icao == expected_icao:
            continue
        hit = next((kw for kw in keywords if kw in text), None)
        if hit is not None:
            return STATUS_MISMATCH, f"rules reference {icao} ({hit!r}), expected {expected_icao}"

    if not expected and not expected_codes:
        return STATUS_REVIEW, f"no aliases known for {expected_icao}; cannot auto-verify"
    return STATUS_REVIEW, "no known station phrase or CLI code found in rules text"


async def verify_all(settings: Settings) -> tuple[list[VerifyResult], bool]:
    """Verify every configured ``weather_series`` mapping. Returns (results, all_ok)."""
    client = KalshiClient(settings)
    results: list[VerifyResult] = []
    all_ok = True
    try:
        for series, icao in settings.weather_series:
            result = await _verify_one(client, series, icao)
            results.append(result)
            if not result.is_confirmed_ok:
                all_ok = False
    finally:
        await client.close()
    return results, all_ok


async def _verify_one(client: KalshiClient, series: str, icao: str) -> VerifyResult:
    try:
        markets = await client.get_markets(series_ticker=series, status="open")
    except MarketClientError as exc:
        return VerifyResult(series, icao, STATUS_ERROR, str(exc))
    if not markets:
        return VerifyResult(series, icao, STATUS_NO_MARKETS, "no open markets to sample")
    ticker = markets[0].get("ticker")
    if not ticker:
        return VerifyResult(series, icao, STATUS_NO_MARKETS, "market missing ticker")
    try:
        rules = await client.get_market_rules(str(ticker))
    except MarketClientError as exc:
        return VerifyResult(series, icao, STATUS_ERROR, str(exc))
    status, detail = classify_rules(rules or "", icao)
    return VerifyResult(series, icao, status, detail, (rules or "")[:200],
                        detect_authority(rules or ""))


def _render(results: list[VerifyResult], all_ok: bool) -> str:
    symbol = {
        STATUS_OK: "OK  ", STATUS_MISMATCH: "FAIL", STATUS_REVIEW: "WARN",
        STATUS_NO_MARKETS: "----", STATUS_ERROR: "ERR ",
    }
    lines = ["series -> station verification (vs Kalshi live rules)", "-" * 60]
    for r in results:
        line = f"  [{symbol.get(r.status, '??')}] {r.series:<12} -> {r.icao:<5} {r.status}"
        if r.detail and r.status in (STATUS_MISMATCH, STATUS_REVIEW, STATUS_ERROR):
            line += f"  :: {r.detail}"
        line += f"   [authority: {r.authority or 'UNRECOGNISED'}]"
        lines.append(line)
        if not r.authority_ok:
            lines.append(f"        !! settlement authority is not {EXPECTED_AUTHORITY!r} "
                         f"-- the observation basis may have moved; re-verify before trading")
        if (r.status == STATUS_MISMATCH or not r.authority_ok) and r.rules_excerpt:
            lines.append(f"        rules: {r.rules_excerpt}")
    lines.append("-" * 60)
    lines.append("  RESULT: " + ("ALL MAPPINGS CONFIRMED" if all_ok
                                 else "ATTENTION NEEDED (see FAIL/WARN/ERR above)"))
    return "\n".join(lines)


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Verify Kalshi series->station mappings against live rules text."
    )
    parser.add_argument("--json", action="store_true", help="emit JSON")
    args = parser.parse_args(argv)

    settings = Settings.from_env()
    results, all_ok = asyncio.run(verify_all(settings))

    if args.json:
        print(json.dumps(
            {"all_ok": all_ok,
             "results": [asdict(r) for r in results]},
            indent=2, default=str,
        ))
    else:
        print(_render(results, all_ok))
    # Exit non-zero when any mapping could not be confirmed, so a scheduled job
    # (launchd/cron) surfaces it.
    return 0 if all_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
