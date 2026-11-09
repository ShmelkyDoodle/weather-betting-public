"""1-minute ASOS augmenter for the Strategy-1 lock-in engine.

Kalshi settles on the next-day NWS Climate Report, which is derived from the full
**1-minute** ASOS record and so includes brief intra-hour spikes the routine
METAR poller (hourly + SPECI) misses. On the majority of days the NWS max exceeds
the hourly-METAR max (see :mod:`divergence`), so a Kalshi bucket can become
*certain* off a 1-minute spike that no METAR ever printed -- and the METAR-only
lock-in engine would miss the lock.

This augmenter pulls the 1-minute series from the IEM archive, computes a
**corroborated** running daily max, and feeds any new high into the *same*
``update_tmax`` + ``on_new_high`` pipeline the METAR poller uses. It only ever
*raises* ``T_max`` toward the true NWS value -- ``update_tmax`` takes the max
server-side, so it can never lower it, and it never touches the WU/Polymarket
numbers computed elsewhere.

Safety (this is the live trade path):

* **opt-in** -- ``enable_1min_lockin`` is off by default;
* **corroboration** -- a candidate max is accepted only if >= ``onemin_min_confirm``
  minutes sit within ``confirmation_tolerance_f`` of it (no lone-minute glitch);
* **sanity cap** -- a candidate that exceeds the current ``T_max`` by more than
  ``spike_threshold_f`` is rejected and logged for review, never locked;
* the IEM 1-minute feed is itself quality-controlled.

The IEM 1-minute archive lags real time by up to ~an hour, so this improves lock
*accuracy/completeness*, not latency -- the METAR poller stays the fast path.
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import date as date_cls, datetime, timedelta, timezone
from typing import Awaitable, Callable, Optional

import aiohttp

from . import timeutils
from .config import Settings
from .db import Database

log = logging.getLogger(__name__)

_IEM_1MIN = "https://mesonet.agron.iastate.edu/cgi-bin/request/asos1min.py"
_MIN_REQUEST_SPACING_S = 1.2

NewHighCallback = Callable[[str, str, float], Awaitable[None]]


def corroborated_day_max(
    obs: list[tuple[datetime, float]], climate_date: str, offset: int,
    min_confirm: int, tolerance_f: float,
) -> Optional[tuple[float, datetime]]:
    """Return the ``(max_f, peak_time)`` for the highest *corroborated* level in
    the climate day, or ``None`` if nothing is corroborated.

    A level ``c`` is corroborated when at least ``min_confirm`` observations sit
    within ``tolerance_f`` of it (``>= c - tolerance_f``). Scanning observed values
    high-to-low and taking the first corroborated one means a lone-minute spike
    *above* the real peak is skipped (glitch) yet the sustained peak just below it
    is still found -- rather than the whole day being discarded."""
    day = [(t, v) for t, v in obs if timeutils.climate_date_str(t, offset) == climate_date]
    if not day:
        return None
    for c in sorted({v for _, v in day}, reverse=True):
        if sum(1 for _, v in day if v >= c - tolerance_f) >= min_confirm:
            return (c, max(t for t, v in day if v == c))
    return None


class OneMinutePoller:
    """Polls the IEM 1-minute ASOS archive and augments the lock-in ``T_max``."""

    def __init__(self, *, session: aiohttp.ClientSession, db: Database,
                 settings: Settings, on_new_high: Optional[NewHighCallback] = None) -> None:
        self._session = session
        self._db = db
        self._settings = settings
        self._on_new_high = on_new_high
        # Only the stations Kalshi actually settles against (from weather_series).
        self._icaos = sorted({icao for _, icao in settings.weather_series})
        self._req_lock = asyncio.Lock()
        self._last_req = 0.0

    async def run(self, stop_event: asyncio.Event) -> None:
        log.info("1min lock-in augmenter started",
                 extra={"context": {"stations": self._icaos,
                                    "interval_s": self._settings.onemin_poll_seconds}})
        while not stop_event.is_set():
            try:
                await self._poll_once()
            except Exception:  # noqa: BLE001 - a poll error must not kill the loop
                log.exception("1min augmenter poll failed")
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=self._settings.onemin_poll_seconds)
            except asyncio.TimeoutError:
                pass

    async def _poll_once(self) -> None:
        for icao in self._icaos:
            station = self._settings.station_by_icao(icao)
            if station is None:
                continue
            offset = station.standard_utc_offset_hours
            climate_date = timeutils.climate_date_str(timeutils.utc_now(), offset)
            obs = await self._fetch_1min(icao, climate_date, offset)
            if obs:
                await self._apply(icao, climate_date, offset, obs)

    async def _apply(self, icao: str, climate_date: str, offset: int,
                     obs: list[tuple[datetime, float]]) -> Optional[float]:
        """Corroborate, sanity-check, and (if a new high) raise T_max + notify.

        Returns the resulting stored ``T_max`` when this reading set a new high,
        else ``None``. Pure of network -- unit-testable with a stub DB.
        """
        cm = corroborated_day_max(
            obs, climate_date, offset,
            self._settings.onemin_min_confirm, self._settings.confirmation_tolerance_f,
        )
        if cm is None:
            return None
        candidate, peak_ts = cm
        prev = await self._db.get_tmax(icao, climate_date)
        if prev is not None:
            if candidate <= prev:
                return None  # nothing new to lock
            if candidate > prev + self._settings.spike_threshold_f:
                log.warning(
                    "1min candidate rejected: exceeds T_max by > spike_threshold",
                    extra={"context": {"icao": icao, "date": climate_date,
                                       "candidate_f": candidate, "tmax_f": prev,
                                       "threshold_f": self._settings.spike_threshold_f}},
                )
                return None
        current = await self._db.update_tmax(
            icao=icao, climate_date=climate_date, temp_f=candidate, observed_at_utc=peak_ts,
        )
        if prev is None or current > prev:
            log.info("1min new high -> lock-in re-eval",
                     extra={"context": {"icao": icao, "date": climate_date,
                                        "tmax_f": current, "prev_f": prev}})
            if self._on_new_high is not None:
                await self._on_new_high(icao, climate_date, current)
        return current

    async def _fetch_1min(self, icao: str, climate_date: str, offset: int
                          ) -> list[tuple[datetime, float]]:
        # The LST climate day spans a UTC window inside [date, date+1]; fetch that.
        d1 = date_cls.fromisoformat(climate_date)
        d2 = d1 + timedelta(days=1)
        iem_id = icao[1:] if icao.startswith("K") else icao
        params = {
            "station": iem_id, "tz": "UTC", "vars": "tmpf", "sample": "1min",
            "what": "download", "delim": "comma", "gis": "no",
            "year1": d1.year, "month1": d1.month, "day1": d1.day,
            "year2": d2.year, "month2": d2.month, "day2": d2.day,
        }
        return _parse_1min(await self._get(params))

    async def _get(self, params: dict) -> str:
        for attempt in range(4):
            async with self._req_lock:
                wait = _MIN_REQUEST_SPACING_S - (time.monotonic() - self._last_req)
                if wait > 0:
                    await asyncio.sleep(wait)
                self._last_req = time.monotonic()
            try:
                async with self._session.get(_IEM_1MIN, params=params) as resp:
                    if resp.status == 429 or resp.status >= 500:
                        await asyncio.sleep(min(8.0, 1.5 * (2 ** attempt)))
                        continue
                    if resp.status >= 400:
                        return ""
                    return await resp.text()
            except (aiohttp.ClientError, asyncio.TimeoutError):
                await asyncio.sleep(1.5 * (2 ** attempt))
        return ""


def _parse_1min(text: str) -> list[tuple[datetime, float]]:
    """Parse IEM asos1min CSV (station,station_name,valid(UTC),tmpf)."""
    out: list[tuple[datetime, float]] = []
    for line in text.splitlines()[1:]:
        parts = line.split(",")
        if len(parts) < 4:
            continue
        raw_ts, raw_val = parts[2].strip(), parts[3].strip()
        if not raw_val or raw_val.upper() == "M":
            continue
        try:
            ts = datetime.strptime(raw_ts, "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc)
            out.append((ts, float(raw_val)))
        except ValueError:
            continue
    return out
