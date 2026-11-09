"""Secondary observation feed: the api.weather.gov 5-minute ASOS product.

WHY. The hourly METAR reports a crossing a measured median ~53 minutes after the
temperature actually crossed (n=412, 5 stations, 5 days). That window is the only
genuine inefficiency this project has found. The IEM 1-minute archive cannot
reach it -- same-day requests return zero rows and the archive is only ~38-50%
complete -- but `api.weather.gov/stations/{id}/observations` serves a 5-minute
product, free, with no API key, at KMDW/KMIA/KAUS/KLAX/KPHL.

WHAT IT IS NOT ALLOWED TO DO. It cannot cause a trade. Two independent reasons:

1. **Quantization.** The 5-minute rows carry whole degC -- 1.8 degF steps --
   against 1 degF bucket boundaries. That is COARSER than the +-0.08 degF METAR
   error that produced 18 wrong lock-in calls, so it is structurally incapable of
   proving certainty at bucket granularity. It can only ever say "look at this".
2. **It is unproven.** Publication lag was measured once, at one instant
   (13-23 min). Whether it manufactures false crossings is exactly what this
   module exists to find out, and that question cannot be answered by a source
   that is already being trusted.

So it writes `source_observations` rows and calls
:meth:`ArbitrageEngine.observe_shadow_lockin`, which records detections and arms
the book recorder and reaches no order path at all.

KNYC and KDEN serve hourly data only. That is recorded rather than worked around:
`availability` reports it, and the daily report names the stations where the
whole premise is unavailable.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import Any, Awaitable, Callable, Optional

import aiohttp

from . import timeutils
from .config import Settings, StationConfig
from .db import Database

log = logging.getLogger(__name__)

# The observation carries degC. A whole-degC value is the 5-minute product
# (1 degC = 1.8 degF resolution); a tenths value comes from a METAR T-group
# (0.1 degC = 0.18 degF). Recorded per observation so the analysis can weight
# a crossing by how precisely it was actually known.
PRECISION_WHOLE_C_F = 1.8
PRECISION_TENTH_C_F = 0.18

SOURCE_5MIN = "nws5min"
SOURCE_HOURLY = "nws_hourly"

ShadowCallback = Callable[..., Awaitable[int]]


def _c_to_f(celsius: float) -> float:
    return celsius * 9.0 / 5.0 + 32.0


# A station is treated as serving the 5-minute product when its observations
# arrive at least this often. 15 min is a wide margin either side of the two
# real regimes (5 min and 60 min), so it does not need tuning.
SUB_HOURLY_MAX_GAP_S = 900.0


def median_gap_seconds(timestamps: list[datetime]) -> Optional[float]:
    """Median spacing between consecutive observations, or None if <2."""
    if len(timestamps) < 2:
        return None
    ordered = sorted(timestamps)
    gaps = [(b - a).total_seconds()
            for a, b in zip(ordered, ordered[1:]) if b > a]
    if not gaps:
        return None
    gaps.sort()
    mid = len(gaps) // 2
    return gaps[mid] if len(gaps) % 2 else (gaps[mid - 1] + gaps[mid]) / 2.0


def classify_precision(
    celsius: float, has_raw_metar: bool, sub_hourly_station: bool = True
) -> tuple[str, float]:
    """(source, precision_f) for one observation.

    ⚠ `rawMessage` alone is NOT a reliable discriminator, and assuming it was
    produced a wrong answer on the first live run: KNYC and KDEN returned rows
    with no `rawMessage` that were plainly hourly (3.5-24 h old, 60 min apart),
    and every station was reported as serving the 5-minute product. The
    trustworthy signal is CADENCE -- so a station whose observations arrive
    hourly is labelled hourly no matter what the payload omits.

    Precision is read from the VALUE, not assumed from the source, because the
    value is what actually limits crossing detection: whole degC is 1.8 degF
    steps against 1 degF buckets.
    """
    whole = abs(celsius - round(celsius)) < 1e-9
    is_five = (not has_raw_metar) and sub_hourly_station
    source = SOURCE_5MIN if is_five else SOURCE_HOURLY
    return source, (PRECISION_WHOLE_C_F if whole else PRECISION_TENTH_C_F)


class NwsObservationPoller:
    """Polls the 5-minute product, journals it, and raises SHADOW detections."""

    def __init__(
        self, *, session: aiohttp.ClientSession, db: Database, settings: Settings,
        on_shadow_lockin: Optional[ShadowCallback] = None,
    ) -> None:
        self._session = session
        self._db = db
        self._settings = settings
        self._cfg = settings.experiment
        self._on_shadow = on_shadow_lockin
        # (icao, climate_date) -> running max on THIS source alone. Deliberately
        # separate from daily_tmax: mixing a 1.8 degF-quantized feed into the
        # value the lock-in rule consumes is exactly what must not happen.
        self._running: dict[tuple[str, str], float] = {}
        # icao -> whether a sub-hourly row has ever been seen (KNYC/KDEN: never)
        self._sub_hourly_seen: dict[str, bool] = {}

    # ------------------------------------------------------------------ #
    # Loop
    # ------------------------------------------------------------------ #
    async def run(self, stop_event: asyncio.Event) -> None:
        if not (self._cfg.enabled and self._cfg.enable_nws_5min):
            log.info("nws 5-minute poller disabled")
            await stop_event.wait()
            return
        log.info(
            "nws 5-minute poller started",
            extra={"context": {
                "stations": [s.icao for s in self._settings.stations],
                "interval_s": self._cfg.nws_5min_poll_seconds,
                "shadow_only": True,
            }},
        )
        while not stop_event.is_set():
            try:
                await self.poll_once()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - a secondary feed must never kill the process
                log.exception("nws 5-minute poll failed")
            try:
                await asyncio.wait_for(
                    stop_event.wait(), timeout=self._cfg.nws_5min_poll_seconds)
            except asyncio.TimeoutError:
                pass

    async def poll_once(self) -> int:
        """One pass over every station. Returns observations journalled."""
        total = 0
        for station in self._settings.stations:
            try:
                total += await self._poll_station(station)
            except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                log.warning(
                    "nws 5-minute fetch failed",
                    extra={"context": {"icao": station.icao, "error": str(exc)}},
                )
        return total

    async def _poll_station(self, station: StationConfig) -> int:
        url = self._cfg.nws_5min_url.format(station=station.icao)
        params = {"limit": self._cfg.nws_5min_limit}
        async with self._session.get(
            url, params=params, headers={"Accept": "application/geo+json"}
        ) as resp:
            if resp.status >= 400:
                log.warning(
                    "nws 5-minute HTTP error",
                    extra={"context": {"icao": station.icao, "status": resp.status}},
                )
                return 0
            payload = await resp.json(content_type=None)
        received_at = timeutils.utc_now()

        observations = _parse_features(payload)
        if not observations:
            return 0
        gap_s = median_gap_seconds([ts for ts, _c, _raw in observations])
        sub_hourly = gap_s is not None and gap_s <= SUB_HOURLY_MAX_GAP_S
        if station.icao not in self._sub_hourly_seen:
            self._sub_hourly_seen[station.icao] = sub_hourly
            log.info(
                "nws 5-minute availability",
                extra={"context": {
                    "icao": station.icao, "sub_hourly_available": sub_hourly,
                    "median_gap_s": gap_s, "n_observations": len(observations),
                }},
            )
        else:
            self._sub_hourly_seen[station.icao] = sub_hourly

        journalled = 0
        # The freshest observation in the response is the feed's real latency;
        # the other 23 are backfill and their "age" says nothing about lag. The
        # flag lets the report measure latency without averaging in history.
        newest_at = max(ts for ts, _c, _raw in observations)
        # Oldest first, so the running max advances in real order.
        for observed_at, celsius, has_raw in sorted(observations):
            source, precision = classify_precision(
                celsius, has_raw, sub_hourly_station=sub_hourly)
            temp_f = _c_to_f(celsius)
            climate_date = timeutils.climate_date_str(
                observed_at, station.standard_utc_offset_hours)
            key = (station.icao, climate_date)
            prior = self._running.get(key)
            running = temp_f if prior is None else max(prior, temp_f)

            await self._db.record_source_observation({
                "icao": station.icao,
                "source": source,
                "climate_date": climate_date,
                "observed_at_utc": observed_at.isoformat(),
                "received_at_utc": received_at.isoformat(),
                "publication_age_s": (received_at - observed_at).total_seconds(),
                "temp_f": temp_f,
                "raw_value": (f"{celsius}C|newest" if observed_at == newest_at
                              else f"{celsius}C"),
                "precision_f": precision,
                "running_max_f": running,
            })
            journalled += 1

            # A new high on THIS source may prove a crossing earlier than the
            # METAR will. Shadow only -- see the module docstring.
            if source == SOURCE_5MIN and (prior is None or running > prior):
                self._running[key] = running
                if self._on_shadow is not None:
                    try:
                        await self._on_shadow(
                            icao=station.icao, climate_date=climate_date,
                            tmax_f=running, source=SOURCE_5MIN,
                            observed_at_utc=observed_at,
                        )
                    except Exception:  # noqa: BLE001
                        log.exception("shadow lock-in observation failed")
            elif prior is None or running > prior:
                self._running[key] = running
        return journalled

    def availability(self) -> dict[str, bool]:
        """icao -> whether a sub-hourly observation has ever been served."""
        return dict(self._sub_hourly_seen)


def _parse_features(payload: Any) -> list[tuple[datetime, float, bool]]:
    """(observed_at, degC, has_raw_metar) for every usable GeoJSON feature."""
    out: list[tuple[datetime, float, bool]] = []
    if not isinstance(payload, dict):
        return out
    for feature in payload.get("features") or []:
        if not isinstance(feature, dict):
            continue
        props = feature.get("properties")
        if not isinstance(props, dict):
            continue
        temp = props.get("temperature")
        value = temp.get("value") if isinstance(temp, dict) else None
        stamp = props.get("timestamp")
        if value is None or not stamp:
            continue
        try:
            observed_at = timeutils.parse_isoformat_utc(str(stamp).replace("Z", "+00:00"))
            celsius = float(value)
        except (ValueError, TypeError):
            continue
        out.append((observed_at, celsius, bool(props.get("rawMessage"))))
    return out
