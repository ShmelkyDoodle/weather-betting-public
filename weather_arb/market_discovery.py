"""Automatic discovery of tradeable Kalshi weather markets.

Kalshi's daily high-temperature series (``KXHIGHNY``, ``KXHIGHCHI``, ...) expose
a fresh set of range-bucket markets each day.  Rather than hand-maintaining
``WA_MARKETS_JSON``, :class:`MarketDiscovery` periodically queries the exchange,
parses each bucket's ``floor_strike`` / ``cap_strike`` / ``strike_type``, maps
the series to the ICAO station Kalshi settles it against, and publishes the
result into a :class:`MarketRegistry` the arbitrage engine reads live.

Only series whose settlement station is one we actually observe are admitted --
otherwise our running ``T_max`` would not correspond to the contract's
settlement source and both strategies would be unsound.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Optional

from .config import Settings
from .market_client import MarketClient, MarketClientError
from .models import (
    STRIKE_BETWEEN,
    STRIKE_GREATER,
    STRIKE_GREATER_EQ,
    STRIKE_LESS,
    STRIKE_LESS_EQ,
    Venue,
    WeatherMarket,
)
from .verify_mappings import (EXPECTED_AUTHORITY, STATUS_MISMATCH, STATUS_REVIEW,
                              classify_rules, detect_authority)
from . import timeutils

log = logging.getLogger(__name__)

# Kalshi statuses in which a market can still be opened/traded.
_OPEN_STATUSES = frozenset({"active", "open", "initialized"})


class MarketRegistry:
    """Thread/async-safe holder for the current tradeable market set.

    The engine reads a consistent immutable snapshot via :meth:`all` /
    :meth:`for_station`; discovery swaps the whole set atomically via
    :meth:`replace`.
    """

    def __init__(self, initial: tuple[WeatherMarket, ...] = ()) -> None:
        self._markets: tuple[WeatherMarket, ...] = tuple(initial)

    def replace(self, markets: tuple[WeatherMarket, ...]) -> None:
        self._markets = tuple(markets)

    def all(self) -> tuple[WeatherMarket, ...]:
        return self._markets

    def for_station(self, icao: str) -> tuple[WeatherMarket, ...]:
        return tuple(m for m in self._markets if m.icao == icao)

    def tickers(self) -> list[str]:
        return [m.ticker for m in self._markets]


class MarketDiscovery:
    """Populates a :class:`MarketRegistry` from live Kalshi weather series."""

    def __init__(
        self,
        *,
        client: MarketClient,
        settings: Settings,
        registry: MarketRegistry,
    ) -> None:
        self._client = client
        self._settings = settings
        self._registry = registry
        # Daily settlement-mapping self-verification state.
        self._verified_on: dict[str, str] = {}   # series -> UTC date last checked
        self._blocked: set[str] = set()           # series with a station mismatch
        # Only trade series whose settlement station we actually observe.
        observed = {s.icao for s in settings.stations}
        self._series_map: dict[str, str] = {
            series: icao
            for series, icao in settings.weather_series
            if icao in observed
        }
        skipped = [
            (series, icao)
            for series, icao in settings.weather_series
            if icao not in observed
        ]
        if skipped:
            log.warning(
                "skipping weather series with unobserved settlement station",
                extra={"context": {"skipped": skipped}},
            )

    async def run(self, stop_event: asyncio.Event) -> None:
        """Refresh loop until shutdown."""
        if not self._series_map:
            log.warning("market discovery idle: no admissible weather series")
            await stop_event.wait()
            return
        log.info(
            "market discovery started",
            extra={"context": {"series": self._series_map,
                               "interval_s": self._settings.discovery_refresh_seconds}},
        )
        while not stop_event.is_set():
            try:
                await self.refresh()
            except asyncio.CancelledError:
                raise
            except MarketClientError as exc:
                log.warning(
                    "market discovery transient error",
                    extra={"context": {"error": str(exc)}},
                )
            except Exception:  # noqa: BLE001 - discovery must never kill the loop
                log.exception("market discovery failed (unexpected)")
            try:
                await asyncio.wait_for(
                    stop_event.wait(),
                    timeout=self._settings.discovery_refresh_seconds,
                )
            except asyncio.TimeoutError:
                pass

    async def refresh(self) -> tuple[WeatherMarket, ...]:
        """Scan all admissible series once and publish the result.

        Each series is verified against Kalshi's live settlement rules once per
        UTC day; a series whose reference station no longer matches our observed
        station is **excluded** (never traded) until it verifies clean again.
        """
        today = timeutils.utc_now().date().isoformat()
        discovered: list[WeatherMarket] = []
        for series, icao in self._series_map.items():
            raw_markets = await self._client.get_markets(
                series_ticker=series, status="open"
            )
            if not raw_markets:
                continue
            if self._verified_on.get(series) != today:
                await self._verify_series(
                    series, icao, str(raw_markets[0].get("ticker") or ""), today
                )
            if series in self._blocked:
                continue  # settlement station drifted -> do not trade this series
            for raw in raw_markets:
                market = _parse_market(raw, icao)
                if market is not None:
                    discovered.append(market)
        self._registry.replace(tuple(discovered))
        log.info(
            "market discovery refreshed",
            extra={"context": {"count": len(discovered),
                               "blocked_series": sorted(self._blocked),
                               "tickers": [m.ticker for m in discovered][:25]}},
        )
        return tuple(discovered)

    async def _verify_series(
        self, series: str, icao: str, ticker: str, today: str
    ) -> None:
        """Check one series' live rules text against its configured station."""
        if not ticker:
            return
        try:
            rules = await self._client.get_market_rules(ticker)
        except MarketClientError:
            return  # transient; leave block state unchanged, retry next refresh
        self._verified_on[series] = today
        status, detail = classify_rules(rules or "", icao)
        authority = detect_authority(rules or "")
        if authority != EXPECTED_AUTHORITY:
            # The authority names the observation series the market settles on.
            # Our T_max is the hourly-METAR max, verified 2026-08-21 to equal The
            # Weather Company's published daily max on 147/147 station-days. Any
            # other authority (or wording we cannot parse) means that equality is
            # unverified, so stop trading the series rather than assuming.
            self._blocked.add(series)
            log.error(
                "SETTLEMENT AUTHORITY CHANGED -- disabling series until re-verified",
                extra={"context": {"series": series, "icao": icao,
                                   "expected": EXPECTED_AUTHORITY,
                                   "found": authority or "unrecognised",
                                   "rules": (rules or "")[:200]}},
            )
            return
        if status == STATUS_MISMATCH:
            self._blocked.add(series)
            log.error(
                "SETTLEMENT MAPPING MISMATCH -- disabling series until corrected",
                extra={"context": {"series": series, "expected_icao": icao,
                                   "detail": detail, "rules": (rules or "")[:200]}},
            )
            return
        was_blocked = series in self._blocked
        self._blocked.discard(series)
        if status == STATUS_REVIEW:
            log.warning(
                "could not confirm settlement station from rules (continuing to trade)",
                extra={"context": {"series": series, "icao": icao,
                                   "detail": detail, "rules": (rules or "")[:200]}},
            )
        elif was_blocked:
            log.info(
                "settlement mapping re-confirmed; re-enabling series",
                extra={"context": {"series": series, "icao": icao}},
            )


def _parse_market(raw: dict[str, Any], icao: str) -> Optional[WeatherMarket]:
    """Convert a raw Kalshi market dict into a :class:`WeatherMarket`."""
    ticker = raw.get("ticker")
    if not ticker:
        return None
    status = str(raw.get("status", "")).lower()
    if status and status not in _OPEN_STATUSES:
        return None

    floor_strike = _to_float(raw.get("floor_strike"))
    cap_strike = _to_float(raw.get("cap_strike"))
    strike_type = _normalize_strike_type(
        str(raw.get("strike_type", "")), floor_strike, cap_strike
    )
    if floor_strike is None and cap_strike is None:
        # Without any numeric strike we cannot reason about settlement.
        return None

    # A representative single strike for logging / back-compat helpers.
    representative = floor_strike if floor_strike is not None else cap_strike
    close_raw = raw.get("close_time")
    close_time = (
        timeutils.parse_isoformat_utc(str(close_raw)) if close_raw else None
    )
    title = str(raw.get("title") or raw.get("subtitle") or raw.get("yes_sub_title") or "")
    event_date = _parse_event_date(str(ticker))

    return WeatherMarket(
        ticker=str(ticker),
        venue=Venue.KALSHI,
        icao=icao,
        strike_temp_f=float(representative) if representative is not None else 0.0,
        floor_strike_f=floor_strike,
        cap_strike_f=cap_strike,
        strike_type=strike_type,
        title=title,
        close_time_utc=close_time,
        status=status or "open",
        event_date=event_date,
    )


_MONTHS = {
    "JAN": 1, "FEB": 2, "MAR": 3, "APR": 4, "MAY": 5, "JUN": 6,
    "JUL": 7, "AUG": 8, "SEP": 9, "OCT": 10, "NOV": 11, "DEC": 12,
}


def _parse_event_date(ticker: str) -> Optional[str]:
    """Extract the settlement date (``YYYY-MM-DD``) from a Kalshi ticker.

    Kalshi weather tickers embed the event date as ``YYMMMDD`` in the second
    dash-delimited segment, e.g. ``KXHIGHNY-26JUL21-B90.5`` -> ``2026-07-21``.
    Returns ``None`` if the segment is absent or unparseable (the engine then
    falls back to the current climate day).
    """
    parts = ticker.split("-")
    if len(parts) < 2:
        return None
    token = parts[1].upper()
    # Expect e.g. 26JUL21 -> YY(2) MMM(3) DD(2).
    if len(token) != 7:
        return None
    yy, mon, dd = token[:2], token[2:5], token[5:]
    if mon not in _MONTHS or not (yy.isdigit() and dd.isdigit()):
        return None
    try:
        return f"20{yy}-{_MONTHS[mon]:02d}-{int(dd):02d}"
    except ValueError:
        return None


def _normalize_strike_type(
    raw: str, floor: Optional[float], cap: Optional[float]
) -> str:
    value = raw.strip().lower()
    known = {STRIKE_GREATER, STRIKE_GREATER_EQ, STRIKE_LESS, STRIKE_LESS_EQ, STRIKE_BETWEEN}
    if value in known:
        return value
    # Infer from which bounds are present.
    if floor is not None and cap is not None:
        return STRIKE_BETWEEN
    if floor is not None:
        return STRIKE_GREATER_EQ
    if cap is not None:
        return STRIKE_LESS_EQ
    return STRIKE_GREATER_EQ


def _to_float(value: object) -> Optional[float]:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
