"""MOS (Model Output Statistics) ingestion from the Iowa Environmental Mesonet.

MOS is bias-corrected, **station-specific** guidance — issued for the exact ASOS
sites Kalshi settles on — which makes it a natural second (and third) opinion on
the daily high alongside the NWS gridpoint forecast.  We pull two model families
from IEM's MOS API: ``NBS`` (the National Blend of Models, what NWS leans on) and
``GFS`` (GFS MOS / MAV).  Temperatures are already in °F.

The daily-max estimate is the maximum of the 3-hourly ``tmp`` values that fall
within the target Local-Standard-Time climate day — robust and unambiguous
(the ``n_x`` max/min field is only sparsely populated).
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional

import aiohttp

from . import timeutils
from .config import Settings

log = logging.getLogger(__name__)

_MOS_URL = "https://mesonet.agron.iastate.edu/api/1/mos.json"

# Friendly model name -> IEM MOS model code.
MODEL_CODES: dict[str, str] = {"nbm": "NBS", "gfs_mos": "GFS"}


class MosClient:
    """Fetches and caches MOS temperature series per (station, model)."""

    def __init__(self, *, session: aiohttp.ClientSession, settings: Settings) -> None:
        self._session = session
        self._settings = settings
        # (icao, code) -> (fetched_at, [(ftime_utc, temp_f)])
        self._cache: dict[tuple[str, str], tuple[datetime, list[tuple[datetime, float]]]] = {}

    async def daily_max_f(
        self, icao: str, model_code: str, climate_date: str, standard_offset_hours: int
    ) -> Optional[float]:
        """Forecast daily max (°F) for a station/model/climate-day, or ``None``."""
        series = await self._series(icao, model_code)
        if not series:
            return None
        start, end = timeutils.climate_day_bounds_utc(climate_date, standard_offset_hours)
        temps = [t for (ts, t) in series if start <= ts < end]
        return max(temps) if temps else None

    async def _series(self, icao: str, model_code: str) -> list[tuple[datetime, float]]:
        key = (icao, model_code)
        cached = self._cache.get(key)
        ttl = self._settings.forecast.refresh_seconds
        if cached is not None and (timeutils.utc_now() - cached[0]).total_seconds() < ttl:
            return cached[1]
        try:
            async with self._session.get(
                _MOS_URL, params={"station": icao, "model": model_code}
            ) as resp:
                resp.raise_for_status()
                payload = await resp.json(content_type=None)
        except (aiohttp.ClientError, TimeoutError):
            log.warning("MOS fetch failed",
                        extra={"context": {"icao": icao, "model": model_code}})
            return cached[1] if cached else []

        rows = payload.get("data") if isinstance(payload, dict) else None
        series: list[tuple[datetime, float]] = []
        if rows:
            latest = max(str(r.get("runtime", "")) for r in rows)
            for r in rows:
                if str(r.get("runtime", "")) != latest:
                    continue
                tmp = r.get("tmp")
                ftime = r.get("ftime_utc") or r.get("ftime")
                if tmp is None or not ftime:
                    continue
                ts = _parse_mos_time(str(ftime))
                if ts is None:
                    continue
                try:
                    series.append((ts, float(tmp)))
                except (TypeError, ValueError):
                    continue
        self._cache[key] = (timeutils.utc_now(), series)
        return series


def _parse_mos_time(value: str) -> Optional[datetime]:
    s = value.strip().replace("T", " ")
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(s, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    try:
        return timeutils.parse_isoformat_utc(value)
    except ValueError:
        return None
