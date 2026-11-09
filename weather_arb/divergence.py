"""NWS-CLI-vs-observed-max divergence detector.

.. note::

   **REGISTRY AND FRAMING CORRECTED 2026-09-08.**  Two rounds of correction have
   landed on this module; the current state is:

   * **The ORIGINAL framing is right.**  Kalshi settles on the **NWS CLI** daily
     high -- re-measured 2026-08-23 against Kalshi's own settled markets, 245/245
     = 100%.  The 2026-08-21 warning that lived here ("Kalshi settles on The
     Weather Company == the hourly-METAR max, so the basis is INVERTED") was
     itself the error: it took the max over TWC's *hourly observations* feed,
     which is the same hourly METAR data this repo already ingests, so it could
     only ever return "identical".  It is **retracted**.  ``nws_max_f`` is the
     Kalshi-relevant column; ``delta_f`` describes a real settlement gap again.
   * **Polymarket's basis is now OBSERVED**, and it is neither the CLI nor
     Weather Underground: it is the **NWS WRH "Hourly Data" timeseries** (routine
     METARs + SPECIs, MADIS sub-hourly rows excluded, each rounded half-up to a
     whole degree F), scored at 281/282 = 99.6% against Polymarket's own payouts
     by :mod:`weather_arb.polymarket_basis`.  Weather Underground was correct
     until ~2026-08-21, when the venue switched sources.
   * **The station table here was wrong, and is fixed below.**  Polymarket
     settles Denver at **KBKF (Buckley SFB)**, not KDEN -- in *both* eras -- and
     lists **no Philadelphia market at all**.  So the cities where Kalshi and
     Polymarket share a station are **MIA / AUS / LAX**, not the five this module
     used to claim.

.. warning::

   ``pm_max_f`` here is an **approximation of** Polymarket's basis, not a faithful
   reconstruction of it.  :meth:`DivergenceClient.fetch_metar` requests IEM's
   ``tmpf`` column, which is pre-rounded to a whole degree F with an unstated tie
   rule, and applies no ``report_type`` filter, so MADIS sub-hourly rows can enter
   the max -- and that row set scored only ~69% against Polymarket's payouts.
   :mod:`weather_arb.polymarket_basis` decodes the METAR ``T``-group directly and
   filters the rows the way the WRH page does; **use that module when the answer
   has to be right.**  This one is a fast monitor, and its ``pm_*`` fields should
   be read as indicative.

Premise (re-confirmed 2026-08-23):

Kalshi settles a city's daily high on the next-day **NWS Climate Report (CLI)**,
which is derived from the full 1-minute ASOS record and so captures brief
intra-hour temperature spikes. Polymarket settles on the max of an *observation
table* -- Weather Underground's daily rows until ~2026-08-21, the NWS WRH hourly
timeseries since -- which misses a spike that happens between reports. So for the
same city/day the two settlement bases can round to **different whole degrees**
and therefore different market buckets.

Your tracker already ingests high-frequency observations, so you can *see* the
NWS-basis max (1-minute) before the CLI is published and before traders who only
watch hourly reports realise it. This module quantifies that gap:

* ``nws_max_f``  -- rounded max of the **1-minute ASOS** series  (Kalshi basis)
* ``wu_max_f``   -- rounded max of the **hourly METAR** series   (observed-max basis
  at *Kalshi's* station; the historical name is kept for compatibility)
* ``delta_f``    -- ``nws_max_f - wu_max_f`` (>= 0; a positive value is the gap)
* ``pm_max_f``   -- the same, at *Polymarket's* station (see the warning above)
* ``crosses_2f_bin`` -- do the two land in different 2 F bins?

Two modes:

* **snapshot** (a single date) -- the running divergence per city today;
* **frequency** (``--days N``) -- *how often* the divergence actually occurs per
  city over the last N days, which is what decides whether the edge is real.

Station note (corrected 2026-09-08, OBSERVED from each Polymarket event's own
``resolutionSource``): Kalshi and Polymarket share a station only at **MIA / AUS
/ LAX**. They differ at **NYC** (KNYC vs KLGA), **CHI** (KMDW vs KORD) and
**DEN** (KDEN vs **KBKF**, Buckley SFB), where a station basis stacks on top of
the source basis. At **PHIL** Polymarket lists no market, so ``pm_*`` is ``None``
there rather than silently echoing the Kalshi station.

Data: IEM ASOS archive -- ``asos1min.py`` (1-minute) and ``asos.py`` (routine +
special METARs). Read-only; nothing is traded.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import time
from dataclasses import dataclass
from datetime import date as date_cls, datetime, timedelta, timezone
from typing import Optional

import aiohttp

from . import timeutils
from .alerts import AlertManager
from .config import Settings

_IEM_ASOS = "https://mesonet.agron.iastate.edu/cgi-bin/request/asos.py"
_IEM_1MIN = "https://mesonet.agron.iastate.edu/cgi-bin/request/asos1min.py"
# IEM requires the network= param for a bulk fetch to be reliable; a missing entry
# silently degrades the request.  KLGA and KBKF are Polymarket's stations and were
# absent until 2026-09-08, so every NYC cross-station fetch ran without one.
_IEM_NETWORKS = {
    "KMDW": "IL_ASOS", "KORD": "IL_ASOS", "KNYC": "NY_ASOS", "KMIA": "FL_ASOS",
    "KAUS": "TX_ASOS", "KDEN": "CO_ASOS", "KLAX": "CA_ASOS", "KPHL": "PA_ASOS",
    "KLGA": "NY_ASOS", "KBKF": "CO_ASOS",
}
_MIN_REQUEST_SPACING_S = 1.2

# City key -> Kalshi settlement ICAO (the station Kalshi's CLI settles against).
CITY_ICAO: dict[str, str] = {
    "NYC": "KNYC", "CHI": "KMDW", "MIA": "KMIA", "AUS": "KAUS",
    "DEN": "KDEN", "LAX": "KLAX", "PHIL": "KPHL",
}
# Cities where Kalshi and Polymarket share the station -> the divergence is purely
# a basis (source) effect.  OBSERVED 2026-09-08 from each Polymarket event's own
# resolutionSource, in both the WU and WRH eras.  DEN and PHIL used to be listed
# here and both were wrong; see PM_STATION and PM_NOT_LISTED.
SAME_STATION = frozenset({"MIA", "AUS", "LAX"})
# Where Polymarket settles on a DIFFERENT station than Kalshi (else same as CITY_ICAO).
# LaGuardia/O'Hare run warmer than Central Park/Midway, so the station effect can
# offset -- or even reverse -- the source effect there.  Denver is not a warmer/cooler
# variant of the same field at all: KBKF (Buckley SFB) is a separate airfield ~20mi
# from KDEN, and its resolutionSource named it in the WU era too, so the old KDEN
# entry was never correct rather than merely out of date.
PM_STATION = {"NYC": "KLGA", "CHI": "KORD", "DEN": "KBKF"}
# Cities Polymarket does not list.  Checked across both eras via the Gamma API
# (2026-07-22..2026-09-07): every Philadelphia slug returns no event.  Without this
# set, PM_STATION.get(city, icao) would quietly fall back to Kalshi's own station
# and report a 0 F cross-venue basis for a market that does not exist.
PM_NOT_LISTED = frozenset({"PHIL"})


# --------------------------------------------------------------------------- #
# Pure helpers
# --------------------------------------------------------------------------- #
def round_f(x: float) -> int:
    """Round to the nearest whole degree F, half up (how CLI/WU report)."""
    return math.floor(x + 0.5)


def bin_2f(tf: int) -> tuple[int, int]:
    """The Polymarket 2 F bin ``[even, even+1]`` containing whole-degree ``tf``."""
    lo = 2 * (tf // 2)
    return (lo, lo + 1)


def day_max_f(series: list[tuple[datetime, float]], climate_date: str, offset: int) -> Optional[float]:
    """Max temperature (F) among ``series`` obs that fall in the LST climate day."""
    best: Optional[float] = None
    for ts, tf in series:
        if timeutils.climate_date_str(ts, offset) == climate_date:
            if best is None or tf > best:
                best = tf
    return best


@dataclass(frozen=True, slots=True)
class Divergence:
    city: str
    icao: str
    date: str
    one_min_max_f: Optional[int]  # rounded 1-minute (ASOS) max   @ Kalshi station
    wu_max_f: Optional[int]       # rounded hourly-METAR max       @ Kalshi station
    n_1min: int
    n_metar: int
    same_station: bool
    # Polymarket's actual station (differs at NYC/CHI/DEN). ``pm_max_f`` is the
    # hourly-METAR max there; == ``wu_max_f`` when the station is shared, and both
    # are ``None`` when Polymarket lists no market for the city (PHIL).
    pm_icao: str = ""
    pm_max_f: Optional[int] = None
    pm_listed: bool = True

    @property
    def nws_max_f(self) -> Optional[int]:
        """Kalshi/CLI basis: never below WU (the CLI sees the METARs too), so we
        take the max of the 1-minute and METAR maxima. This also makes a gappy
        1-minute archive degrade to the METAR value instead of going negative."""
        vals = [v for v in (self.one_min_max_f, self.wu_max_f) if v is not None]
        return max(vals) if vals else None

    @property
    def delta_f(self) -> Optional[int]:
        if self.nws_max_f is None or self.wu_max_f is None:
            return None
        return self.nws_max_f - self.wu_max_f

    @property
    def diverges(self) -> bool:
        d = self.delta_f
        return d is not None and d >= 1

    @property
    def crosses_2f_bin(self) -> bool:
        if self.nws_max_f is None or self.wu_max_f is None:
            return False
        return bin_2f(self.nws_max_f) != bin_2f(self.wu_max_f)

    @property
    def pm_delta_f(self) -> Optional[int]:
        """Kalshi(CLI) minus Polymarket's *actual*-station WU max -- the true
        cross-venue basis. For NYC/CHI this folds in the station difference
        (LaGuardia/O'Hare vs Central Park/Midway) and can be negative when
        Polymarket's warmer station settles the higher bucket."""
        if self.nws_max_f is None or self.pm_max_f is None:
            return None
        return self.nws_max_f - self.pm_max_f

    @property
    def pm_diverges(self) -> bool:
        d = self.pm_delta_f
        return d is not None and d >= 1

    @property
    def cross_station(self) -> bool:
        return bool(self.pm_icao) and self.pm_icao != self.icao

    @property
    def one_min_ok(self) -> bool:
        """True if the 1-minute archive covered at least the METAR peak (i.e. it
        did not gap through the hot part of the day). When False the day's
        divergence is understated -- treat the estimate as a conservative floor."""
        if self.one_min_max_f is None:
            return False
        return self.wu_max_f is None or self.one_min_max_f >= self.wu_max_f


def classify(city: str, icao: str, climate_date: str,
             one_min: list[tuple[datetime, float]], metar: list[tuple[datetime, float]],
             offset: int, *, pm_icao: Optional[str] = None,
             pm_metar: Optional[list[tuple[datetime, float]]] = None) -> Divergence:
    """Build a :class:`Divergence` for one day.

    ``one_min``/``metar`` are at Kalshi's station. ``pm_metar`` (defaulting to
    ``metar``) is the hourly METAR at Polymarket's station -- pass LaGuardia /
    O'Hare / Buckley for NYC/CHI/DEN to capture the station basis; omit for the
    same-station cities (MIA/AUS/LAX).

    For a city in :data:`PM_NOT_LISTED` there is no Polymarket market to compare
    against, so ``pm_icao`` is blank and ``pm_max_f`` is ``None``.  Reporting
    Kalshi's own station there would fabricate a 0 F cross-venue basis."""
    listed = city not in PM_NOT_LISTED
    if not listed:
        pm_icao, pm_series = "", []
    else:
        pm_icao = pm_icao or icao
        pm_series = pm_metar if pm_metar is not None else metar
    day_1m = [(t, v) for t, v in one_min if timeutils.climate_date_str(t, offset) == climate_date]
    day_mt = [(t, v) for t, v in metar if timeutils.climate_date_str(t, offset) == climate_date]
    nws = day_max_f(day_1m, climate_date, offset)
    wu = day_max_f(day_mt, climate_date, offset)
    pm = day_max_f(pm_series, climate_date, offset)
    return Divergence(
        city=city, icao=icao, date=climate_date,
        one_min_max_f=round_f(nws) if nws is not None else None,
        wu_max_f=round_f(wu) if wu is not None else None,
        n_1min=len(day_1m), n_metar=len(day_mt),
        same_station=city in SAME_STATION,
        pm_icao=pm_icao,
        pm_max_f=round_f(pm) if pm is not None else None,
        pm_listed=listed,
    )


# --------------------------------------------------------------------------- #
# IEM fetching (defensive, rate-limited)
# --------------------------------------------------------------------------- #
class DivergenceClient:
    def __init__(self, session: aiohttp.ClientSession, settings: Settings) -> None:
        self._session = session
        self._settings = settings
        self._req_lock = asyncio.Lock()
        self._last_req = 0.0

    async def _get(self, url: str, params: dict) -> str:
        for attempt in range(4):
            async with self._req_lock:
                wait = _MIN_REQUEST_SPACING_S - (time.monotonic() - self._last_req)
                if wait > 0:
                    await asyncio.sleep(wait)
                self._last_req = time.monotonic()
            try:
                async with self._session.get(url, params=params) as resp:
                    if resp.status == 429 or resp.status >= 500:
                        await asyncio.sleep(min(8.0, 1.5 * (2 ** attempt)))
                        continue
                    if resp.status >= 400:
                        return ""
                    return await resp.text()
            except (aiohttp.ClientError, asyncio.TimeoutError):
                await asyncio.sleep(1.5 * (2 ** attempt))
        return ""

    async def fetch_metar(self, icao: str, d1: date_cls, d2: date_cls) -> list[tuple[datetime, float]]:
        iem_id = icao[1:] if icao.startswith("K") else icao
        params = {
            "station": iem_id, "data": "tmpf", "tz": "Etc/UTC", "format": "onlycomma",
            "latlon": "no", "missing": "empty",
            "year1": d1.year, "month1": d1.month, "day1": d1.day,
            "year2": d2.year, "month2": d2.month, "day2": d2.day,
        }
        network = _IEM_NETWORKS.get(icao)
        if network:
            params["network"] = network
        return _parse_iem(await self._get(_IEM_ASOS, params), ts_col=1, val_col=2)

    async def fetch_1min(self, icao: str, d1: date_cls, d2: date_cls) -> list[tuple[datetime, float]]:
        iem_id = icao[1:] if icao.startswith("K") else icao
        params = {
            "station": iem_id, "tz": "UTC", "vars": "tmpf", "sample": "1min",
            "what": "download", "delim": "comma", "gis": "no",
            "year1": d1.year, "month1": d1.month, "day1": d1.day,
            "year2": d2.year, "month2": d2.month, "day2": d2.day,
        }
        return _parse_iem(await self._get(_IEM_1MIN, params), ts_col=2, val_col=3)

    async def analyze(self, city: str, icao: str, offset: int,
                      start: date_cls, n_days: int) -> list[Divergence]:
        """Divergence for each of ``n_days`` climate days starting at ``start``."""
        hi = start + timedelta(days=n_days) + timedelta(days=1)
        one_min, metar = await asyncio.gather(
            self.fetch_1min(icao, start, hi),
            self.fetch_metar(icao, start, hi),
        )
        pm_icao = "" if city in PM_NOT_LISTED else PM_STATION.get(city, icao)
        # Only fetch a second station when Polymarket actually differs (NYC/CHI/DEN),
        # and not at all when it lists no market for the city (PHIL).
        pm_metar = (await self.fetch_metar(pm_icao, start, hi)
                    if pm_icao and pm_icao != icao else metar)
        return [
            classify(city, icao, (start + timedelta(days=i)).isoformat(), one_min, metar, offset,
                     pm_icao=pm_icao, pm_metar=pm_metar)
            for i in range(n_days)
        ]


def _parse_iem(text: str, ts_col: int, val_col: int) -> list[tuple[datetime, float]]:
    out: list[tuple[datetime, float]] = []
    for line in text.splitlines()[1:]:
        parts = line.split(",")
        if len(parts) <= max(ts_col, val_col):
            continue
        raw_ts, raw_val = parts[ts_col].strip(), parts[val_col].strip()
        if not raw_val or raw_val.upper() == "M":
            continue
        try:
            ts = datetime.strptime(raw_ts, "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc)
            out.append((ts, float(raw_val)))
        except ValueError:
            continue
    return out


def alert_events(prev: Optional[Divergence], curr: Divergence) -> list[str]:
    """State-change alerts for the live detector. Fires only when a condition
    *newly appears* (vs the prior cycle), so a running watch never spams: a
    persistent divergence alerts once, not every cycle."""
    if not curr.diverges:
        return []
    events: list[str] = []
    if prev is None or not prev.diverges:
        events.append(f"divergence appeared: NWS {curr.nws_max_f}F vs hourly {curr.wu_max_f}F "
                      f"(+{curr.delta_f}F) — the higher Kalshi bucket is underpriced")
    if curr.crosses_2f_bin and not (prev is not None and prev.crosses_2f_bin):
        events.append(f"2F bin flip: Kalshi bucket {bin_2f(curr.nws_max_f)} vs "
                      f"Polymarket {bin_2f(curr.wu_max_f)}")
    return events


# --------------------------------------------------------------------------- #
# Aggregation + orchestration
# --------------------------------------------------------------------------- #
@dataclass(frozen=True, slots=True)
class CityStats:
    city: str
    icao: str
    days: int
    n_diverge: int
    n_cross_bin: int
    mean_delta_on_diverge: float
    max_delta: int
    n_unreliable: int          # days the 1-minute archive gapped through the peak
    same_station: bool


def summarize(results: list[Divergence]) -> Optional[CityStats]:
    graded = [r for r in results if r.delta_f is not None]
    if not graded:
        return None
    div = [r for r in graded if r.diverges]
    deltas = [r.delta_f for r in div]  # type: ignore[misc]
    return CityStats(
        city=graded[0].city, icao=graded[0].icao, days=len(graded),
        n_diverge=len(div), n_cross_bin=sum(1 for r in graded if r.crosses_2f_bin),
        mean_delta_on_diverge=(sum(deltas) / len(deltas)) if deltas else 0.0,
        max_delta=max((r.delta_f for r in graded), default=0),  # type: ignore[misc]
        n_unreliable=sum(1 for r in graded if not r.one_min_ok),
        same_station=graded[0].same_station,
    )


def _resolve(settings: Settings, cities: list[str]) -> list[tuple[str, str, int]]:
    """(city, icao, standard_offset) for cities with a known Kalshi station."""
    out: list[tuple[str, str, int]] = []
    for city in cities:
        icao = CITY_ICAO.get(city)
        station = settings.station_by_icao(icao) if icao else None
        if icao and station:
            out.append((city, icao, station.standard_utc_offset_hours))
    return out


async def run(settings: Settings, cities: list[str], start: date_cls, n_days: int
              ) -> dict[str, list[Divergence]]:
    async with aiohttp.ClientSession(headers={"User-Agent": settings.http_user_agent}) as session:
        client = DivergenceClient(session, settings)
        out: dict[str, list[Divergence]] = {}
        for city, icao, offset in _resolve(settings, cities):
            out[city] = await client.analyze(city, icao, offset, start, n_days)
        return out


def _watch_line(now: str, r: Divergence, events: list[str]) -> str:
    if r.delta_f is None:
        return f"[{now}] {r.city:<5} (no data yet)"
    tags = " DIVERGES" if r.diverges else ""
    if r.crosses_2f_bin:
        tags += " [bin-flip]"
    if not r.one_min_ok:
        tags += " (1-min floor)"
    ev = f"   -> {'; '.join(events)}" if events else ""
    return f"[{now}] {r.city:<5} {r.icao} nws={r.nws_max_f}F wu={r.wu_max_f}F {r.delta_f:+d}F{tags}{ev}"


async def watch(settings: Settings, cities: list[str], interval_s: float, cycles: int = 0) -> None:
    """Live detector: each cycle recompute today's running divergence per city and
    alert (ntfy + alerts.log) when a divergence or bin-flip newly appears. Runs
    until interrupted, or ``cycles`` passes if > 0.

    Note: the IEM 1-minute archive lags real time by up to ~an hour, so this
    detects a divergence with that latency -- still well before the next-day CLI.
    """
    alerter = AlertManager(settings)
    resolved = _resolve(settings, cities)
    async with aiohttp.ClientSession(headers={"User-Agent": settings.http_user_agent}) as session:
        client = DivergenceClient(session, settings)
        state: dict[str, Divergence] = {}
        n = 0
        while True:
            now = timeutils.utc_now().strftime("%H:%M UTC")
            for city, icao, offset in resolved:
                today = date_cls.fromisoformat(timeutils.climate_date_str(timeutils.utc_now(), offset))
                rows = await client.analyze(city, icao, offset, today, 1)
                curr = rows[0]
                prev = state.get(city)
                if prev is not None and prev.date != curr.date:  # new climate day -> reset
                    prev = None
                events = alert_events(prev, curr)
                state[city] = curr
                print(_watch_line(now, curr, events), flush=True)
                for ev in events:
                    alerter.send(f"📈 {city} temp divergence", f"{city} {curr.date}: {ev}")
            n += 1
            if cycles and n >= cycles:
                break
            await asyncio.sleep(interval_s)


# --------------------------------------------------------------------------- #
# Rendering / CLI
# --------------------------------------------------------------------------- #
def _render_snapshot(results: dict[str, list[Divergence]], on_date: str) -> str:
    out = ["=" * 74,
           f"  NWS-vs-WU SETTLEMENT DIVERGENCE     date={on_date}",
           "  nws_max (Kalshi/1-min) vs wu_max (Polymarket/hourly METAR)",
           "=" * 74, ""]
    for city, rows in results.items():
        r = rows[0] if rows else None
        if r is None or r.delta_f is None:
            out.append(f"  {city:<5} (no data yet)")
            continue
        star = " <== DIVERGES" + ("  [crosses 2F bin]" if r.crosses_2f_bin else "") if r.diverges else ""
        quality = "" if r.one_min_ok else "  (1-min gappy: floor)"
        out.append(f"  {city:<5} {r.icao}  nws={r.nws_max_f}F  wu={r.wu_max_f}F  "
                   f"delta={r.delta_f:+d}F{star}{quality}")
        if not r.pm_listed:
            out.append("        vs Polymarket: no market listed for this city")
        elif r.cross_station and r.pm_delta_f is not None:
            note = (" <== Kalshi settles higher" if r.pm_diverges else
                    "  (Polymarket's station settles higher)" if r.pm_delta_f < 0 else "")
            out.append(f"        vs Polymarket @{r.pm_icao}: pm={r.pm_max_f}F  "
                       f"cross-venue delta={r.pm_delta_f:+d}F{note}")
    out.append("")
    out.append("-" * 74)
    out.append("  delta>0 => NWS (Kalshi) settles a higher whole degree than hourly METAR")
    out.append("  saw it -- so the higher Kalshi bucket is underpriced by the hourly-watching crowd.")
    out.append("=" * 74)
    return "\n".join(out)


def _render_frequency(results: dict[str, list[Divergence]], start: str, n_days: int) -> str:
    out = ["=" * 78,
           f"  DIVERGENCE FREQUENCY -- how real is the edge?   {n_days} days from {start}",
           "=" * 78, "",
           f"  {'city':<6}{'station':<8}{'days':>5}{'diverge':>9}{'cross-bin':>11}"
           f"{'mean dF':>9}{'max dF':>8}",
           "  " + "-" * 74]
    total_days = total_div = total_cross = 0
    for city, rows in results.items():
        s = summarize(rows)
        if s is None:
            out.append(f"  {city:<6}{'-':<8}{'(no data)':>25}")
            continue
        pct = (100.0 * s.n_diverge / s.days) if s.days else 0.0
        flag = "" if s.same_station else " *"
        if s.n_unreliable:
            flag += f" ({s.n_unreliable} gappy)"
        out.append(f"  {city:<6}{s.icao:<8}{s.days:>5}{s.n_diverge:>6} ({pct:>3.0f}%)"
                   f"{s.n_cross_bin:>11}{s.mean_delta_on_diverge:>9.1f}{s.max_delta:>8}{flag}")
        total_days += s.days
        total_div += s.n_diverge
        total_cross += s.n_cross_bin
    out.append("  " + "-" * 74)
    pct = (100.0 * total_div / total_days) if total_days else 0.0
    out.append(f"  {'ALL':<6}{'':<8}{total_days:>5}{total_div:>6} ({pct:>3.0f}%){total_cross:>11}")
    out.append("")
    out.append("  diverge = NWS whole-degree max > hourly-METAR max (the edge fires that day).")
    out.append("  cross-bin = the two land in different Polymarket 2F bins (a bucket flip).")
    out.append("  gappy = days the 1-min archive missed the peak -> those diverge counts are a floor.")
    out.append("  * = NYC/CHI/DEN: this table is same-station CLI-vs-hourly; Polymarket's real")
    out.append("      station (KLGA/KORD/KBKF) basis shows in snapshot / --json as pm_delta,")
    out.append("      and can go either way.  PHIL is starred too: Polymarket lists no market.")
    out.append("=" * 78)
    return "\n".join(out)


def _parse_date(raw: Optional[str]) -> date_cls:
    if raw:
        return datetime.strptime(raw, "%Y-%m-%d").date()
    # Default to yesterday (today's 1-minute archive is usually incomplete).
    return (datetime.now(timezone.utc) - timedelta(days=1)).date()


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Detect NWS-vs-Weather-Underground settlement divergence (read-only)."
    )
    parser.add_argument("--date", default=None, help="climate day YYYY-MM-DD (default: yesterday UTC)")
    parser.add_argument("--city", action="append", default=None, help="city key (repeatable)")
    parser.add_argument("--days", type=int, default=1,
                        help="N>1 => frequency table over the N days ending at --date")
    parser.add_argument("--watch", action="store_true",
                        help="live detector: poll today's running divergence and alert on changes")
    parser.add_argument("--interval", type=float, default=600.0,
                        help="--watch poll interval seconds (default 600; 1-min data lags anyway)")
    parser.add_argument("--cycles", type=int, default=0,
                        help="--watch: stop after N cycles (0 = run until interrupted)")
    parser.add_argument("--json", action="store_true", help="emit JSON")
    args = parser.parse_args(argv)

    settings = Settings.from_env()
    cities = [c.upper() for c in args.city] if args.city else list(CITY_ICAO)

    if args.watch:
        try:
            asyncio.run(watch(settings, cities, args.interval, max(0, args.cycles)))
        except KeyboardInterrupt:
            print("\nstopped.")
        return 0

    end = _parse_date(args.date)
    n_days = max(1, args.days)
    start = end - timedelta(days=n_days - 1)

    results = asyncio.run(run(settings, cities, start, n_days))

    if args.json:
        print(json.dumps({
            "start": start.isoformat(), "days": n_days,
            "results": {city: [
                {"date": r.date, "nws_max_f": r.nws_max_f, "wu_max_f": r.wu_max_f,
                 "one_min_max_f": r.one_min_max_f, "delta_f": r.delta_f,
                 "diverges": r.diverges, "crosses_2f_bin": r.crosses_2f_bin,
                 "one_min_ok": r.one_min_ok, "same_station": r.same_station,
                 "pm_icao": r.pm_icao, "pm_max_f": r.pm_max_f,
                 "pm_delta_f": r.pm_delta_f, "cross_station": r.cross_station,
                 "pm_listed": r.pm_listed,
                 "n_1min": r.n_1min, "n_metar": r.n_metar}
                for r in rows] for city, rows in results.items()},
        }, indent=2))
    elif n_days == 1:
        print(_render_snapshot(results, start.isoformat()))
    else:
        print(_render_frequency(results, start.isoformat(), n_days))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
