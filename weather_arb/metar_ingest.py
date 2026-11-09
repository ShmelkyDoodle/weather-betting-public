"""Real-time METAR/ASOS ingestion with sensor-glitch protection.

The poller fetches surface observations for the configured ICAO stations every
``metar_poll_seconds`` (default 45s), decodes temperature/dew point, maintains a
*validated* running daily maximum (in the NWS Local-Standard-Time climate day),
and persists everything to the ledger.  Whenever the validated max advances it
invokes the ``on_new_high`` callback so the arbitrage engine can react to a
freshly locked-in temperature immediately (event-driven, not just on the poll
timer).

Glitch protection
-----------------
ASOS temperature sensors occasionally emit a single spurious spike.  Acting on
such a spike would place a "deterministic" lock-in order against a temperature
that never really happened.  A new high is therefore only *validated* (and only
then used to move ``T_max``) when at least one of the following holds:

1. The rise is **kinematically plausible** -- the air could physically have
   warmed this much in the time elapsed since the last trusted reading (see
   :func:`plausible_rise`) -- accepted immediately.
2. The reading is **confirmed by a subsequent reading within 5 minutes** at a
   comparable temperature.
3. The reading is **corroborated by a nearby station** (same metro, within
   ~60 km) reporting a comparably high temperature in the same window.

Why rule 1 is a *rate* and not a step
-------------------------------------
It used to be a flat ``jump <= spike_threshold_f`` (5 deg F). That silently broke
the stations that warm fastest. METARs arrive roughly hourly, so at a site whose
ordinary morning warm-up exceeds 5 deg F/hour every routine observation looked
like a spike; rule 2 then needed a confirming reading *within 5 minutes*, which
an hourly feed can never supply, so ``validated_max_f`` froze near the morning
value for the rest of the day. Measured 2026-08-21: ``daily_tmax`` was short by
>= 2 deg F on 21% of city-days, by a mean of 11.5 deg F at DEN and 7.2 deg F at
AUS, while MIA and LAX (which warm gently) were perfect. That corrupts the
Strategy-1 lock-in and the intraday model's running-max input.

The rule now bounds how fast the air may warm rather than how far it may step, so
reporting hourly is no longer treated as suspicious. A genuine sensor spike is
still caught, because a spike is fast *relative to the elapsed time*.

In addition, physically impossible readings (dew point far above air
temperature, or temperatures outside a sane envelope) are rejected outright and
logged with ``accepted = 0`` for audit.
"""

from __future__ import annotations

import asyncio
import logging
import math
import random
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Awaitable, Callable, Optional, Protocol

import aiohttp

from . import timeutils
from .config import Settings, StationConfig
from .db import Database
from .models import MetarObservation

log = logging.getLogger(__name__)

NewHighCallback = Callable[[str, str, float], Awaitable[None]]

# Physically sane air-temperature envelope for the monitored US stations (deg F).
_MIN_PLAUSIBLE_F = -80.0
_MAX_PLAUSIBLE_F = 140.0
# Dew point may legitimately equal air temperature (saturation); allow a small
# rounding slack before declaring a sensor fault.
_DEWPOINT_SLACK_F = 1.5
# Two stations closer than this are treated as mutually corroborating.
_CORROBORATION_KM = 60.0


# IEM ``current.py`` needs the state ASOS network; map the default stations.
_IEM_NETWORKS: dict[str, str] = {
    "KORD": "IL_ASOS",
    "KMDW": "IL_ASOS",
    "KNYC": "NY_ASOS",
    "KBOS": "MA_ASOS",
}


class MetarProvider(Protocol):
    """A source of decoded observations for a batch of ICAO codes."""

    async def fetch(
        self, session: aiohttp.ClientSession, stations: tuple[StationConfig, ...]
    ) -> list[MetarObservation]:
        ...


class AviationWeatherProvider:
    """NOAA Aviation Weather Center METAR API (``aviationweather.gov``).

    Returns current observations for many stations in a single request.  Fields
    used: ``icaoId``, ``obsTime`` (epoch seconds), ``temp``/``dewp`` (deg C),
    ``rawOb`` (raw METAR text).
    """

    BASE_URL = "https://aviationweather.gov/api/data/metar"

    async def fetch(
        self, session: aiohttp.ClientSession, stations: tuple[StationConfig, ...]
    ) -> list[MetarObservation]:
        ids = ",".join(s.icao for s in stations)
        params = {"ids": ids, "format": "json", "taf": "false"}
        payload = await _get_json_with_retry(session, self.BASE_URL, params)
        if not isinstance(payload, list):
            return []
        observations: list[MetarObservation] = []
        for item in payload:
            obs = _parse_awc_item(item)
            if obs is not None:
                observations.append(obs)
        return observations


class IEMProvider:
    """Iowa Environmental Mesonet current-observation JSON API.

    Fetched one station at a time (the ``current.py`` endpoint is per-station and
    needs the state ASOS network).  Key extraction is defensive because IEM has
    exposed slightly different field names over time.
    """

    BASE_URL = "https://mesonet.agron.iastate.edu/json/current.py"

    async def fetch(
        self, session: aiohttp.ClientSession, stations: tuple[StationConfig, ...]
    ) -> list[MetarObservation]:
        tasks = [self._fetch_one(session, s) for s in stations]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        observations: list[MetarObservation] = []
        for station, result in zip(stations, results):
            if isinstance(result, Exception):
                log.warning(
                    "IEM fetch failed",
                    extra={"context": {"icao": station.icao, "error": str(result)}},
                )
                continue
            if result is not None:
                observations.append(result)
        return observations

    async def _fetch_one(
        self, session: aiohttp.ClientSession, station: StationConfig
    ) -> Optional[MetarObservation]:
        network = _IEM_NETWORKS.get(station.icao)
        if network is None:
            return None
        iem_id = station.icao[1:] if station.icao.startswith("K") else station.icao
        params = {"station": iem_id, "network": network}
        payload = await _get_json_with_retry(session, self.BASE_URL, params)
        return _parse_iem_payload(station.icao, payload)


def build_provider(source: str) -> MetarProvider:
    """Factory selecting a provider by ``settings.metar_source``."""
    if source.lower() == "iem":
        return IEMProvider()
    return AviationWeatherProvider()


def max_plausible_rise_f(
    elapsed_seconds: float,
    *,
    rate_f_per_hour: float = 8.0,
    buffer_f: float = 1.0,
    absolute_cap_f: float = 15.0,
    rapid_update_seconds: float = 60.0,
    rapid_update_cap_f: float = 2.0,
) -> float:
    """Largest believable warming, in deg F, over ``elapsed_seconds``.

    Three regimes, in order of precedence:

    * **Sub-minute** (``elapsed < rapid_update_seconds``) -- SPECI bursts and the
      1-minute augmenter can restate a temperature seconds apart. Rate is
      meaningless at that resolution, so a small fixed allowance applies.
    * **Rate-limited** -- ``rate_f_per_hour`` per hour elapsed, plus ``buffer_f``
      of slack for the 1 deg F rounding in a METAR group and for clock jitter.
    * **Hard cap** -- never more than ``absolute_cap_f`` however long the gap.
      Without this the allowance would grow without bound across a data outage
      and a spike arriving after one would sail through.
    """
    if elapsed_seconds < rapid_update_seconds:
        return rapid_update_cap_f
    allowance = rate_f_per_hour * (elapsed_seconds / 3600.0) + buffer_f
    return min(allowance, absolute_cap_f)


def plausible_rise(
    last_temp_f: float,
    last_time: datetime,
    new_temp_f: float,
    new_time: datetime,
    **limits: float,
) -> bool:
    """True iff warming from ``last_temp_f`` to ``new_temp_f`` over the elapsed
    time is physically believable.

    **Cooling is always plausible.** A falling temperature cannot inflate the
    daily maximum, so there is nothing to protect against and rate-limiting it
    would only risk rejecting a real reading. Out-of-order timestamps are treated
    as zero elapsed time (the strictest regime) rather than trusted as a long gap.

    ``limits`` are forwarded to :func:`max_plausible_rise_f`.
    """
    if new_temp_f <= last_temp_f:
        return True
    elapsed = (new_time - last_time).total_seconds()
    return (new_temp_f - last_temp_f) <= max_plausible_rise_f(max(elapsed, 0.0), **limits)


@dataclass(slots=True)
class _StationTrack:
    """Per-station in-memory glitch-protection state (one climate day)."""

    climate_date: str
    validated_max_f: Optional[float] = None
    pending_temp_f: Optional[float] = None
    pending_since: Optional[datetime] = None
    last_temp_f: Optional[float] = None
    last_temp_at: Optional[datetime] = None
    last_obs_time: Optional[datetime] = None
    # Last reading we actually BELIEVED -- i.e. not one held as an unconfirmed
    # spike. The velocity check measures from this, never from `last_temp_f`:
    # a held spike must not become the baseline that legitimises the next one.
    trusted_temp_f: Optional[float] = None
    trusted_at: Optional[datetime] = None


class MetarPoller:
    """Owns the async polling loop and the running ``T_max`` per station."""

    def __init__(
        self,
        *,
        session: aiohttp.ClientSession,
        db: Database,
        settings: Settings,
        on_new_high: Optional[NewHighCallback] = None,
    ) -> None:
        self._session = session
        self._db = db
        self._settings = settings
        self._provider = build_provider(settings.metar_source)
        self._on_new_high = on_new_high
        self._tracks: dict[str, _StationTrack] = {}

    async def run(self, stop_event: asyncio.Event) -> None:
        """Poll until ``stop_event`` is set, sleeping between cycles with jitter."""
        log.info(
            "metar poller started",
            extra={
                "context": {
                    "stations": [s.icao for s in self._settings.stations],
                    "source": self._settings.metar_source,
                    "interval_s": self._settings.metar_poll_seconds,
                }
            },
        )
        while not stop_event.is_set():
            try:
                await self._poll_once()
            except asyncio.CancelledError:
                raise
            except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                # Expected transient upstream issue (timeout, 5xx, dropped conn).
                # Log concisely and retry on the next cycle -- no stack trace.
                log.warning(
                    "metar poll transient error; retrying next cycle",
                    extra={"context": {"error": _brief_error(exc)}},
                )
            except Exception:  # noqa: BLE001 - poller must never die
                log.exception("metar poll cycle failed (unexpected)")
            await self._sleep_with_jitter(stop_event)

    async def _sleep_with_jitter(self, stop_event: asyncio.Event) -> None:
        # +/-15% jitter so all stations don't hammer the API in lockstep.
        base = self._settings.metar_poll_seconds
        delay = base * random.uniform(0.85, 1.15)
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=delay)
        except asyncio.TimeoutError:
            pass

    async def _poll_once(self) -> None:
        observations = await self._provider.fetch(
            self._session, self._settings.stations
        )
        # Sort by time so corroboration sees the most recent readings first.
        observations.sort(key=lambda o: o.observed_at_utc)
        for obs in observations:
            await self._process(obs)

    # ------------------------------------------------------------------ #
    # Core observation handling + glitch protection
    # ------------------------------------------------------------------ #
    async def _process(self, obs: MetarObservation) -> None:
        station = self._settings.station_by_icao(obs.icao)
        if station is None:
            return

        climate_date = timeutils.climate_date_str(
            obs.observed_at_utc, station.standard_utc_offset_hours
        )
        track = self._track_for(obs.icao, climate_date)

        # De-duplicate: METAR only updates ~hourly (plus specials); skip repeats.
        if track.last_obs_time is not None and obs.observed_at_utc <= track.last_obs_time:
            return
        track.last_obs_time = obs.observed_at_utc

        temp_f = obs.temp_f
        if temp_f is None or not _plausible_temp(temp_f):
            await self._record(obs, station, climate_date, accepted=False)
            log.warning(
                "rejected implausible/absent temperature",
                extra={"context": {"icao": obs.icao, "temp_f": temp_f}},
            )
            return

        # Physical sanity: dew point cannot meaningfully exceed air temperature.
        dew_f = obs.dewpoint_f
        if dew_f is not None and dew_f > temp_f + _DEWPOINT_SLACK_F:
            await self._record(obs, station, climate_date, accepted=False)
            log.warning(
                "rejected reading: dewpoint exceeds temperature (sensor fault)",
                extra={"context": {"icao": obs.icao, "temp_f": temp_f, "dew_f": dew_f}},
            )
            return

        accepted_as_high = await self._update_validated_max(
            track, station, climate_date, temp_f, obs.observed_at_utc
        )

        # A reading left holding the pending slot is an unconfirmed spike; every
        # other outcome (not a new high, or validated as one) means we believed it,
        # so it becomes the velocity reference for the next reading.
        if track.pending_temp_f is None:
            track.trusted_temp_f = temp_f
            track.trusted_at = obs.observed_at_utc
        track.last_temp_f = temp_f
        track.last_temp_at = obs.observed_at_utc
        await self._record(obs, station, climate_date, accepted=True)

        if accepted_as_high:
            new_max = track.validated_max_f
            assert new_max is not None
            stored_max = await self._db.update_tmax(
                icao=obs.icao,
                climate_date=climate_date,
                temp_f=new_max,
                observed_at_utc=obs.observed_at_utc,
            )
            log.info(
                "validated new daily max",
                extra={
                    "context": {
                        "icao": obs.icao,
                        "climate_date": climate_date,
                        "tmax_f": stored_max,
                        "reading_f": temp_f,
                    }
                },
            )
            if self._on_new_high is not None:
                await self._on_new_high(obs.icao, climate_date, stored_max)

    async def _update_validated_max(
        self,
        track: _StationTrack,
        station: StationConfig,
        climate_date: str,
        temp_f: float,
        observed_at: datetime,
    ) -> bool:
        """Apply glitch-protection rules; return True iff the validated max rose."""
        # Seed the day's baseline from the DB on first sight (crash recovery):
        # a persisted higher max must not be forgotten, nor re-treated as new.
        if track.validated_max_f is None:
            persisted = await self._db.get_tmax(station.icao, climate_date)
            if persisted is None:
                # First reading of the climate day establishes the baseline high.
                track.validated_max_f = temp_f
                track.trusted_temp_f = temp_f
                track.trusted_at = observed_at
                return True
            # Adopt the persisted max and evaluate this reading against it below.
            track.validated_max_f = persisted

        if temp_f <= track.validated_max_f:
            # Not a new high; nothing to validate. Clear any stale pending spike.
            track.pending_temp_f = None
            track.pending_since = None
            return False

        return self._accept_step(track, station, climate_date, temp_f, observed_at)

    def _accept_step(
        self,
        track: _StationTrack,
        station: StationConfig,
        climate_date: str,
        temp_f: float,
        observed_at: datetime,
    ) -> bool:
        """Decide whether a *new-high* reading may be validated immediately."""
        assert track.validated_max_f is not None
        jump = temp_f - track.validated_max_f

        # (1) Kinematically plausible rise -- the air could have warmed this much
        #     in the time available, so this is ordinary weather, not a spike.
        if self._plausible_step(track, temp_f, observed_at):
            track.validated_max_f = temp_f
            track.pending_temp_f = None
            track.pending_since = None
            return True

        # (2) Confirmed by a prior pending reading within the 5-minute window.
        if self._confirms_pending(track, temp_f, observed_at):
            track.validated_max_f = max(temp_f, track.pending_temp_f or temp_f)
            track.pending_temp_f = None
            track.pending_since = None
            return True

        # (3) Corroborated by a nearby station reporting a comparable high.
        if self._corroborated_by_neighbor(station, temp_f, observed_at):
            track.validated_max_f = temp_f
            track.pending_temp_f = None
            track.pending_since = None
            log.info(
                "spike corroborated by neighbor station",
                extra={"context": {"icao": station.icao, "temp_f": temp_f}},
            )
            return True

        # Otherwise: hold as pending and wait for confirmation. Do NOT move max.
        track.pending_temp_f = temp_f
        track.pending_since = observed_at
        log.warning(
            "unconfirmed temperature spike held pending",
            extra={
                "context": {
                    "icao": station.icao,
                    "reading_f": temp_f,
                    "prior_max_f": track.validated_max_f,
                    "jump_f": round(jump, 2),
                    "trusted_f": track.trusted_temp_f,
                    "rate_f_per_hour": _rate_f_per_hour(
                        track.trusted_temp_f, track.trusted_at, temp_f, observed_at),
                }
            },
        )
        return False

    def _plausible_step(
        self, track: _StationTrack, temp_f: float, observed_at: datetime
    ) -> bool:
        """Is this new high a believable *rate* of warming from the last trusted
        reading?

        With no trusted reference yet -- the first observation after a restart,
        where ``validated_max_f`` was seeded from the DB but no reading has been
        seen this process -- there is no velocity to compute, so this falls back
        to the old flat ``spike_threshold_f`` step against the max. That is the
        conservative direction, and it self-heals on the very next observation.
        """
        assert track.validated_max_f is not None
        if track.trusted_temp_f is None or track.trusted_at is None:
            return (temp_f - track.validated_max_f) <= self._settings.spike_threshold_f
        st = self._settings
        return plausible_rise(
            track.trusted_temp_f, track.trusted_at, temp_f, observed_at,
            rate_f_per_hour=st.max_warming_rate_f_per_hour,
            buffer_f=st.warming_rate_buffer_f,
            absolute_cap_f=st.max_absolute_jump_f,
            rapid_update_seconds=st.rapid_update_seconds,
            rapid_update_cap_f=st.rapid_update_max_jump_f,
        )

    def _confirms_pending(
        self, track: _StationTrack, temp_f: float, observed_at: datetime
    ) -> bool:
        if track.pending_temp_f is None or track.pending_since is None:
            return False
        elapsed = (observed_at - track.pending_since).total_seconds()
        if elapsed < 0 or elapsed > self._settings.confirmation_window_seconds:
            return False
        return abs(temp_f - track.pending_temp_f) <= self._settings.confirmation_tolerance_f

    def _corroborated_by_neighbor(
        self, station: StationConfig, temp_f: float, observed_at: datetime
    ) -> bool:
        window = self._settings.confirmation_window_seconds
        tol = self._settings.confirmation_tolerance_f
        for other in self._settings.stations:
            if other.icao == station.icao:
                continue
            if _haversine_km(station, other) > _CORROBORATION_KM:
                continue
            other_track = self._tracks.get(other.icao)
            if other_track is None or other_track.last_temp_f is None:
                continue
            if other_track.last_temp_at is None:
                continue
            age = abs((observed_at - other_track.last_temp_at).total_seconds())
            if age > window:
                continue
            if other_track.last_temp_f >= temp_f - tol:
                return True
        return False

    # ------------------------------------------------------------------ #
    # Helpers
    # ------------------------------------------------------------------ #
    def _track_for(self, icao: str, climate_date: str) -> _StationTrack:
        track = self._tracks.get(icao)
        if track is None or track.climate_date != climate_date:
            # New station or the LST climate day rolled over -> fresh state.
            track = _StationTrack(climate_date=climate_date)
            self._tracks[icao] = track
        return track

    async def _record(
        self,
        obs: MetarObservation,
        station: StationConfig,
        climate_date: str,
        *,
        accepted: bool,
    ) -> None:
        await self._db.record_observation(
            icao=obs.icao,
            observed_at_utc=obs.observed_at_utc,
            climate_date=climate_date,
            temp_f=obs.temp_f,
            dewpoint_f=obs.dewpoint_f,
            raw_metar=obs.raw_metar,
            accepted=accepted,
        )


# ---------------------------------------------------------------------- #
# Parsing helpers
# ---------------------------------------------------------------------- #
def _parse_awc_item(item: object) -> Optional[MetarObservation]:
    if not isinstance(item, dict):
        return None
    icao = item.get("icaoId") or item.get("station_id")
    obs_time = item.get("obsTime")
    if not icao or obs_time is None:
        return None
    try:
        observed_at = timeutils.from_epoch_seconds(float(obs_time))
    except (TypeError, ValueError):
        return None
    temp_c = _to_float(item.get("temp"))
    dew_c = _to_float(item.get("dewp"))
    raw = str(item.get("rawOb") or "")
    if temp_c is None and raw:
        temp_c, dew_c = _decode_metar_temps(raw, fallback_dew=dew_c)
    return MetarObservation(
        icao=str(icao),
        observed_at_utc=observed_at,
        temp_c=temp_c,
        dewpoint_c=dew_c,
        raw_metar=raw,
    )


def _parse_iem_payload(icao: str, payload: object) -> Optional[MetarObservation]:
    if not isinstance(payload, dict):
        return None
    last_ob = payload.get("last_ob")
    if not isinstance(last_ob, dict):
        return None
    utc_valid = last_ob.get("utc_valid") or last_ob.get("valid")
    if not utc_valid:
        return None
    try:
        observed_at = timeutils.parse_isoformat_utc(str(utc_valid))
    except ValueError:
        return None

    temp_f = _to_float(
        last_ob.get("airtemp[F]")
        or last_ob.get("tmpf")
        or last_ob.get("temp[F]")
    )
    dew_f = _to_float(
        last_ob.get("dewpointtemp[F]")
        or last_ob.get("dwpf")
        or last_ob.get("dewp[F]")
    )
    temp_c = (temp_f - 32.0) * 5.0 / 9.0 if temp_f is not None else None
    dew_c = (dew_f - 32.0) * 5.0 / 9.0 if dew_f is not None else None
    raw = str(last_ob.get("raw") or last_ob.get("metar") or "")
    return MetarObservation(
        icao=icao,
        observed_at_utc=observed_at,
        temp_c=temp_c,
        dewpoint_c=dew_c,
        raw_metar=raw,
    )


# Precise temperature group in a raw METAR, e.g. ``T02500178`` -> 25.0C / 17.8C.
_METAR_TGROUP = re.compile(r"\bT(\d)(\d{3})(\d)(\d{3})\b")
# Coarse group, e.g. ``25/18`` or ``M03/M07``.
_METAR_COARSE = re.compile(r"\b(M?\d{1,2})/(M?\d{1,2})\b")


def _decode_metar_temps(
    raw: str, *, fallback_dew: Optional[float]
) -> tuple[Optional[float], Optional[float]]:
    """Decode temperature/dew point (deg C) from raw METAR as a fallback."""
    match = _METAR_TGROUP.search(raw)
    if match:
        t_sign, t_val, d_sign, d_val = match.groups()
        temp_c = int(t_val) / 10.0 * (-1 if t_sign == "1" else 1)
        dew_c = int(d_val) / 10.0 * (-1 if d_sign == "1" else 1)
        return temp_c, dew_c
    coarse = _METAR_COARSE.search(raw)
    if coarse:
        temp_c = _metar_int(coarse.group(1))
        dew_c = _metar_int(coarse.group(2))
        return temp_c, dew_c
    return None, fallback_dew


def _metar_int(token: str) -> float:
    if token.startswith("M"):
        return -float(token[1:])
    return float(token)


def _to_float(value: object) -> Optional[float]:
    if value is None:
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _plausible_temp(temp_f: float) -> bool:
    return _MIN_PLAUSIBLE_F <= temp_f <= _MAX_PLAUSIBLE_F


# HTTP statuses worth a transparent retry (transient upstream failures).
_RETRYABLE_HTTP = frozenset({429, 500, 502, 503, 504})


async def _get_json_with_retry(
    session: aiohttp.ClientSession,
    url: str,
    params: dict[str, object],
    *,
    attempts: int = 3,
) -> object:
    """GET JSON with bounded exponential backoff on transient failures.

    Retries on 429/5xx and network timeouts so an isolated upstream hiccup
    (e.g. a single ``504 Gateway Time-out``) never reaches the caller.  The
    final failed attempt raises the underlying :class:`aiohttp` error.
    """
    last_exc: Optional[BaseException] = None
    for attempt in range(attempts):
        try:
            async with session.get(url, params=params) as resp:
                if resp.status in _RETRYABLE_HTTP and attempt < attempts - 1:
                    await asyncio.sleep(_retry_backoff(attempt))
                    continue
                resp.raise_for_status()
                return await resp.json(content_type=None)
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            last_exc = exc
            if attempt < attempts - 1:
                await asyncio.sleep(_retry_backoff(attempt))
                continue
            raise
    if last_exc is not None:  # pragma: no cover - defensive
        raise last_exc
    return None


def _retry_backoff(attempt: int) -> float:
    return min(5.0, 0.5 * (2 ** attempt))


def _brief_error(exc: BaseException) -> str:
    """A one-line description of an error, without a traceback."""
    if isinstance(exc, aiohttp.ClientResponseError):
        return f"HTTP {exc.status} {exc.message}".strip()
    return f"{type(exc).__name__}: {exc}".strip()


def _rate_f_per_hour(
    last_temp_f: Optional[float], last_time: Optional[datetime],
    new_temp_f: float, new_time: datetime,
) -> Optional[float]:
    """Observed warming rate in deg F/hour, for logging a rejection in the units
    the guard actually reasons in. ``None`` when it is not computable."""
    if last_temp_f is None or last_time is None:
        return None
    elapsed = (new_time - last_time).total_seconds()
    if elapsed <= 0:
        return None
    return round((new_temp_f - last_temp_f) / (elapsed / 3600.0), 2)


def _haversine_km(a: StationConfig, b: StationConfig) -> float:
    """Great-circle distance between two stations, kilometers."""
    radius = 6371.0
    lat1, lat2 = math.radians(a.latitude), math.radians(b.latitude)
    dlat = lat2 - lat1
    dlon = math.radians(b.longitude - a.longitude)
    h = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    return 2 * radius * math.asin(math.sqrt(h))
