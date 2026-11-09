"""Short-range forecast ingestion and strike-exceedance probability model.

Strategy 2 needs, for each strike, an estimate of ``P(daily T_max >= strike)``.
We obtain the deterministic short-range temperature guidance from the NWS
gridpoint API, whose hourly grids are populated by the National Blend of Models
(NBM) and, at short lead times, by HRRR -- exactly the guidance the task calls
for.  Raw HRRR GRIB decoding is deliberately avoided; the NWS gridpoint product
already exposes the blended hourly temperatures we need without a GRIB toolchain.

Modeling the daily maximum
--------------------------
The quantity that settles a contract is the *daily maximum*, so we take the
maximum of the forecast hourly temperatures over the target climate day as the
location parameter ``mu``.  Forecast error is modeled as Gaussian with a
standard deviation ``sigma`` that grows with lead time (calibrated from the
``ForecastConfig``).  Crucially, the eventual daily max can never fall below the
temperature *already observed today*, so we condition on that: the distribution
of the final max is a **normal truncated below at the observed running max**.

    P(final_max >= strike) =
        1                                              if strike <= observed_max
        [1 - Phi((strike-mu)/sigma)] /
        [1 - Phi((observed_max-mu)/sigma)]             otherwise

Intraday sharpening
-------------------
Lead time is a crude proxy for uncertainty *within* the target day: at 4pm the
day's max is all but decided, yet a lead-time sigma still calls it ~4 deg F
uncertain, smearing probability into buckets that will never settle YES. When an
:class:`~weather_arb.intraday.IntradayCurves` is supplied, every model also gets a
``<model>_id`` variant whose ``(mu, sigma)`` is blended with the intraday
remaining-rise estimate, plus a standalone ``intraday`` model. These are shadow
predictions like ``_bc``/``climo`` -- graded by Brier and by the Strategy-2
backtest -- until ``WA_ENABLE_INTRADAY_FORECAST`` promotes them to the trading
path.
"""

from __future__ import annotations

import asyncio
import logging
import math
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from statistics import NormalDist
from typing import TYPE_CHECKING, Optional

import aiohttp

from . import timeutils
from .config import Settings, StationConfig
from .intraday import blend_normal
from .models import ProbabilityEstimate, WeatherMarket

if TYPE_CHECKING:
    from .climatology import ClimatologyClient
    from .intraday import IntradayCurves, Prediction
    from .mos_ingest import MosClient

log = logging.getLogger(__name__)

_NWS_POINTS_URL = "https://api.weather.gov/points/{lat:.4f},{lon:.4f}"
# Suffix marking a model variant sharpened by the intraday remaining-rise curve
# (mirrors the ``_bc`` bias-corrected suffix), and the name of the standalone
# intraday model.
INTRADAY_SUFFIX = "_id"
INTRADAY_MODEL = "intraday"


def base_model_name(model: str) -> str:
    """The unsharpened model behind ``model``.

    Both the ``<model>_id`` blends and the standalone ``intraday`` model depend on
    the intraday curve, so both must answer to ``WA_ENABLE_INTRADAY_FORECAST``;
    ``intraday`` has no base model of its own, so it falls back to ``current``.
    """
    if model == INTRADAY_MODEL:
        return "current"
    if model.endswith(INTRADAY_SUFFIX):
        return model[: -len(INTRADAY_SUFFIX)] or "current"
    return model


@dataclass(slots=True)
class _GridForecast:
    """Cached forecast temperature series (deg F) for one station."""

    grid_url: str
    fetched_at_utc: datetime
    # (period_start_utc, temp_f) for each forecast grid value.
    values: tuple[tuple[datetime, float], ...]


class ForecastClient:
    """Fetches NWS/NBM gridded temperatures and computes exceedance probability."""

    def __init__(
        self,
        *,
        session: aiohttp.ClientSession,
        settings: Settings,
        mos: Optional["MosClient"] = None,
        climo: Optional["ClimatologyClient"] = None,
        intraday: Optional["IntradayCurves"] = None,
    ) -> None:
        self._session = session
        self._settings = settings
        self._mos = mos
        self._climo = climo
        self._intraday = intraday
        self._grid_urls: dict[str, str] = {}
        self._cache: dict[str, _GridForecast] = {}

    # ------------------------------------------------------------------ #
    # Public API
    # ------------------------------------------------------------------ #
    async def probability(
        self,
        *,
        station: StationConfig,
        strike_temp_f: float,
        climate_date: str,
        observed_tmax_f: Optional[float],
    ) -> Optional[ProbabilityEstimate]:
        """Return ``P(T_max >= strike)`` for a station/day, or ``None`` on failure.

        ``observed_tmax_f`` (the validated running max so far today) is used as
        the lower truncation bound; pass ``None`` before any observation exists.
        """
        forecast = await self._get_forecast(station)
        if forecast is None:
            return None

        day_temps = self._temps_for_climate_day(
            forecast, climate_date, station.standard_utc_offset_hours
        )
        if not day_temps:
            log.warning(
                "no forecast hours for climate day",
                extra={"context": {"icao": station.icao, "date": climate_date}},
            )
            return None

        mu = max(day_temps)
        sigma = self._sigma_for(forecast, climate_date, station)
        lower_bound = observed_tmax_f if observed_tmax_f is not None else -1e9
        # A max can't be below what's already been observed.
        mu = max(mu, lower_bound if observed_tmax_f is not None else mu)

        prob = _truncated_normal_exceed(mu, sigma, strike_temp_f, lower_bound)
        return ProbabilityEstimate(
            icao=station.icao,
            strike_temp_f=strike_temp_f,
            probability=prob,
            forecast_high_f=mu,
            sigma_f=sigma,
            lower_bound_f=lower_bound if observed_tmax_f is not None else mu,
        )

    async def market_probability(
        self,
        *,
        station: StationConfig,
        market: WeatherMarket,
        climate_date: str,
        observed_tmax_f: Optional[float],
        apply_intraday: bool = False,
    ) -> Optional[ProbabilityEstimate]:
        """Return ``P(bucket settles YES)`` for a (possibly ranged) market.

        Handles all Kalshi bucket shapes: tail-high (``floor`` only), tail-low
        (``cap`` only) and interior (``floor`` and ``cap``).  The final daily max
        is modeled as a normal truncated below at the temperature already
        observed today.

        This is the ``current`` fast path (gridpoint only, no MOS fetches).
        ``apply_intraday`` blends in the intraday remaining-rise estimate when one
        is available, making it equivalent to the ``current_id`` variant.
        """
        grid = await self._grid_forecast_high(station, climate_date)
        if grid is None:
            return None
        mu, sigma = grid
        if apply_intraday:
            intra = await self._intraday_prediction(station, climate_date, observed_tmax_f)
            if intra is not None:
                mu, sigma = blend_normal(
                    mu, sigma, intra.predicted_max_f, intra.sigma_f,
                    sigma_floor=self._settings.forecast.intraday_sigma_floor_f,
                )
        return self._estimate(station, market, mu, sigma, observed_tmax_f)

    async def raw_model_highs(
        self, station: StationConfig, climate_date: str
    ) -> dict[str, float]:
        """Uncorrected forecast daily-high (°F) per model, plus ``ensemble`` (their mean)."""
        means: dict[str, float] = {}
        grid = await self._grid_forecast_high(station, climate_date)
        if grid is not None:
            means["current"] = grid[0]
        if self._mos is not None:
            from .mos_ingest import MODEL_CODES
            for name, code in MODEL_CODES.items():
                high = await self._mos.daily_max_f(
                    station.icao, code, climate_date, station.standard_utc_offset_hours
                )
                if high is not None:
                    means[name] = high
        if len(means) >= 2:
            means["ensemble"] = sum(means.values()) / len(means)
        return means

    async def model_probabilities(
        self,
        *,
        station: StationConfig,
        market: WeatherMarket,
        climate_date: str,
        observed_tmax_f: Optional[float],
        bias: Optional[dict[str, float]] = None,
    ) -> dict[str, ProbabilityEstimate]:
        """Return P(YES) from every forecast model, keyed by name.

        Base models: ``current`` (NWS gridpoint), ``nbm``/``gfs_mos`` (station MOS),
        ``ensemble`` (their mean, sigma widened by disagreement = a real spread).
        When ``bias`` (per-model °F correction from recent error) is supplied, a
        ``<model>_bc`` bias-corrected variant is emitted alongside each, so the
        Brier report measures whether the correction actually helps. When an
        intraday curve is available for the day in progress, a ``<model>_id``
        intraday-sharpened variant is emitted the same way, plus a standalone
        ``intraday`` model (the remaining-rise estimate on its own, so the report
        can tell "the blend helps" apart from "the forecast was never needed").
        The engine logs all of these; trading is unaffected.
        """
        grid = await self._grid_forecast_high(station, climate_date)
        base_sigma = grid[1] if grid is not None else self._settings.forecast.base_sigma_f
        means: dict[str, float] = {}
        if grid is not None:
            means["current"] = grid[0]
        if self._mos is not None:
            from .mos_ingest import MODEL_CODES
            for name, code in MODEL_CODES.items():
                high = await self._mos.daily_max_f(
                    station.icao, code, climate_date, station.standard_utc_offset_hours
                )
                if high is not None:
                    means[name] = high
        if not means:
            return {}

        sigmas: dict[str, float] = {name: base_sigma for name in means}
        if len(means) >= 2:
            values = list(means.values())
            ens_mu = sum(values) / len(values)
            var = sum((v - ens_mu) ** 2 for v in values) / len(values)
            means["ensemble"] = ens_mu
            sigmas["ensemble"] = math.sqrt(base_sigma ** 2 + var)

        intra = await self._intraday_prediction(station, climate_date, observed_tmax_f)
        floor = self._settings.forecast.intraday_sigma_floor_f

        out: dict[str, ProbabilityEstimate] = {}
        for name, mu in means.items():
            out[name] = self._estimate(station, market, mu, sigmas[name], observed_tmax_f)
            if bias and name in bias:
                out[f"{name}_bc"] = self._estimate(
                    station, market, mu + bias[name], sigmas[name], observed_tmax_f
                )
            if intra is not None:
                mu_id, sigma_id = blend_normal(
                    mu, sigmas[name], intra.predicted_max_f, intra.sigma_f, sigma_floor=floor
                )
                out[f"{name}{INTRADAY_SUFFIX}"] = self._estimate(
                    station, market, mu_id, sigma_id, observed_tmax_f
                )
        if intra is not None:
            out[INTRADAY_MODEL] = self._estimate(
                station, market, intra.predicted_max_f, intra.sigma_f, observed_tmax_f
            )

        # Climatology base-rate pillar + a forecast/climatology blend.
        if self._climo is not None:
            dist = await self._climo.distribution(
                station.icao, climate_date, station.standard_utc_offset_hours
            )
            if dist is not None:
                cmean, cstd, _ = dist
                out["climo"] = self._estimate(station, market, cmean, cstd, observed_tmax_f)
                base = "ensemble" if "ensemble" in out else "current"
                if base in out:
                    blended = 0.5 * out["climo"].probability + 0.5 * out[base].probability
                    out["blend"] = ProbabilityEstimate(
                        icao=station.icao, strike_temp_f=market.strike_temp_f,
                        probability=blended,
                        forecast_high_f=(cmean + out[base].forecast_high_f) / 2.0,
                        sigma_f=cstd, lower_bound_f=out["climo"].lower_bound_f,
                    )
        return out

    async def _intraday_prediction(
        self,
        station: StationConfig,
        climate_date: str,
        observed_tmax_f: Optional[float],
    ) -> Optional["Prediction"]:
        """The intraday remaining-rise estimate, or ``None`` when unavailable.

        ``None`` covers every "not applicable" case -- no intraday client wired,
        no observation yet, a future/past climate day, or a curve still building
        in the background -- and every caller degrades to the static forecast.
        """
        if self._intraday is None:
            return None
        try:
            return await self._intraday.prediction(
                station=station, climate_date=climate_date, observed_tmax_f=observed_tmax_f
            )
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - sharpening must never break forecasting
            log.exception(
                "intraday prediction failed; using unsharpened forecast",
                extra={"context": {"icao": station.icao, "date": climate_date}},
            )
            return None

    async def _grid_forecast_high(
        self, station: StationConfig, climate_date: str
    ) -> Optional[tuple[float, float]]:
        """(forecast daily-max °F, sigma °F) from the NWS gridpoint, or None."""
        forecast = await self._get_forecast(station)
        if forecast is None:
            return None
        day_temps = self._temps_for_climate_day(
            forecast, climate_date, station.standard_utc_offset_hours
        )
        if not day_temps:
            return None
        return max(day_temps), self._sigma_for(forecast, climate_date, station)

    def _estimate(
        self,
        station: StationConfig,
        market: WeatherMarket,
        mu: float,
        sigma: float,
        observed_tmax_f: Optional[float],
    ) -> ProbabilityEstimate:
        """Build a bucket-exceedance estimate for a given mean/sigma."""
        if observed_tmax_f is not None:
            mu = max(mu, observed_tmax_f)
        # The continuous region that ROUNDS into this bucket -- not the raw integer
        # strikes. An interior bucket covers two whole degrees of settlement, so
        # pricing it as a 1F window lost ~30% of the mass. See
        # WeatherMarket.settlement_interval_f.
        lo, hi = market.settlement_interval_f()
        prob = _truncated_normal_between(mu, sigma, lo, hi, observed_tmax_f)
        return ProbabilityEstimate(
            icao=station.icao,
            strike_temp_f=market.strike_temp_f,
            probability=prob,
            forecast_high_f=mu,
            sigma_f=sigma,
            lower_bound_f=observed_tmax_f if observed_tmax_f is not None else mu,
        )

    # ------------------------------------------------------------------ #
    # NWS gridpoint fetching (with per-station caching)
    # ------------------------------------------------------------------ #
    async def _get_forecast(self, station: StationConfig) -> Optional[_GridForecast]:
        cached = self._cache.get(station.icao)
        ttl = self._settings.forecast.refresh_seconds
        if cached is not None:
            age = (timeutils.utc_now() - cached.fetched_at_utc).total_seconds()
            if age < ttl:
                return cached

        try:
            grid_url = await self._grid_url_for(station)
            values = await self._fetch_grid_temperatures(grid_url)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - degrade gracefully to stale/None
            log.exception(
                "forecast fetch failed",
                extra={"context": {"icao": station.icao}},
            )
            return cached  # fall back to last good forecast if we have one

        forecast = _GridForecast(
            grid_url=grid_url,
            fetched_at_utc=timeutils.utc_now(),
            values=values,
        )
        self._cache[station.icao] = forecast
        return forecast

    async def _grid_url_for(self, station: StationConfig) -> str:
        if station.icao in self._grid_urls:
            return self._grid_urls[station.icao]
        url = _NWS_POINTS_URL.format(lat=station.latitude, lon=station.longitude)
        async with self._session.get(url) as resp:
            resp.raise_for_status()
            payload = await resp.json(content_type=None)
        grid_url = str(payload["properties"]["forecastGridData"])
        self._grid_urls[station.icao] = grid_url
        return grid_url

    async def _fetch_grid_temperatures(
        self, grid_url: str
    ) -> tuple[tuple[datetime, float], ...]:
        async with self._session.get(grid_url) as resp:
            resp.raise_for_status()
            payload = await resp.json(content_type=None)
        temperature = payload["properties"]["temperature"]
        unit = str(temperature.get("uom", "wmoUnit:degC"))
        celsius = unit.endswith("degC")
        values: list[tuple[datetime, float]] = []
        for entry in temperature.get("values", []):
            valid_time = str(entry.get("validTime", ""))
            raw_value = entry.get("value")
            if raw_value is None or "/" not in valid_time:
                continue
            start_str, duration_str = valid_time.split("/", 1)
            try:
                start = timeutils.parse_isoformat_utc(start_str)
            except ValueError:
                continue
            temp_f = float(raw_value) * 9.0 / 5.0 + 32.0 if celsius else float(raw_value)
            # Each grid value covers an ISO-8601 duration; expand hour-by-hour so
            # a multi-hour block is represented at each hour it spans.
            hours = max(1, _iso_duration_hours(duration_str))
            for offset in range(hours):
                values.append((start + timedelta(hours=offset), temp_f))
        return tuple(values)

    # ------------------------------------------------------------------ #
    # Modeling helpers
    # ------------------------------------------------------------------ #
    def _temps_for_climate_day(
        self,
        forecast: _GridForecast,
        climate_date: str,
        standard_offset_hours: int,
    ) -> list[float]:
        start_utc, end_utc = _climate_day_bounds_utc(climate_date, standard_offset_hours)
        return [
            temp_f
            for (period_start, temp_f) in forecast.values
            if start_utc <= period_start < end_utc
        ]

    def _sigma_for(
        self,
        forecast: _GridForecast,
        climate_date: str,
        station: StationConfig,
    ) -> float:
        cfg = self._settings.forecast
        # Lead time to the afternoon of the target day (peak-heating window).
        _, end_utc = _climate_day_bounds_utc(
            climate_date, station.standard_utc_offset_hours
        )
        lead_hours = max(0.0, (end_utc - timeutils.utc_now()).total_seconds() / 3600.0)
        sigma = cfg.base_sigma_f + cfg.sigma_per_hour_lead_f * lead_hours
        return min(cfg.max_sigma_f, max(cfg.base_sigma_f, sigma))


def _truncated_normal_exceed(
    mu: float, sigma: float, strike: float, lower_bound: float
) -> float:
    """P(X >= strike | X >= lower_bound) for X ~ Normal(mu, sigma)."""
    if strike <= lower_bound:
        return 1.0
    if sigma <= 1e-9:
        return 1.0 if mu >= strike else 0.0
    dist = NormalDist(mu, sigma)
    tail_strike = 1.0 - dist.cdf(strike)
    tail_lower = 1.0 - dist.cdf(lower_bound)
    if tail_lower <= 1e-9:
        # Forecast said reaching the (already observed) lower bound was ~impossible,
        # yet it happened -- guidance is biased low; treat exceedance as likely.
        return 1.0 if mu >= strike else min(1.0, tail_strike / 1e-9)
    return max(0.0, min(1.0, tail_strike / tail_lower))


def _truncated_normal_between(
    mu: float,
    sigma: float,
    lo: float,
    hi: float,
    lower_bound: Optional[float],
) -> float:
    """P(lo <= X <= hi | X >= lower_bound) for X ~ Normal(mu, sigma).

    ``lo``/``hi`` may be ``-inf``/``+inf`` for tail buckets.  With no
    ``lower_bound`` the conditioning is dropped.
    """
    lo_eff = lo
    if lower_bound is not None:
        lo_eff = max(lo, lower_bound)
    if hi < lo_eff:
        return 0.0
    if sigma <= 1e-9:
        point = mu if lower_bound is None else max(mu, lower_bound)
        return 1.0 if lo <= point <= hi else 0.0
    dist = NormalDist(mu, sigma)
    denom = 1.0 if lower_bound is None else max(1.0 - dist.cdf(lower_bound), 1e-9)
    numerator = _cdf(dist, hi) - _cdf(dist, lo_eff)
    return max(0.0, min(1.0, numerator / denom))


def _cdf(dist: NormalDist, x: float) -> float:
    """``NormalDist.cdf`` extended to accept infinite bounds."""
    if x == math.inf:
        return 1.0
    if x == -math.inf:
        return 0.0
    return dist.cdf(x)


def _climate_day_bounds_utc(
    climate_date: str, standard_offset_hours: int
) -> tuple[datetime, datetime]:
    """UTC [start, end) covering the LST climate day (delegates to timeutils)."""
    return timeutils.climate_day_bounds_utc(climate_date, standard_offset_hours)


def _iso_duration_hours(duration: str) -> int:
    """Parse the hour count from an ISO-8601 duration like ``PT1H`` / ``P1DT6H``.

    Only day and hour components affect hourly bucketing; minutes/seconds round
    down to whole hours (grid values are hourly or coarser).
    """
    if not duration.startswith("P"):
        return 1
    days = 0
    hours = 0
    number = ""
    in_time = False
    for ch in duration[1:]:
        if ch == "T":
            in_time = True
            number = ""
            continue
        if ch.isdigit():
            number += ch
            continue
        value = int(number) if number else 0
        if ch == "D":
            days = value
        elif ch == "H" and in_time:
            hours = value
        # Minutes/seconds ('M'/'S') are ignored for hourly bucketing.
        number = ""
    return days * 24 + hours
