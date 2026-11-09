"""Climatological base-rate model.

The crowd anchors to a headline forecast bucket and ignores how often that
temperature range *historically* occurs on this calendar date — the exact edge
described in weather-market write-ups. This module builds that base rate
*rigorously*: for a station and target climate day it pulls the daily maxima
from the IEM ASOS archive over a **window of days across many years** (not a
single noisy date), fits a climatological normal, and exposes it as one more
forecast "model" (``climo``) plus a forecast/climatology ``blend``. Both are
logged as shadow predictions and graded by Brier alongside everything else —
so the data, not a sales pitch, decides whether the base rate adds edge.

Rigor notes: a windowed multi-year sample (default ±7 days x 12 years) avoids
the small-sample overfitting of a single-date frequency, and we fit a normal
rather than using a raw bucket count so narrow buckets are smooth.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from statistics import fmean, pstdev
from typing import Optional

import aiohttp

from . import timeutils
from .config import Settings

log = logging.getLogger(__name__)

_IEM_URL = "https://mesonet.agron.iastate.edu/cgi-bin/request/asos.py"
# ICAO -> IEM state ASOS network (needed for reliable archive queries).
_IEM_NETWORKS = {
    "KMDW": "IL_ASOS", "KORD": "IL_ASOS", "KNYC": "NY_ASOS", "KMIA": "FL_ASOS",
    "KAUS": "TX_ASOS", "KDEN": "CO_ASOS", "KLAX": "CA_ASOS", "KPHL": "PA_ASOS",
    "KBOS": "MA_ASOS",
}
_MIN_REQUEST_SPACING_S = 1.2  # be gentle with the IEM archive


class ClimatologyClient:
    """Windowed multi-year climatology of daily maxima per station (cached)."""

    def __init__(self, *, session: aiohttp.ClientSession, settings: Settings) -> None:
        self._session = session
        self._settings = settings
        # (icao, "MMDD") -> (mean_f, std_f, n) or None
        self._cache: dict[tuple[str, str], Optional[tuple[float, float, int]]] = {}
        self._key_locks: dict[tuple[str, str], asyncio.Lock] = defaultdict(asyncio.Lock)
        self._req_lock = asyncio.Lock()
        self._last_req = 0.0

    async def distribution(
        self, icao: str, climate_date: str, standard_offset_hours: int
    ) -> Optional[tuple[float, float, int]]:
        """Return (climatological mean °F, std °F, sample count) for the day, or None."""
        network = _IEM_NETWORKS.get(icao)
        if network is None:
            return None
        _, mm, dd = climate_date.split("-")
        key = (icao, mm + dd)
        async with self._key_locks[key]:
            if key in self._cache:
                return self._cache[key]
            maxes = await self._collect(icao, network, int(mm), int(dd), standard_offset_hours)
            cfg = self._settings.forecast
            result: Optional[tuple[float, float, int]] = None
            if len(maxes) >= cfg.climo_min_samples:
                mean = fmean(maxes)
                std = pstdev(maxes) if len(maxes) > 1 else cfg.base_sigma_f
                result = (mean, max(std, 1.0), len(maxes))
                log.info(
                    "climatology built",
                    extra={"context": {"icao": icao, "date": climate_date,
                                       "mean_f": round(mean, 1), "std_f": round(result[1], 1),
                                       "samples": len(maxes)}},
                )
            self._cache[key] = result
            return result

    async def _collect(
        self, icao: str, network: str, month: int, day: int, offset: int
    ) -> list[float]:
        iem_id = icao[1:] if icao.startswith("K") else icao
        cfg = self._settings.forecast
        this_year = timeutils.utc_now().year
        window = cfg.climo_window_days
        maxes: list[float] = []
        for year in range(this_year - 1, this_year - 1 - cfg.climo_years, -1):
            try:
                center = date(year, month, day)
            except ValueError:  # e.g. Feb 29 in a non-leap year
                continue
            start, end = center - timedelta(days=window), center + timedelta(days=window)
            maxes.extend(await self._daily_maxes(iem_id, network, start, end, offset))
        return maxes

    async def _daily_maxes(
        self, iem_id: str, network: str, start: date, end: date, offset: int
    ) -> list[float]:
        params = {
            "station": iem_id, "network": network, "data": "tmpf", "tz": "Etc/UTC",
            "format": "onlycomma", "latlon": "no", "missing": "empty",
            "year1": start.year, "month1": start.month, "day1": start.day,
            "year2": end.year, "month2": end.month, "day2": end.day,
        }
        text = await self._get(params)
        by_day: dict[date, float] = {}
        for row in text.splitlines()[1:]:
            parts = row.split(",")
            if len(parts) < 3 or not parts[2].strip():
                continue
            try:
                ts = datetime.strptime(parts[1].strip(), "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc)
                temp = float(parts[2])
            except ValueError:
                continue
            climate_day = (ts + timedelta(hours=offset)).date()
            by_day[climate_day] = max(by_day.get(climate_day, -1e9), temp)
        return list(by_day.values())

    async def _get(self, params: dict[str, object]) -> str:
        """Rate-limited GET with backoff on 429/5xx."""
        for attempt in range(4):
            async with self._req_lock:
                wait = _MIN_REQUEST_SPACING_S - (time.monotonic() - self._last_req)
                if wait > 0:
                    await asyncio.sleep(wait)
                self._last_req = time.monotonic()
            try:
                async with self._session.get(_IEM_URL, params=params) as resp:
                    if resp.status == 429 or resp.status >= 500:
                        await asyncio.sleep(min(8.0, 1.5 * (2 ** attempt)))
                        continue
                    if resp.status >= 400:
                        return ""
                    return await resp.text()
            except (aiohttp.ClientError, TimeoutError):
                await asyncio.sleep(1.5 * (2 ** attempt))
        return ""
