"""Intraday daily-max probability model: given the running max so far and the
local hour, how much higher will the day still go?

The static forecast scales its sigma by lead time only. But *within* the day the
uncertainty about the daily max collapses in a very specific way: once you are
past peak heating (mid-afternoon), the running max is essentially the final max --
remaining rise -> 0 with tiny variance. Before peak, the temperature is still
climbing and the max is uncertain.

This module learns that shape from history: for a station and target date it pulls
a windowed multi-year sample of hourly obs from the IEM archive, and for every
Local-Standard-Time hour ``h`` measures the **remaining rise** =
``daily_max - running_max_at_h`` (>= 0). Aggregated per hour that gives a
climatological ``(mean, std)`` curve. The live prediction is then::

    daily_max ~ Normal(running_max_now + mean_rise[h], std_rise[h])   (>= running_max_now)

so a Strategy-2 forecast gets sharp late in the day and appropriately wide in the
morning. Read-only; reuses the IEM archive + LST climate-day logic.

    python -m weather_arb.intraday [--city X] [--now] [--json]

Live use
--------
:class:`IntradayCurves` is the engine-facing wrapper: it caches one curve per
(station, calendar date) and never blocks an evaluation cycle on the archive.
:func:`blend_normal` combines the intraday estimate with the static forecast --
see :mod:`weather_arb.forecast_ingest`, which emits the blended ``<model>_id``
shadow variants and (behind ``WA_ENABLE_INTRADAY_FORECAST``) trades them.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import time
from collections import defaultdict
from dataclasses import dataclass
from datetime import date as date_cls, datetime, timedelta, timezone
from statistics import fmean, pstdev
from typing import TYPE_CHECKING, Optional

import aiohttp

from . import timeutils
from .config import Settings
from .divergence import CITY_ICAO

if TYPE_CHECKING:
    from .config import StationConfig

log = logging.getLogger(__name__)

_IEM_URL = "https://mesonet.agron.iastate.edu/cgi-bin/request/asos.py"
_IEM_NETWORKS = {
    "KMDW": "IL_ASOS", "KORD": "IL_ASOS", "KNYC": "NY_ASOS", "KMIA": "FL_ASOS",
    "KAUS": "TX_ASOS", "KDEN": "CO_ASOS", "KLAX": "CA_ASOS", "KPHL": "PA_ASOS",
}
_MIN_REQUEST_SPACING_S = 1.2


# --------------------------------------------------------------------------- #
# Pure model core
# --------------------------------------------------------------------------- #
def remaining_rise_samples(
    obs: list[tuple[datetime, float]], offset: int
) -> dict[int, list[float]]:
    """Local-hour -> list of ``daily_max - running_max_at_that_ob`` samples.

    Obs are grouped into LST climate days; within each day they are walked in time
    order tracking the running max, and each ob contributes one sample under its
    local hour. All samples are non-negative (the daily max is >= any running max)."""
    by_day: dict[str, list[tuple[datetime, float]]] = defaultdict(list)
    for ts, temp in obs:
        by_day[timeutils.climate_date_str(ts, offset)].append((ts, temp))

    out: dict[int, list[float]] = defaultdict(list)
    for day_obs in by_day.values():
        day_obs.sort(key=lambda x: x[0])
        daily_max = max(t for _, t in day_obs)
        running = -1e9
        for ts, temp in day_obs:
            running = max(running, temp)
            local_hour = (ts + timedelta(hours=offset)).hour
            out[local_hour].append(daily_max - running)
    return out


def build_curve(samples: dict[int, list[float]], min_samples: int = 20
                ) -> dict[int, tuple[float, float, int]]:
    """Per-hour ``(mean_remaining_rise, std, n)``, keeping hours with enough data."""
    curve: dict[int, tuple[float, float, int]] = {}
    for hour, vals in samples.items():
        if len(vals) >= min_samples:
            std = pstdev(vals) if len(vals) > 1 else 0.0
            curve[hour] = (fmean(vals), std, len(vals))
    return curve


@dataclass(frozen=True, slots=True)
class Prediction:
    predicted_max_f: float
    sigma_f: float
    mean_rise_f: float
    n: int


def predict(curve: dict[int, tuple[float, float, int]], local_hour: int,
            running_max_now: float, sigma_floor: float = 1.0) -> Optional[Prediction]:
    """Daily-max distribution given the current local hour + running max, or None
    if that hour has no climatology."""
    entry = curve.get(local_hour)
    if entry is None:
        return None
    mean_rise, std, n = entry
    return Prediction(
        predicted_max_f=running_max_now + mean_rise,
        sigma_f=max(std, sigma_floor),
        mean_rise_f=mean_rise, n=n,
    )


def blend_normal(
    mu_a: float, sigma_a: float, mu_b: float, sigma_b: float, sigma_floor: float = 1.0
) -> tuple[float, float]:
    """Combine two normal estimates of the same daily max into one ``(mu, sigma)``.

    The mean is **precision-weighted** (``1/sigma^2``), so whichever estimate is
    currently sharper dominates: in the morning the intraday remaining-rise
    spread is wide and the static forecast carries the answer; after peak heating
    the rise collapses to ~0 with tiny variance and the observed running max takes
    over. That switchover is the whole point of the model.

    The sigma is deliberately **not** the Bayesian product-rule
    ``sqrt(1/(1/sa^2 + 1/sb^2))``. That formula assumes the two errors are
    independent, and they are not -- both describe the same air mass on the same
    day, so the product rule would manufacture confidence the pair does not have.
    We take the sharper of the two instead: a strict improvement over the static
    sigma, with no unearned narrowing. ``sigma_floor`` then holds back the
    settlement-basis gap the intraday curve knows nothing about (hourly-METAR
    running max vs. Kalshi's 1-minute CLI max) -- see
    ``ForecastConfig.intraday_sigma_floor_f``.
    """
    sa = max(sigma_a, 1e-6)
    sb = max(sigma_b, 1e-6)
    wa, wb = 1.0 / (sa * sa), 1.0 / (sb * sb)
    mu = (wa * mu_a + wb * mu_b) / (wa + wb)
    return mu, max(min(sa, sb), sigma_floor)


# --------------------------------------------------------------------------- #
# IEM fetching (windowed multi-year, rate-limited)
# --------------------------------------------------------------------------- #
class IntradayModel:
    def __init__(self, session: aiohttp.ClientSession, settings: Settings) -> None:
        self._session = session
        self._settings = settings
        self._req_lock = asyncio.Lock()
        self._last_req = 0.0

    async def curve(self, icao: str, month: int, day: int, offset: int
                    ) -> dict[int, tuple[float, float, int]]:
        cfg = self._settings.forecast
        this_year = timeutils.utc_now().year
        all_obs: list[tuple[datetime, float]] = []
        for year in range(this_year - 1, this_year - 1 - cfg.climo_years, -1):
            try:
                center = date_cls(year, month, day)
            except ValueError:
                continue
            start = center - timedelta(days=cfg.climo_window_days)
            end = center + timedelta(days=cfg.climo_window_days)
            all_obs.extend(await self._fetch(icao, start, end))
        return build_curve(remaining_rise_samples(all_obs, offset), cfg.climo_min_samples)

    async def today_running_max(self, icao: str, offset: int
                                ) -> tuple[Optional[float], int, str]:
        """(running max so far, current local hour, climate_date) for today."""
        now = timeutils.utc_now()
        climate_date = timeutils.climate_date_str(now, offset)
        d1 = date_cls.fromisoformat(climate_date)
        obs = await self._fetch(icao, d1, d1 + timedelta(days=1))
        day = [(t, v) for t, v in obs if timeutils.climate_date_str(t, offset) == climate_date]
        running = max((v for _, v in day), default=None)
        return running, (now + timedelta(hours=offset)).hour, climate_date

    async def _fetch(self, icao: str, start: date_cls, end: date_cls
                     ) -> list[tuple[datetime, float]]:
        iem_id = icao[1:] if icao.startswith("K") else icao
        params = {
            "station": iem_id, "data": "tmpf", "tz": "Etc/UTC", "format": "onlycomma",
            "latlon": "no", "missing": "empty",
            "year1": start.year, "month1": start.month, "day1": start.day,
            "year2": end.year, "month2": end.month, "day2": end.day,
        }
        network = _IEM_NETWORKS.get(icao)
        if network:
            params["network"] = network
        text = await self._get(params)
        out: list[tuple[datetime, float]] = []
        for line in text.splitlines()[1:]:
            parts = line.split(",")
            if len(parts) < 3 or not parts[2].strip():
                continue
            try:
                ts = datetime.strptime(parts[1].strip(), "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc)
                out.append((ts, float(parts[2])))
            except ValueError:
                continue
        return out

    async def _get(self, params: dict) -> str:
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
            except (aiohttp.ClientError, asyncio.TimeoutError):
                await asyncio.sleep(1.5 * (2 ** attempt))
        return ""


# --------------------------------------------------------------------------- #
# Live engine wrapper: cached, non-blocking curves
# --------------------------------------------------------------------------- #
_BUILD_RETRY_SECONDS = 1800.0


class IntradayCurves:
    """Per-station remaining-rise curves for the live engine.

    Building one curve costs ``climo_years`` windowed archive fetches, measured at
    ~45s per station against the live IEM archive -- far too slow to sit inside an
    evaluation cycle. So a lookup **never waits**: it returns ``None``
    and kicks off a background build, and later cycles pick up the cached curve
    once it lands. Trading simply runs unsharpened until then.

    A build that fails or finds too little history is retried after
    ``_BUILD_RETRY_SECONDS`` rather than on every 2-minute cycle, so an IEM outage
    cannot turn into a request storm. Curves are keyed by calendar date (``MM-DD``)
    because the remaining-rise shape is seasonal, matching
    :class:`~weather_arb.climatology.ClimatologyClient`.

    ``wait_for_build=True`` makes lookups block on the build instead -- for
    one-shot CLIs (``signals``) that get a single pass and would otherwise always
    see ``None``.
    """

    def __init__(
        self,
        *,
        session: aiohttp.ClientSession,
        settings: Settings,
        wait_for_build: bool = False,
    ) -> None:
        self._model = IntradayModel(session, settings)
        self._settings = settings
        self._wait = wait_for_build
        self._curves: dict[tuple[str, str], dict[int, tuple[float, float, int]]] = {}
        self._builds: dict[tuple[str, str], asyncio.Task[None]] = {}
        self._failed_at: dict[tuple[str, str], float] = {}

    async def prediction(
        self,
        *,
        station: "StationConfig",
        climate_date: str,
        observed_tmax_f: Optional[float],
        now: Optional[datetime] = None,
    ) -> Optional[Prediction]:
        """Today's daily-max distribution given the running max, or ``None``.

        ``None`` means "no intraday information available" -- no observation yet,
        the curve is still building, or the market settles on a different day.
        Callers fall back to the static forecast.
        """
        if observed_tmax_f is None:
            return None
        offset = station.standard_utc_offset_hours
        now = now or timeutils.utc_now()
        # The curve is indexed by "how much of today is still to come", which says
        # nothing about a climate day that has not started (or has already ended).
        if timeutils.climate_date_str(now, offset) != climate_date:
            return None
        curve = await self._curve(station.icao, climate_date, offset)
        if not curve:
            return None
        local_hour = (now + timedelta(hours=offset)).hour
        return predict(
            curve, local_hour, observed_tmax_f,
            sigma_floor=self._settings.forecast.intraday_sigma_floor_f,
        )

    async def _curve(
        self, icao: str, climate_date: str, offset: int
    ) -> Optional[dict[int, tuple[float, float, int]]]:
        key = (icao, climate_date[5:])          # ("KNYC", "08-16")
        cached = self._curves.get(key)
        if cached is not None:
            return cached
        task = self._builds.get(key)
        if task is not None and not task.done():
            if self._wait:
                await asyncio.shield(task)
                return self._curves.get(key)
            return None
        failed = self._failed_at.get(key)
        if failed is not None and time.monotonic() - failed < _BUILD_RETRY_SECONDS:
            return None
        build = asyncio.create_task(self._build(key, icao, climate_date, offset))
        self._builds[key] = build
        if self._wait:
            await asyncio.shield(build)
            return self._curves.get(key)
        return None

    async def _build(
        self, key: tuple[str, str], icao: str, climate_date: str, offset: int
    ) -> None:
        """Fetch + fit one station/date curve. Never raises: a failure just means
        the engine keeps using the unsharpened forecast."""
        month, day = int(climate_date[5:7]), int(climate_date[8:10])
        try:
            curve = await self._model.curve(icao, month, day, offset)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - sharpening is an optimization, never fatal
            log.exception("intraday curve build failed",
                          extra={"context": {"icao": icao, "date": climate_date}})
            self._failed_at[key] = time.monotonic()
            return
        if not curve:
            log.warning("intraday curve has too little history; staying unsharpened",
                        extra={"context": {"icao": icao, "date": climate_date}})
            self._failed_at[key] = time.monotonic()
            return
        self._curves[key] = curve
        log.info(
            "intraday curve built",
            extra={"context": {
                "icao": icao, "date": climate_date, "hours": len(curve),
                # First LST hour whose climatological remaining rise is ~0, i.e.
                # when the day's max is typically already in.
                "max_in_by_hour": min((h for h in curve if curve[h][0] < 0.5), default=None),
            }},
        )


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
async def run(settings: Settings, cities: list[str], on_date: date_cls, live: bool) -> dict:
    out: dict[str, dict] = {}
    async with aiohttp.ClientSession(headers={"User-Agent": settings.http_user_agent}) as session:
        model = IntradayModel(session, settings)
        for city in cities:
            icao = CITY_ICAO.get(city)
            station = settings.station_by_icao(icao) if icao else None
            if icao is None or station is None:
                continue
            offset = station.standard_utc_offset_hours
            curve = await model.curve(icao, on_date.month, on_date.day, offset)
            entry: dict = {"icao": icao, "curve": {h: curve[h] for h in sorted(curve)}}
            if live:
                running, hour, cd = await model.today_running_max(icao, offset)
                entry["now"] = {"climate_date": cd, "local_hour": hour, "running_max_f": running}
                if running is not None:
                    p = predict(curve, hour, running)
                    entry["now"]["prediction"] = None if p is None else {
                        "predicted_max_f": round(p.predicted_max_f, 1),
                        "sigma_f": round(p.sigma_f, 1), "mean_rise_f": round(p.mean_rise_f, 1),
                    }
            out[city] = entry
    return out


def _render(result: dict, on_date: date_cls, live: bool) -> str:
    lines = ["=" * 74,
             f"  INTRADAY DAILY-MAX MODEL   remaining rise by local hour   ~{on_date:%b %d}",
             "=" * 74]
    for city, e in result.items():
        lines.append("")
        lines.append(f"{city} ({e['icao']}):")
        curve = e["curve"]
        if not curve:
            lines.append("    (not enough history)")
        else:
            for h in sorted(curve):
                mean, std, n = curve[h]
                bar = "#" * int(round(mean))
                lines.append(f"    {h:02d}:00 LST   +{mean:4.1f}F ± {std:3.1f}   (n={n:<4}) {bar}")
        now = e.get("now")
        if live and now:
            rm = now["running_max_f"]
            if rm is None:
                lines.append("    now: (no obs yet today)")
            else:
                p = now.get("prediction")
                base = f"    now {now['local_hour']:02d}:00 LST  running max {rm:.0f}F"
                lines.append(base if not p else
                             f"{base}  ->  daily max ~ {p['predicted_max_f']}F "
                             f"± {p['sigma_f']} (+{p['mean_rise_f']} to go)")
    lines.append("")
    lines.append("  Read: after peak the '+rise' collapses toward 0 -> the max is essentially in.")
    lines.append("=" * 74)
    return "\n".join(lines)


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Intraday daily-max model: remaining-rise climatology + live prediction."
    )
    parser.add_argument("--city", action="append", default=None, help="city key (repeatable)")
    parser.add_argument("--date", default=None, help="target date YYYY-MM-DD (default: today)")
    parser.add_argument("--now", action="store_true",
                        help="also fetch today's running max and predict the daily max live")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    settings = Settings.from_env()
    cities = [c.upper() for c in args.city] if args.city else list(CITY_ICAO)
    on_date = (datetime.strptime(args.date, "%Y-%m-%d").date() if args.date
               else datetime.now(timezone.utc).date())
    result = asyncio.run(run(settings, cities, on_date, args.now))

    if args.json:
        print(json.dumps(result, default=str, indent=2))
    else:
        print(_render(result, on_date, args.now))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
