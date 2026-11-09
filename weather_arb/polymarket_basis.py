"""Measure which basis Polymarket *actually* settles its daily-high markets on.

Companion to :mod:`weather_arb.settlement_basis`, which established that Kalshi
settles on the NWS CLI daily high (245/245).  The cross-venue basis trade needs
the *other* leg pinned down with the same rigour, and it has never been measured
-- ``cross_venue.STATION_MAP`` is a guess, and a wrong one.

**The trap this module exists to avoid.**  An earlier settlement check in this
repo was circular: it took the max over the same observation feed already being
ingested, so it could only ever return "identical".  Here the ground truth is
Polymarket's **own resolved outcomes** -- which bucket actually paid out.  A
candidate basis is scored by whether the number it produces lands in the bucket
Polymarket paid.  No candidate is ever compared against another API's idea of
the max.

**What the venue says it does.**  Every market's ``description`` states the rule
verbatim.  Two eras exist in the available history and they cite *different*
sources, so they are scored separately rather than pooled:

* ``wu``  -- ``wunderground.com/history/daily/...`` (2026-07-22 .. ~2026-08-21)
* ``wrh`` -- ``weather.gov/wrh/timeseries?site=<icao>`` (~2026-08-23 onward)

The ``wrh`` text adds two constraints that a naive reading misses::

    "the highest reading under the 'Temp' column for all times on this day"
    "This market will resolve off of the Hourly Data provided using the
     'Show Hourly Data' button."
    "measures temperatures to whole degrees Fahrenheit"

and the page's own source (``weather.gov/source/wrh/timeseries/obs.js``) pins
down what those mean for an ASOS site:

* the feed is Synoptic ``v2/stations/timeseries`` with ``obtimezone=local``
  -- so "this day" is the **DST-aware civil day**, not the LST climate day;
* "Hourly Data" keeps rows with a non-null sea-level pressure **or** a METAR
  text beginning with the site id -- i.e. routine METARs *plus* SPECIs.  It is
  not a top-of-hour filter, and it excludes the sub-hourly MADIS/HFMETAR rows;
* the Temp column is ``Math.round(air_temp_F)`` -- half-up to a whole degree.

**Why IEM and not Synoptic.**  Synoptic is the true upstream, and the IEM ASOS
archive holds the same METAR and SPECI reports without a rate limit, which is what
carries a 48-day backfill.

⚠ **The ``"Invalid request per token rules"`` refusal is NOT a quota** -- an
earlier note here recorded it as one and that was wrong.  It is a missing header:
the weather.gov token needs **both** ``Referer`` *and* ``Origin`` (see
:func:`wrh_headers`).  Without ``Origin`` every request is refused, including the
``recent=60`` call the live page itself makes, which is what makes the refusal
look like an exhausted allowance.  Check the headers before concluding otherwise.
``--validate-wrh`` / ``--validate-target`` spot-check the reconstruction against
live Synoptic on chosen station-days.  That check tests the *plumbing*; the settled
outcomes test the *basis*.  Neither is circular on its own axis.

Temperatures are decoded from the raw METAR ``T``-group (tenths of a degree C)
rather than read from IEM's ``tmpf`` column, which is pre-rounded to a whole
degree F with an unstated tie rule -- and exact halves do occur (22.5 C is
exactly 72.5 F), which is precisely where half-up and banker's rounding split.

⚠ **The resolution source does not always read the T-group either, and that is
what the single miss turned out to be.**  The feed sometimes serves the
whole-degree *body* group (e.g. ``32/25``) instead of the tenths-precision
``T``-group (``T03170250``), which shifts a reading by up to 0.9 F -- enough to
cross a whole degree.  ``encoding_sensitivity`` in the output bounds how often
that could move a city-day.  It is a **settlement risk of this venue**, not a
defect in this module: the venue pays on what its feed serves.

MEASURED against the live feed on 2026-09-08, 19 station-days across all six
stations: the fallback fires on **8 of 499** official rows (1.6%), touches
**5 of 18** station-days, and **moved the daily max on 1 of 19**.  That one is
the miss below.  The ``encoding_sensitivity`` ceiling (~46% of city-days exposed)
therefore overstates the real rate by roughly an order of magnitude -- it assumes
the fallback lands on the max-setting row every time.

⚠ **The trigger is UNKNOWN.**  An earlier note here proposed that Synoptic's copy
of the METAR carries a *double space* before the T-group and its decoder misses
the group.  That does not survive: across 931 KMIA rows all 3 fallbacks contained
irregular whitespace, but so did **73 rows**, and 70 of those decoded the T-group
correctly.  Irregular whitespace looks necessary in this sample and is plainly not
sufficient.  Do not repeat the double-space explanation as if it were established.

**THE ONE MISS -- CONFIRMED 2026-09-08, and it is ours, not the basis's.**
MIA 2026-09-02: our reconstruction says 89, Polymarket paid 90-91.  Pulled the
venue's own named source for that day and the WRH Temp column maxes at **90**,
inside the paid bucket.  The decisive row is the 13:45 EDT SPECI, where the body
says 32 C -> 89.6 -> **90** and the T-group says 31.7 C -> 89.06 -> **89**; the
feed served 89.6.  Row *selection* was identical (30 rows both sides), so this is
a decoding difference between our IEM-based proxy and the feed, not a wrong basis.
Reproduce::

    python -m weather_arb.polymarket_basis --validate-target KMIA:2026-09-02

⚠ Scope of that confirmation: it re-scores **one** day against the live feed.  The
other 287 city-days were scored on the reconstruction and are NOT re-validated by
it, and the same decoding gap is bidirectional -- it could in principle have turned
a miss into a hit elsewhere.  The headline remains 287/288 as reconstructed.

**Pre-registered gate** (fixed before the first run, not tuned to the result):
the winning candidate must reproduce >= 95% of resolved WRH-era markets pooled,
and must beat every other candidate.  Otherwise the basis is NOT identified and
the divergence figure below is not computed.

⚠ **The tie-break half of that gate was REFINED AFTER the first run, and the
change flipped FAIL to PASS -- read this before trusting the verdict.**  As first
written the gate required the winner to beat *every* other candidate.  P1 and P2
differ only in the day window, and they returned the identical value on
**282 of 282** city-days (a daily maximum simply never falls in the one hour the
two conventions disagree about), so they tied and the gate reported "basis NOT
identified".  That is the wrong reading: the row set, the encoding and the source
were all discriminated decisively.  A candidate differing only on the day window
is therefore no longer counted as a rival; it is reported as an **unresolved
axis** instead, and the verdict line says so.  The 95% threshold was NOT touched.
Nothing here makes the day-window question answered -- it makes it *visible*.

Read-only -- no trading, no schema change, no writes to the ledger.

    python -m weather_arb.polymarket_basis [--days N] [--json] [--cache-dir DIR]
                                           [--validate-wrh N]
                                           [--validate-target STID:DATE]
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import hashlib
import io
import json
import math
import os
import re
import statistics
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import date as date_cls, datetime, timedelta, timezone
from typing import Any, Optional
from zoneinfo import ZoneInfo

import aiohttp

from .config import Settings
from .settlement_basis import SERIES_ICAO, event_date, fetch_cli_highs, fetch_settled, settled_range
from . import timeutils

_GAMMA_EVENTS = "https://gamma-api.polymarket.com/events"
_IEM_ASOS = "https://mesonet.agron.iastate.edu/cgi-bin/request/asos.py"
_SYNOPTIC = "https://api.synopticdata.com/v2/stations/timeseries"
# Published in the clear at https://www.weather.gov/source/wrh/apiKey.js -- this is
# the token the resolution source's own page uses.  Quota-limited; validation only.
_WRH_TOKEN = "7c76618b66c74aee913bdbae4b448bdd"
_WRH_REFERER = "https://www.weather.gov/wrh/timeseries"
_WRH_ORIGIN = "https://www.weather.gov"


def wrh_headers(icao: str) -> dict[str, str]:
    """Headers Synoptic requires before it will honour the weather.gov token.

    ⚠ **Both** are load-bearing, and the failure mode is misleading.  With only a
    ``Referer`` every request -- including ``recent=60``, the one the live page
    itself makes -- comes back ``{"RESPONSE_CODE": 2, "RESPONSE_MESSAGE":
    "Invalid request per token rules"}``.  That message reads like an exhausted
    quota and was recorded as one on 2026-09-08; it is not.  Adding ``Origin``
    makes the identical request succeed.  If this ever refuses again, check the
    headers before concluding the token is spent.
    """
    return {"Referer": f"{_WRH_REFERER}?site={icao.lower()}", "Origin": _WRH_ORIGIN}

_MONTHS = ["january", "february", "march", "april", "may", "june", "july",
           "august", "september", "october", "november", "december"]

# City -> Polymarket slug fragment.  Philadelphia is not listed by Polymarket;
# Los Angeles IS (an earlier note in this repo said otherwise).
CITY_POLY_SLUG: dict[str, str] = {
    "NYC": "nyc", "CHI": "chicago", "MIA": "miami",
    "AUS": "austin", "DEN": "denver", "LAX": "los-angeles",
}
CITY_KALSHI_SERIES: dict[str, str] = {
    "NYC": "KXHIGHNY", "CHI": "KXHIGHCHI", "MIA": "KXHIGHMIA",
    "AUS": "KXHIGHAUS", "DEN": "KXHIGHDEN", "LAX": "KXHIGHLAX",
}
# ICAO -> (IEM network, IANA zone).  Nothing here is assumed about *which* city a
# station belongs to; the station is read off Polymarket's own resolutionSource and
# looked up here, and an unknown station is a hard error rather than a guess.
STATION_META: dict[str, tuple[str, str]] = {
    "KLGA": ("NY_ASOS", "America/New_York"),
    "KORD": ("IL_ASOS", "America/Chicago"),
    "KMIA": ("FL_ASOS", "America/New_York"),
    "KAUS": ("TX_ASOS", "America/Chicago"),
    "KBKF": ("CO_ASOS", "America/Denver"),
    "KLAX": ("CA_ASOS", "America/Los_Angeles"),
    "KNYC": ("NY_ASOS", "America/New_York"),
    "KMDW": ("IL_ASOS", "America/Chicago"),
    "KDEN": ("CO_ASOS", "America/Denver"),
    "KPHL": ("PA_ASOS", "America/New_York"),
}

GATE_MIN_RATE = 0.95        # pre-registered, see module docstring


# --------------------------------------------------------------------------- #
# Pure helpers
# --------------------------------------------------------------------------- #
def round_half_up(x: float) -> int:
    """Round to a whole degree, half away from zero-ward +inf.

    Identical to JavaScript ``Math.round``, which is what the WRH page applies to
    the Temp column.  Python's ``round`` is banker's rounding and would disagree
    at exact halves -- and exact halves are reachable (22.5 C is exactly 72.5 F).
    """
    return math.floor(x + 0.5)


def slug_for(city_frag: str, day: date_cls) -> str:
    return f"highest-temperature-in-{city_frag}-on-{_MONTHS[day.month - 1]}-{day.day}-{day.year}"


_LABEL_RANGE = re.compile(r"^\s*(-?\d+)\s*-\s*(-?\d+)\s*°?F", re.I)
_LABEL_BELOW = re.compile(r"^\s*(-?\d+)\s*°?F\s*or\s*below", re.I)
_LABEL_ABOVE = re.compile(r"^\s*(-?\d+)\s*°?F\s*or\s*(higher|above)", re.I)


def parse_bucket_label(label: str) -> Optional[tuple[Optional[int], Optional[int]]]:
    """``"88-89°F"`` -> ``(88, 89)``; tails -> an open end.  ``None`` if unparsed."""
    m = _LABEL_RANGE.match(label)
    if m:
        return int(m.group(1)), int(m.group(2))
    m = _LABEL_BELOW.match(label)
    if m:
        return None, int(m.group(1))
    m = _LABEL_ABOVE.match(label)
    if m:
        return int(m.group(1)), None
    return None


def buckets_tile_integers(buckets: list[tuple[Optional[int], Optional[int]]]) -> bool:
    """Do the labels partition the integers exactly once, with no gap or overlap?"""
    lows = [b for b in buckets if b[0] is None]
    highs = [b for b in buckets if b[1] is None]
    mids = sorted((b[0], b[1]) for b in buckets if b[0] is not None and b[1] is not None)
    if len(lows) != 1 or len(highs) != 1 or not mids:
        return False
    if mids[0][0] != lows[0][1] + 1 or mids[-1][1] != highs[0][0] - 1:
        return False
    return all(mids[i + 1][0] == mids[i][1] + 1 for i in range(len(mids) - 1))


def in_bucket(value: Optional[int], lo: Optional[int], hi: Optional[int]) -> bool:
    if value is None:
        return False
    return (lo is None or value >= lo) and (hi is None or value <= hi)


_T_GROUP = re.compile(r"\bT([01])(\d{3})([01])(\d{3})\b")
_BODY_TEMP = re.compile(r"(?<=\s)(M?\d{2})/(M?\d{2})(?=\s|$)")


def metar_temp_f(metar: str) -> tuple[Optional[float], str]:
    """Exact air temperature in degF from a raw METAR, plus which group supplied it.

    Prefers the ``RMK Txxxxxxxx`` group (tenths of a degree C) -- that is what
    Synoptic, and therefore the WRH Temp column, reports.  Falls back to the
    whole-degree body group, and reports which was used so the caller can count
    how often the sharper source was missing.
    """
    m = _T_GROUP.search(metar)
    if m:
        c = int(m.group(2)) / 10.0
        if m.group(1) == "1":
            c = -c
        return c * 9.0 / 5.0 + 32.0, "t_group"
    body = metar.split(" RMK", 1)[0]
    found = _BODY_TEMP.findall(body)
    if found:
        raw = found[-1][0]
        c = -float(raw[1:]) if raw.startswith("M") else float(raw)
        return c * 9.0 / 5.0 + 32.0, "body"
    return None, "missing"


def metar_body_temp_f(metar: str) -> Optional[float]:
    """Air temperature from the METAR *body* group only (whole degrees C).

    The body carries whole degrees; the ``RMK Txxxxxxxx`` group carries tenths.
    They routinely disagree by up to half a degree C, which is up to 0.9 degF --
    enough to cross a whole-degF boundary and land in a different bucket.  On
    KMIA 2026-09-02 the body said ``32/24`` (32.0 C = 89.6 F -> 90) while the
    T-group said ``T03170239`` (31.7 C = 89.06 F -> 89), and the market paid the
    90-91 bucket.  Which encoding the venue reads is therefore a real candidate
    axis, not a detail, so it is scored rather than assumed.
    """
    body = metar.split(" RMK", 1)[0]
    found = _BODY_TEMP.findall(body)
    if not found:
        return None
    raw = found[-1][0]
    c = -float(raw[1:]) if raw.startswith("M") else float(raw)
    return c * 9.0 / 5.0 + 32.0


def is_hourly_row(metar: str, icao: str) -> bool:
    """Does this observation appear in the WRH "Hourly Data" table?

    Mirrors ``obs.js``: for an ASOS network it keeps rows carrying a sea-level
    pressure (a routine METAR -- the ``SLPnnn`` remark) or whose METAR text begins
    with the site id (a SPECI).  The sub-hourly MADIS rows are excluded, and the
    two feeds mark them differently: Synoptic prefixes them ``"METAR "`` (which is
    why ``startsWith(SITE)`` rejects them on the page), while IEM leaves the id
    bare and appends a ``MADISHF`` remark.  Both spellings are handled so the same
    predicate can be applied to either dialect.

    The authoritative row set here is IEM's ``report_type`` filter; this is an
    independent cross-check that would catch that filter changing under us.
    """
    upper = metar.upper().strip()
    if "MADISHF" in upper or upper.startswith("METAR "):
        return False
    return " SLP" in upper or upper.startswith(icao.upper())


def is_routine(metar: str) -> bool:
    """Routine hourly METAR (carries the ``SLPnnn`` sea-level-pressure remark)."""
    return " SLP" in metar.upper()


def wilson_interval(hits: int, n: int, z: float = 1.959963985) -> tuple[float, float]:
    """95% Wilson score interval for a binomial rate.

    Reported for scale only.  Station-days are *not* independent -- weather is
    autocorrelated and the six cities share synoptic regimes -- so the true
    interval is wider than this.  See the caveats in ``_render``.
    """
    if n == 0:
        return 0.0, 0.0
    p = hits / n
    d = 1.0 + z * z / n
    centre = (p + z * z / (2 * n)) / d
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return max(0.0, centre - half), min(1.0, centre + half)


# --------------------------------------------------------------------------- #
# Fetching
# --------------------------------------------------------------------------- #
class Fetcher:
    """Throttled HTTP with an optional on-disk cache of raw payloads."""

    def __init__(self, session: aiohttp.ClientSession, cache_dir: Optional[str],
                 user_agent: str) -> None:
        self.session = session
        self.cache_dir = cache_dir
        self.user_agent = user_agent
        if cache_dir:
            os.makedirs(cache_dir, exist_ok=True)

    def _cache_path(self, key: str) -> Optional[str]:
        if not self.cache_dir:
            return None
        return os.path.join(self.cache_dir, hashlib.sha256(key.encode()).hexdigest() + ".cache")

    async def get_text(self, url: str, params: Optional[list[tuple[str, str]]] = None,
                       headers: Optional[dict[str, str]] = None,
                       pause: float = 0.25, retries: int = 3,
                       keep_error_body: bool = False) -> Optional[str]:
        """Fetch a URL, or ``None``.

        ``keep_error_body`` returns a 4xx body instead of discarding it -- an API
        that explains itself in the body of a refusal ("Invalid request per token
        rules") must not be reported as a bare connection failure, because the two
        call for opposite responses.  Error bodies are never cached.
        """
        qs = "&".join(f"{k}={v}" for k, v in (params or []))
        key = f"{url}?{qs}"
        path = self._cache_path(key)
        if path and os.path.exists(path):
            with open(path, "r", encoding="utf-8") as fh:
                return fh.read() or None
        hdrs = {"User-Agent": self.user_agent}
        hdrs.update(headers or {})
        body: Optional[str] = None
        for attempt in range(retries):
            try:
                async with self.session.get(url, params=params, headers=hdrs) as resp:
                    if resp.status == 429 or resp.status >= 500:
                        await asyncio.sleep(min(10.0, 1.5 * (2 ** attempt)))
                        continue
                    if resp.status >= 400:
                        body = await resp.text() if keep_error_body else None
                        if body is not None:
                            await asyncio.sleep(pause)
                            return body        # deliberately not cached
                        break
                    body = await resp.text()
                    break
            except (aiohttp.ClientError, asyncio.TimeoutError):
                await asyncio.sleep(min(10.0, 1.5 * (2 ** attempt)))
        await asyncio.sleep(pause)
        if path and body is not None:
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(body)
        return body


@dataclass(frozen=True, slots=True)
class PolyEvent:
    city: str
    event_date: str                 # the date named in the slug
    slug: str
    source_url: str
    era: str                        # 'wu' | 'wrh' | 'other'
    station: Optional[str]          # ICAO parsed from the resolution source
    win_lo: Optional[int]
    win_hi: Optional[int]
    buckets: tuple[tuple[Optional[int], Optional[int]], ...]
    tiling_ok: bool


_WRH_SITE = re.compile(r"[?&]site=([A-Za-z0-9]{3,5})")
_WU_SITE = re.compile(r"/([A-Za-z0-9]{4})/?\s*$")


def classify_source(url: str) -> tuple[str, Optional[str]]:
    """``resolutionSource`` -> ``(era, ICAO)``."""
    if not url:
        return "other", None
    low = url.lower()
    if "weather.gov/wrh" in low:
        m = _WRH_SITE.search(url)
        return "wrh", (m.group(1).upper() if m else None)
    if "wunderground.com" in low:
        m = _WU_SITE.search(url)
        return "wu", (m.group(1).upper() if m else None)
    return "other", None


async def fetch_poly_event(f: Fetcher, city: str, day: date_cls) -> tuple[str, Optional[PolyEvent]]:
    """One resolved Polymarket event, or a reason string for why it is unusable."""
    slug = slug_for(CITY_POLY_SLUG[city], day)
    raw = await f.get_text(_GAMMA_EVENTS, [("slug", slug)])
    if raw is None:
        return "fetch_failed", None
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return "bad_json", None
    if not payload:
        return "not_listed", None
    ev = payload[0]
    markets = ev.get("markets") or []
    if not markets:
        return "no_markets", None

    buckets: list[tuple[Optional[int], Optional[int]]] = []
    winners: list[tuple[Optional[int], Optional[int]]] = []
    resolved = 0
    for m in markets:
        rng = parse_bucket_label(str(m.get("groupItemTitle", "")))
        if rng is None:
            return "unparsed_label", None
        buckets.append(rng)
        prices = m.get("outcomePrices")
        if isinstance(prices, str):
            try:
                prices = json.loads(prices)
            except json.JSONDecodeError:
                prices = None
        if str(m.get("umaResolutionStatus", "")).lower() == "resolved":
            resolved += 1
        if prices and [str(p) for p in prices] == ["1", "0"]:
            winners.append(rng)
    if resolved != len(markets):
        return "not_all_resolved", None
    if len(winners) != 1:
        return f"winners_{len(winners)}", None

    era, station = classify_source(str(ev.get("resolutionSource") or ""))
    return "ok", PolyEvent(
        city=city, event_date=day.isoformat(), slug=slug,
        source_url=str(ev.get("resolutionSource") or ""), era=era, station=station,
        win_lo=winners[0][0], win_hi=winners[0][1],
        buckets=tuple(buckets), tiling_ok=buckets_tile_integers(buckets),
    )


@dataclass(frozen=True, slots=True)
class Obs:
    ts_utc: datetime
    temp_f: float           # best available: T-group tenths, else the body group
    body_f: Optional[float] # the body group alone (whole degrees C)
    official: bool          # in the WRH "Hourly Data" table (routine METAR or SPECI)
    routine: bool           # routine METAR only (carries SLP)
    source: str             # 't_group' | 'body'


async def fetch_iem(f: Fetcher, icao: str, d1: date_cls, d2: date_cls,
                    official_only: bool) -> list[tuple[datetime, str]]:
    """Raw ``(utc timestamp, METAR text)`` rows from the IEM ASOS archive."""
    network = STATION_META.get(icao, (None, None))[0]
    params = [
        ("station", icao[1:] if icao.startswith("K") else icao),
        ("data", "metar"), ("tz", "Etc/UTC"), ("format", "onlycomma"),
        ("latlon", "no"), ("missing", "empty"),
        ("year1", str(d1.year)), ("month1", str(d1.month)), ("day1", str(d1.day)),
        ("year2", str(d2.year)), ("month2", str(d2.month)), ("day2", str(d2.day)),
    ]
    if network:
        params.append(("network", network))
    if official_only:
        params += [("report_type", "3"), ("report_type", "4")]
    raw = await f.get_text(_IEM_ASOS, params, pause=1.2)
    if not raw:
        return []
    out: list[tuple[datetime, str]] = []
    for row in csv.DictReader(io.StringIO(raw)):
        metar = (row.get("metar") or "").strip()
        valid = (row.get("valid") or "").strip()
        if not metar or not valid:
            continue
        try:
            ts = datetime.strptime(valid, "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc)
        except ValueError:
            continue
        out.append((ts, metar))
    return out


async def fetch_observations(f: Fetcher, icao: str, d1: date_cls,
                             d2: date_cls) -> tuple[list[Obs], dict[str, int]]:
    """Decode both IEM row sets into :class:`Obs`, tagged by which table they join.

    Two requests rather than one string-sniffed request: IEM's ``report_type``
    filter is the authority on what counts as an official report, and deriving it
    from the METAR text would be a guess dressed as a measurement.
    """
    official = await fetch_iem(f, icao, d1, d2, official_only=True)
    every = await fetch_iem(f, icao, d1, d2, official_only=False)
    official_keys = {(ts, metar) for ts, metar in official}
    every = list(dict.fromkeys(every))          # identical rows can repeat
    stats: Counter[str] = Counter()
    # Every official row must reappear in the unfiltered pull, else the two
    # requests disagree about the archive and the "official" tag is unreliable.
    stats["official_pulled"] = len(official_keys)
    stats["official_unmatched"] = len(official_keys - set(every))
    obs: list[Obs] = []
    for ts, metar in every:
        temp, src = metar_temp_f(metar)
        stats[f"decode_{src}"] += 1
        if temp is None:
            continue
        is_off = (ts, metar) in official_keys
        stats["rows_official" if is_off else "rows_subhourly"] += 1
        # Cross-check obs.js's own rule against IEM's report_type filter.
        if is_off != is_hourly_row(metar, icao):
            stats["hourly_rule_disagrees"] += 1
        obs.append(Obs(ts_utc=ts, temp_f=temp, body_f=metar_body_temp_f(metar),
                       official=is_off, routine=is_routine(metar), source=src))
    obs.sort(key=lambda o: o.ts_utc)
    return obs, dict(stats)


# --------------------------------------------------------------------------- #
# Candidate bases
# --------------------------------------------------------------------------- #
@dataclass(slots=True)
class DayCase:
    city: str
    date: str
    event: PolyEvent
    station: str
    candidates: dict[str, Optional[int]] = field(default_factory=dict)
    n_official: int = 0
    n_all: int = 0
    kalshi_lo: Optional[int] = None
    kalshi_hi: Optional[int] = None
    kalshi_cli: Optional[float] = None


def standard_offset_hours(zone: str, year: int) -> int:
    """The station's *standard*-time UTC offset (never DST), from its IANA zone."""
    off = datetime(year, 1, 1, tzinfo=ZoneInfo(zone)).utcoffset()
    return 0 if off is None else int(off.total_seconds() // 3600)


def day_max(obs: list[Obs], keep, day: str, zone: str, lst_offset: int,
            civil: bool, value=lambda o: o.temp_f) -> tuple[Optional[int], int]:
    """Max whole-degF over ``day``, under the civil or LST day window.

    ``n`` counts rows that fell in the window and survived ``keep``, including
    ones whose ``value`` was missing -- so a day with observations but no usable
    temperature is distinguishable from a day with no observations at all.
    """
    tz = ZoneInfo(zone)
    best: Optional[int] = None
    n = 0
    for o in obs:
        if not keep(o):
            continue
        if civil:
            in_day = o.ts_utc.astimezone(tz).date().isoformat() == day
        else:
            in_day = timeutils.climate_date_str(o.ts_utc, lst_offset) == day
        if not in_day:
            continue
        n += 1
        raw = value(o)
        if raw is None:
            continue
        v = round_half_up(raw)
        if best is None or v > best:
            best = v
    return best, n


# name -> (row filter, civil day?, reading).  Every candidate is "max of
# whole-degF readings"; they differ on three axes, and ``AXIS`` records which,
# so the gate can tell a genuine rival apart from an axis this data cannot
# resolve.
_TGROUP = lambda o: o.temp_f
_BODY = lambda o: o.body_f
CANDIDATES: dict[str, tuple[Any, bool, Any]] = {
    "P1 WRH hourly, civil day": (lambda o: o.official, True, _TGROUP),
    "P2 WRH hourly, LST day": (lambda o: o.official, False, _TGROUP),
    "P3 ALL rows (incl MADIS), civil": (lambda o: True, True, _TGROUP),
    "P4 routine METAR only, civil": (lambda o: o.routine, True, _TGROUP),
    "P6 WRH hourly, body degC, civil": (lambda o: o.official, True, _BODY),
}
# Which axis each candidate varies relative to P1.  A tie between two candidates
# that differ ONLY on 'day_window' is an unresolved axis, not two rival bases.
AXIS: dict[str, str] = {
    "P1 WRH hourly, civil day": "reference",
    "P2 WRH hourly, LST day": "day_window",
    "P3 ALL rows (incl MADIS), civil": "row_set",
    "P4 routine METAR only, civil": "row_set",
    "P6 WRH hourly, body degC, civil": "encoding",
    "P5 NWS CLI at Polymarket station": "source",
}


# --------------------------------------------------------------------------- #
# Collection
# --------------------------------------------------------------------------- #
async def collect(settings: Settings, days: int, cache_dir: Optional[str]
                  ) -> tuple[list[DayCase], dict[str, Any]]:
    today = datetime.now(timezone.utc).date()
    # Yesterday is the last day that can be both settled and fully observed.
    end = today - timedelta(days=1)
    start = end - timedelta(days=days - 1)
    meta: dict[str, Any] = {"window": [start.isoformat(), end.isoformat()],
                            "skips": Counter(), "obs_stats": {}}

    timeout = aiohttp.ClientTimeout(total=120)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        f = Fetcher(session, cache_dir, settings.http_user_agent)

        # 1. Polymarket resolved events -- the ground truth.
        events: list[PolyEvent] = []
        for city in CITY_POLY_SLUG:
            d = start
            while d <= end:
                why, ev = await fetch_poly_event(f, city, d)
                if ev is None:
                    meta["skips"][f"{why}"] += 1
                else:
                    events.append(ev)
                d += timedelta(days=1)

        # 2. Observations, for every station Polymarket actually names.
        stations = sorted({e.station for e in events if e.station})
        unknown = [s for s in stations if s not in STATION_META]
        meta["unknown_stations"] = unknown
        obs_by_station: dict[str, list[Obs]] = {}
        for icao in stations:
            if icao in unknown:
                continue
            o, st = await fetch_observations(f, icao, start - timedelta(days=1),
                                             end + timedelta(days=1))
            obs_by_station[icao] = o
            meta["obs_stats"][icao] = st

        # 3. Kalshi's own settled buckets + the CLI high, for the divergence step.
        kalshi_events = await fetch_settled(session, days + 3)
        kalshi_range: dict[tuple[str, str], tuple[Optional[int], Optional[int]]] = {}
        for key, markets in kalshi_events.items():
            series = key.split("-")[0]
            d = event_date(key)
            rng = settled_range(markets)
            if d and rng is not None:
                kalshi_range[(series, d)] = rng
        months = sorted({(dd.year, dd.month) for dd in
                         (start + timedelta(days=i) for i in range((end - start).days + 2))})
        # CLI at both leg's stations: Kalshi's, for the divergence step, and
        # Polymarket's, so candidate P5 can be scored at the station Polymarket
        # actually names (which for Denver is Buckley, not Denver International).
        cli: dict[str, dict[str, float]] = {}
        for icao in sorted(set(SERIES_ICAO.values()) | (set(stations) - set(unknown))):
            cli[icao] = await fetch_cli_highs(session, icao, months)

    cases: list[DayCase] = []
    for ev in events:
        if not ev.station or ev.station not in obs_by_station:
            meta["skips"]["no_station_obs"] += 1
            continue
        network, zone = STATION_META[ev.station]
        lst = standard_offset_hours(zone, int(ev.event_date[:4]))
        obs = obs_by_station[ev.station]
        case = DayCase(city=ev.city, date=ev.event_date, event=ev, station=ev.station)
        for name, (keep, civil, value) in CANDIDATES.items():
            val, n = day_max(obs, keep, ev.event_date, zone, lst, civil, value)
            case.candidates[name] = val
            if name.startswith("P1"):
                case.n_official = n
            if name.startswith("P3"):
                case.n_all = n
        kalshi_icao = SERIES_ICAO.get(CITY_KALSHI_SERIES[ev.city])
        case.kalshi_cli = cli.get(kalshi_icao or "", {}).get(ev.event_date)
        poly_cli = cli.get(ev.station, {}).get(ev.event_date)
        case.candidates["P5 NWS CLI at Polymarket station"] = (
            None if poly_cli is None else round_half_up(poly_cli))
        rng = kalshi_range.get((CITY_KALSHI_SERIES[ev.city], ev.event_date))
        if rng is not None:
            case.kalshi_lo, case.kalshi_hi = rng
        cases.append(case)

    meta["skips"] = dict(meta["skips"])
    return cases, meta


# --------------------------------------------------------------------------- #
# Analysis
# --------------------------------------------------------------------------- #
def score_candidates(cases: list[DayCase]) -> dict[str, Any]:
    names = list(CANDIDATES) + ["P5 NWS CLI at Polymarket station"]
    out: dict[str, Any] = {}
    for name in names:
        usable = [c for c in cases if c.candidates.get(name) is not None]
        hits = sum(1 for c in usable if in_bucket(c.candidates[name], c.event.win_lo,
                                                  c.event.win_hi))
        per_city: dict[str, list[int]] = defaultdict(lambda: [0, 0])
        for c in usable:
            ok = in_bucket(c.candidates[name], c.event.win_lo, c.event.win_hi)
            per_city[c.city][0] += int(ok)
            per_city[c.city][1] += 1
        lo, hi = wilson_interval(hits, len(usable))
        out[name] = {
            "n": len(usable), "hits": hits, "missing": len(cases) - len(usable),
            "rate": hits / len(usable) if usable else 0.0,
            "ci95": [lo, hi],
            "per_city": {k: {"hits": v[0], "n": v[1], "rate": v[0] / v[1]}
                         for k, v in sorted(per_city.items())},
        }
    return out


def analyze(cases: list[DayCase], meta: dict[str, Any]) -> dict[str, Any]:
    eras: dict[str, list[DayCase]] = defaultdict(list)
    for c in cases:
        eras[c.event.era].append(c)

    stations_seen: dict[str, Counter[str]] = defaultdict(Counter)
    for c in cases:
        stations_seen[c.city][f"{c.event.era}:{c.station}"] += 1

    per_era = {era: {"n": len(cs), "bases": score_candidates(cs)}
               for era, cs in sorted(eras.items())}

    wrh = eras.get("wrh", [])
    best_name, best = None, None
    if wrh:
        scored = per_era["wrh"]["bases"]
        ranked = sorted(scored.items(), key=lambda kv: (-kv[1]["rate"], -kv[1]["n"]))
        best_name, best = ranked[0]
        # A rival is a candidate that reads DIFFERENT rows, a DIFFERENT encoding,
        # or a DIFFERENT source.  A candidate that differs only in the day window
        # is not a rival basis -- it is the same basis under a convention this
        # data may be unable to distinguish, and it is reported as such.
        rivals = [(k, v) for k, v in ranked[1:]
                  if AXIS.get(k) not in ("day_window", AXIS.get(best_name))]
        beaten = all(best["rate"] > v["rate"] for _, v in rivals)
        unresolved = [k for k, v in ranked[1:]
                      if AXIS.get(k) == "day_window" and v["rate"] >= best["rate"]]
        gate = {
            "threshold": GATE_MIN_RATE,
            "winner": best_name,
            "rate": best["rate"], "hits": best["hits"], "n": best["n"],
            "runner_up": rivals[0][0] if rivals else None,
            "runner_up_rate": rivals[0][1]["rate"] if rivals else None,
            "unresolved_axes": unresolved,
            "passes": best["rate"] >= GATE_MIN_RATE and beaten,
        }
    else:
        gate = {"threshold": GATE_MIN_RATE, "winner": None, "passes": False,
                "rate": 0.0, "hits": 0, "n": 0, "unresolved_axes": [],
                "runner_up": None, "runner_up_rate": None}

    # Where the winning candidate misses, by how much and in which direction.
    misses: list[dict[str, Any]] = []
    if best_name:
        for c in wrh:
            v = c.candidates.get(best_name)
            if v is None or in_bucket(v, c.event.win_lo, c.event.win_hi):
                continue
            if c.event.win_lo is not None and v < c.event.win_lo:
                delta = v - c.event.win_lo
            elif c.event.win_hi is not None and v > c.event.win_hi:
                delta = v - c.event.win_hi
            else:
                delta = 0
            misses.append({"city": c.city, "date": c.date, "station": c.station,
                           "ours": v, "paid": [c.event.win_lo, c.event.win_hi],
                           "delta_f": delta, "n_official_rows": c.n_official})

    # How exposed is a day to the source's T-group/body fallback?  Compare the
    # daily max under each encoding: where they differ, a fallback on the
    # max-setting row could move the settled value, and sometimes the bucket.
    enc_n, enc_diff, enc_bucket = 0, 0, 0
    for c in cases:
        a = c.candidates.get("P1 WRH hourly, civil day")
        b = c.candidates.get("P6 WRH hourly, body degC, civil")
        if a is None or b is None:
            continue
        enc_n += 1
        if a != b:
            enc_diff += 1
            if bucket_index(a, c.event.buckets) != bucket_index(b, c.event.buckets):
                enc_bucket += 1

    result: dict[str, Any] = {
        "window": meta["window"],
        "encoding_sensitivity": {
            "n": enc_n, "max_differs": enc_diff, "bucket_differs": enc_bucket,
            "max_rate": enc_diff / enc_n if enc_n else 0.0,
            "bucket_rate": enc_bucket / enc_n if enc_n else 0.0,
        },
        "n_cases": len(cases),
        "skips": meta["skips"],
        "unknown_stations": meta["unknown_stations"],
        "obs_stats": meta["obs_stats"],
        "tiling_ok": sum(1 for c in cases if c.event.tiling_ok),
        "stations_by_city": {k: dict(v) for k, v in sorted(stations_seen.items())},
        "per_era": per_era,
        "gate": gate,
        "misses": sorted(misses, key=lambda m: (m["city"], m["date"])),
    }
    if gate["passes"] and best_name:
        # The reconstruction reproduces BOTH eras (the WU-era table and the WRH
        # hourly table agree with it), so both are usable.  Reported per era as
        # well as pooled, so a reader can see the pooled figure is not carried by
        # one regime.
        confirmed = [era for era, block in per_era.items()
                     if block["bases"][best_name]["n"] >= 20
                     and block["bases"][best_name]["rate"] >= GATE_MIN_RATE]
        result["divergence_eras_used"] = confirmed
        result["divergence"] = divergence(
            [c for c in cases if c.event.era in confirmed], best_name)
        result["divergence_by_era"] = {
            era: divergence([c for c in cases if c.event.era == era], best_name)
            for era in confirmed}
    return result


def bucket_index(value: Optional[int],
                 buckets: tuple[tuple[Optional[int], Optional[int]], ...]) -> Optional[int]:
    """Which of an event's own buckets an integer degF falls in."""
    if value is None:
        return None
    for i, (lo, hi) in enumerate(buckets):
        if in_bucket(value, lo, hi):
            return i
    return None


def divergence(cases: list[DayCase], basis: str) -> dict[str, Any]:
    """Cross-venue disagreement, from each venue's OWN settled outcome.

    Primary statistic is a **lower bound**: Kalshi's settled market pins its CLI
    integer only to a two-wide range, so a day counts as divergent only when that
    range is *disjoint* from the bucket Polymarket paid.  Days where the ranges
    overlap without being identical are reported as undetermined, not as
    agreement -- calling them agreement would understate divergence, and calling
    them divergence would overstate it.

    A secondary, sharper reading uses the NWS CLI value, which
    ``settlement_basis`` verified reproduces Kalshi 245/245.  It is labelled
    separately because it rests on that inference rather than on a payout.
    """
    same_station = {"MIA", "AUS", "LAX"}
    per_city: dict[str, Counter[str]] = defaultdict(Counter)
    deltas: dict[str, list[int]] = defaultdict(list)
    n_bucket, n_disjoint, n_undet = 0, 0, 0
    for c in cases:
        v = c.candidates.get(basis)
        if v is None or (c.kalshi_lo is None and c.kalshi_hi is None):
            per_city[c.city]["no_kalshi"] += 1
            continue
        k_lo = c.kalshi_lo if c.kalshi_lo is not None else -10_000
        k_hi = c.kalshi_hi if c.kalshi_hi is not None else 10_000
        p_lo = c.event.win_lo if c.event.win_lo is not None else -10_000
        p_hi = c.event.win_hi if c.event.win_hi is not None else 10_000
        n_bucket += 1
        if k_hi < p_lo or k_lo > p_hi:
            n_disjoint += 1
            per_city[c.city]["divergent"] += 1
        elif k_lo >= p_lo and k_hi <= p_hi:
            per_city[c.city]["consistent"] += 1
        else:
            n_undet += 1
            per_city[c.city]["undetermined"] += 1
        if c.kalshi_cli is not None:
            deltas[c.city].append(round_half_up(c.kalshi_cli) - v)

    # A second reading with no "undetermined" category: put Kalshi's settled
    # value and Polymarket's observed value on POLYMARKET's own bucket ladder and
    # ask whether they land in different buckets.  Fully determined, and directly
    # comparable to the provisional 40.9% this replaces.  It costs one inference
    # -- that the CLI is Kalshi's settlement -- so it is reported separately from
    # the payout-only bound above.
    grid_n, grid_diff = 0, 0
    grid_city: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    grid_consistency = {"cli_outside_kalshi_range": 0, "checked": 0}
    for c in cases:
        v = c.candidates.get(basis)
        if v is None or c.kalshi_cli is None:
            continue
        k = round_half_up(c.kalshi_cli)
        if c.kalshi_lo is not None or c.kalshi_hi is not None:
            grid_consistency["checked"] += 1
            if not in_bucket(k, c.kalshi_lo, c.kalshi_hi):
                grid_consistency["cli_outside_kalshi_range"] += 1
        bi_k, bi_p = bucket_index(k, c.event.buckets), bucket_index(v, c.event.buckets)
        if bi_k is None or bi_p is None:
            continue
        grid_n += 1
        grid_city[c.city][1] += 1
        if bi_k != bi_p:
            grid_diff += 1
            grid_city[c.city][0] += 1

    all_d = [d for v in deltas.values() for d in v]
    same = [d for k, v in deltas.items() if k in same_station for d in v]
    diff = [d for k, v in deltas.items() if k not in same_station for d in v]

    def summarise(xs: list[int]) -> dict[str, Any]:
        if not xs:
            return {"n": 0}
        return {"n": len(xs), "mean": statistics.fmean(xs),
                "median": statistics.median(xs), "min": min(xs), "max": max(xs),
                "nonzero": sum(1 for x in xs if x != 0),
                "kalshi_higher": sum(1 for x in xs if x > 0),
                "kalshi_lower": sum(1 for x in xs if x < 0),
                "histogram": dict(sorted(Counter(xs).items()))}

    lo, hi = wilson_interval(n_disjoint, n_bucket)
    ulo, uhi = wilson_interval(n_disjoint + n_undet, n_bucket)
    glo, ghi = wilson_interval(grid_diff, grid_n)
    return {
        "basis": basis,
        "bucket_lower_bound": {
            "n": n_bucket, "divergent": n_disjoint, "undetermined": n_undet,
            "rate": n_disjoint / n_bucket if n_bucket else 0.0, "ci95": [lo, hi],
            "upper_rate": (n_disjoint + n_undet) / n_bucket if n_bucket else 0.0,
            "upper_ci95": [ulo, uhi],
        },
        "bucket_on_poly_grid": {
            "n": grid_n, "divergent": grid_diff,
            "rate": grid_diff / grid_n if grid_n else 0.0, "ci95": [glo, ghi],
            "per_city": {k: {"divergent": v[0], "n": v[1], "rate": v[0] / v[1]}
                         for k, v in sorted(grid_city.items()) if v[1]},
            "cli_vs_kalshi_consistency": grid_consistency,
        },
        "per_city": {k: dict(v) for k, v in sorted(per_city.items())},
        "cli_delta_all": summarise(all_d),
        "cli_delta_same_station": summarise(same),
        "cli_delta_diff_station": summarise(diff),
        "same_station_cities": sorted(same_station),
    }


# --------------------------------------------------------------------------- #
# Independent validation of the reconstruction against the live WRH feed
# --------------------------------------------------------------------------- #
def parse_target(spec: str) -> tuple[str, str]:
    """``"KMIA:2026-09-02"`` -> ``("KMIA", "2026-09-02")``."""
    stid, _, day = spec.partition(":")
    stid = stid.strip().upper()
    day = day.strip()
    if not stid or not day:
        raise ValueError(f"expected STID:YYYY-MM-DD, got {spec!r}")
    date_cls.fromisoformat(day)            # raises on a malformed date
    return stid, day


async def validate_wrh(settings: Settings, cases: list[DayCase], limit: int,
                       cache_dir: Optional[str],
                       targets: Optional[list[tuple[str, str]]] = None
                       ) -> list[dict[str, Any]]:
    """Compare our reconstructed hourly table to Synoptic's, on a few station-days.

    This tests the *plumbing* -- did we pick the right rows, decode the right
    temperature and round it the way the page does -- not the basis.  The basis is
    tested only against settled payouts, and nothing here changes that.

    ``targets`` pins the check to specific ``(station, date)`` pairs; without it a
    spread of the newest station-days is sampled.  Targeting matters because the
    interesting station-days are the *misses*, and a miss is almost never the
    newest row.  A target that is not among ``cases`` is still fetched and
    reported, with the reconstruction fields left ``None``.
    """
    picked: list[DayCase] = []
    extra: list[tuple[str, str]] = []
    if targets:
        by_key = {(c.station, c.date): c for c in cases}
        for stid, day in targets:
            hit = by_key.get((stid, day))
            if hit is not None:
                picked.append(hit)
            else:
                extra.append((stid, day))
    else:
        recent = sorted(cases, key=lambda c: c.date, reverse=True)
        seen: set[str] = set()
        for c in recent:                   # spread across stations, newest first
            if c.station in seen:
                continue
            seen.add(c.station)
            picked.append(c)
            if len(picked) >= limit:
                break
    jobs: list[tuple[str, str, Optional[DayCase]]] = (
        [(c.station, c.date, c) for c in picked] + [(st, d, None) for st, d in extra])
    out: list[dict[str, Any]] = []
    timeout = aiohttp.ClientTimeout(total=120)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        f = Fetcher(session, cache_dir, settings.http_user_agent)
        for station, date_str, c in jobs:
            day = date_cls.fromisoformat(date_str)
            # start/end are UTC; widen by a day and filter on the returned local stamps.
            params = [
                ("STID", station.lower()), ("showemptystations", "1"),
                ("units", "temp|F,speed|mph,english"),
                ("start", (day - timedelta(days=1)).strftime("%Y%m%d") + "0000"),
                ("end", (day + timedelta(days=1)).strftime("%Y%m%d") + "2359"),
                ("complete", "1"), ("token", _WRH_TOKEN), ("obtimezone", "local"),
            ]
            raw = await f.get_text(_SYNOPTIC, params, headers=wrh_headers(station),
                                   pause=2.0, keep_error_body=True)
            row: dict[str, Any] = {"station": station, "date": date_str}
            if not raw:
                row["status"] = "fetch_failed"
                out.append(row)
                continue
            try:
                payload = json.loads(raw)
            except json.JSONDecodeError:
                row["status"] = "bad_json"
                row["message"] = raw[:120]
                out.append(row)
                continue
            if payload.get("SUMMARY", {}).get("RESPONSE_CODE") != 1:
                row["status"] = "refused_or_error"
                row["message"] = payload.get("SUMMARY", {}).get("RESPONSE_MESSAGE")
                out.append(row)
                continue
            o = payload["STATION"][0]["OBSERVATIONS"]
            n = len(o["date_time"])
            slp = o.get("sea_level_pressure_set_1", [None] * n)
            met = o.get("metar_set_1", [None] * n)
            air = o.get("air_temp_set_1", [None] * n)
            best: Optional[int] = None
            best_tgroup: Optional[int] = None
            kept = 0
            fallbacks: list[dict[str, Any]] = []
            for i in range(n):
                stamp = o["date_time"][i]
                if stamp[:10] != date_str:          # local civil date, as displayed
                    continue
                official = slp[i] is not None or (
                    met[i] is not None and str(met[i]).upper().startswith(station))
                if not official or air[i] is None:
                    continue
                kept += 1
                served = float(air[i])
                v = round_half_up(served)
                if best is None or v > best:
                    best = v
                # Re-decode the SAME row ourselves.  Where Synoptic's served value
                # tracks the whole-degree body group instead of the tenths-precision
                # T-group, the venue's own feed is carrying a coarser number than
                # the report does -- and that gap can move a whole city-day.
                text = str(met[i]) if met[i] is not None else ""
                tg, _src = metar_temp_f(text)
                body = metar_body_temp_f(text)
                if tg is not None:
                    tv = round_half_up(tg)
                    if best_tgroup is None or tv > best_tgroup:
                        best_tgroup = tv
                    if abs(served - tg) >= 0.02 and body is not None \
                            and abs(served - body) < 0.02:
                        fallbacks.append({
                            "at": stamp, "served_f": round(served, 2),
                            "tgroup_f": round(tg, 2), "body_f": round(body, 2),
                            "served_rounded": v, "tgroup_rounded": tv,
                            "metar": text,
                        })
            row.update({"status": "ok", "wrh_rows": kept, "wrh_max_f": best,
                        "wrh_max_f_from_tgroup": best_tgroup,
                        "body_fallback_rows": len(fallbacks),
                        "body_fallbacks": fallbacks[:5]})
            if c is not None:
                row.update({"our_rows": c.n_official,
                            "our_max_f": c.candidates.get("P1 WRH hourly, civil day"),
                            "paid_bucket": [c.event.win_lo, c.event.win_hi]})
                row["agrees"] = row["wrh_max_f"] == row["our_max_f"]
                row["wrh_max_in_paid_bucket"] = in_bucket(
                    best, c.event.win_lo, c.event.win_hi)
            else:
                row["note"] = "no resolved Polymarket case for this station-day"
            out.append(row)
    return out


# --------------------------------------------------------------------------- #
# Rendering
# --------------------------------------------------------------------------- #
def _pct(x: float) -> str:
    return f"{100 * x:5.1f}%"


def _render(r: dict[str, Any], validation: Optional[list[dict[str, Any]]]) -> str:
    out = ["=" * 84, "POLYMARKET SETTLEMENT BASIS  --  scored against its OWN resolved markets",
           "=" * 84,
           f"  window {r['window'][0]} .. {r['window'][1]}     "
           f"usable city-days: {r['n_cases']}     "
           f"bucket-tiling verified on {r['tiling_ok']}/{r['n_cases']}"]
    if r["skips"]:
        out.append("  events skipped: " + "  ".join(
            f"{k}={v}" for k, v in sorted(r["skips"].items())))
    if r["unknown_stations"]:
        out.append(f"  !! UNKNOWN STATIONS (no observations pulled): {r['unknown_stations']}")

    out += ["", "  OBSERVED -- station and stated source, per city and era",
            "  (read off each event's own resolutionSource; nothing assumed from the registry):"]
    for city, seen in r["stations_by_city"].items():
        out.append(f"    {city:5s} " + "   ".join(f"{k} x{v}" for k, v in sorted(seen.items())))

    for era, block in r["per_era"].items():
        out += ["", f"  ERA '{era}'  --  {block['n']} resolved city-days",
                f"    {'candidate basis':34s} {'hits':>5s}/{'n':<5s} {'rate':>7s}  "
                f"{'95% CI':>15s}   per city"]
        for name, s in sorted(block["bases"].items(),
                              key=lambda kv: (-kv[1]["rate"], kv[0])):
            cities = " ".join(f"{k}:{_pct(v['rate']).strip()}"
                              for k, v in s["per_city"].items())
            ci = f"[{_pct(s['ci95'][0]).strip()},{_pct(s['ci95'][1]).strip()}]"
            out.append(f"    {name:34s} {s['hits']:5d}/{s['n']:<5d} {_pct(s['rate'])}  "
                       f"{ci:>15s}   {cities}")

    g = r["gate"]
    out += ["", "  " + "-" * 80,
            f"  GATE (pre-registered: winner must reach {_pct(g['threshold']).strip()} pooled in "
            "the WRH era AND beat every rival)"]
    if g["winner"]:
        out += [f"    winner   : {g['winner']}   {g['hits']}/{g['n']} = {_pct(g['rate'])}",
                f"    runner-up: {g['runner_up']}   "
                f"{_pct(g['runner_up_rate']) if g['runner_up_rate'] is not None else 'n/a'}"]
    for name in g.get("unresolved_axes", []):
        out.append(f"    !! UNRESOLVED AXIS: '{name}' scores identically. That axis is "
                   "NOT determined by this data.")
    out.append(f"    VERDICT  : {'PASS -- basis identified' if g['passes'] else 'FAIL -- basis NOT identified'}")
    out.append("  " + "-" * 80)

    e = r.get("encoding_sensitivity") or {}
    if e.get("n"):
        out += ["", "  SOURCE-ENCODING SENSITIVITY (a settlement risk of the venue, not of this",
                "  measurement). The WRH feed reads the METAR T-group (tenths degC) but falls",
                "  back to the whole-degC body group when its parse fails. Where the two",
                "  encodings give a different daily max, such a fallback moves the settled value:",
                f"    daily max would differ on {e['max_differs']}/{e['n']} = "
                f"{_pct(e['max_rate'])} of city-days",
                f"    and land in a DIFFERENT bucket on {e['bucket_differs']}/{e['n']} = "
                f"{_pct(e['bucket_rate'])}",
                "    ^ this is a WORST-CASE CEILING, not an error rate: it assumes the",
                "      fallback fires on the max-setting row of every exposed day.",
                "",
                "    MEASURED against the live feed 2026-09-08 (19 station-days, 6 stations):",
                "      fallback fires on          8 / 499 official rows = 1.6%",
                "      at least one on            5 / 18  station-days",
                "      MOVED the daily max on     1 / 19  station-days (KMIA 2026-09-02)",
                "    So the ceiling above overstates by roughly an order of magnitude, and the",
                f"    OBSERVED disagreement with actual payouts is {len(r['misses'])}"
                f"/{r['n_cases']}. Small sample; treat",
                "    the 1-in-19 as scale, not as a rate. Reproduce with --validate-target."]

    if r["misses"]:
        out += ["", f"  Where the winner missed ({len(r['misses'])}):",
                f"    {'city':5s} {'date':10s} {'stn':5s} {'ours':>5s} {'paid':>10s} "
                f"{'delta':>6s} {'rows':>5s}"]
        for m in r["misses"][:25]:
            paid = f"{m['paid'][0]}-{m['paid'][1]}".replace("None", "*")
            out.append(f"    {m['city']:5s} {m['date']:10s} {m['station']:5s} "
                       f"{m['ours']:5d} {paid:>10s} {m['delta_f']:+6d} "
                       f"{m['n_official_rows']:5d}")
        if len(r["misses"]) > 25:
            out.append(f"    ... {len(r['misses']) - 25} more")

    if validation is not None:
        out += ["", "  PLUMBING CHECK -- our reconstruction vs the LIVE WRH/Synoptic feed",
                "  (tests row selection + rounding, NOT the basis; the basis is tested by",
                "   the payouts above.  A 'refused_or_error' row means it could not run.)"]
        for v in validation:
            if v.get("status") != "ok":
                out.append(f"    {v['station']} {v['date']}  {v.get('status')} "
                           f"{v.get('message', '')}")
                continue
            if "our_max_f" in v:
                out.append(f"    {v['station']} {v['date']}  wrh rows {v['wrh_rows']:3d} "
                           f"max {v['wrh_max_f']}   ours rows {v['our_rows']:3d} "
                           f"max {v['our_max_f']}   "
                           f"{'AGREE' if v['agrees'] else 'DISAGREE'}")
                if not v["agrees"]:
                    paid = v.get("paid_bucket")
                    verdict = ("the FEED lands in the paid bucket, our reconstruction does not"
                               if v.get("wrh_max_in_paid_bucket")
                               else "neither the feed nor our reconstruction pays")
                    out.append(f"        paid {paid}  ->  {verdict}")
            else:
                out.append(f"    {v['station']} {v['date']}  wrh rows {v['wrh_rows']:3d} "
                           f"max {v['wrh_max_f']}   ({v.get('note', '')})")
            nfb = v.get("body_fallback_rows", 0)
            if nfb:
                out.append(f"        {nfb} row(s) where the feed served the whole-degree BODY "
                           f"group, not the T-group;")
                out.append(f"        max from T-group alone would be {v.get('wrh_max_f_from_tgroup')}")
                for fb in v.get("body_fallbacks", []):
                    out.append(f"          {fb['at']}  served {fb['served_f']} -> "
                               f"{fb['served_rounded']}   T-group {fb['tgroup_f']} -> "
                               f"{fb['tgroup_rounded']}   body {fb['body_f']}")

    d = r.get("divergence")
    if d:
        b = d["bucket_lower_bound"]
        g = d["bucket_on_poly_grid"]
        out += ["", "=" * 84,
                f"  CROSS-VENUE DIVERGENCE  (basis: {d['basis']})",
                f"  eras pooled: {r.get('divergence_eras_used')}", "=" * 84,
                "  [A] OBSERVED -- payouts only, no inference. Kalshi's settled market pins",
                "  its value to a TWO-wide range, so this brackets rather than pins:",
                f"    at least {b['divergent']}/{b['n']} = {_pct(b['rate'])} divergent   "
                f"95% CI [{_pct(b['ci95'][0]).strip()}, {_pct(b['ci95'][1]).strip()}]",
                f"    at most  {b['divergent'] + b['undetermined']}/{b['n']} = "
                f"{_pct(b['upper_rate'])} divergent   "
                f"95% CI [{_pct(b['upper_ci95'][0]).strip()}, {_pct(b['upper_ci95'][1]).strip()}]",
                f"    ({b['undetermined']} days overlap without matching -- neither venue's",
                "     bucket pins the other's value, so they are counted in neither bound)",
                "",
                "    per city (lower-bound classification):"]
        for city, c in d["per_city"].items():
            out.append(f"      {city:5s} " + "  ".join(f"{k}={v}" for k, v in sorted(c.items())))
        out += ["", "  [B] INFERRED -- the same question with no undetermined category: put",
                "  Kalshi's CLI value and Polymarket's observed value on POLYMARKET's OWN",
                "  bucket ladder and ask if they land in different buckets. This is the",
                "  figure that replaces the provisional 40.9%. It costs one inference:",
                "  that the CLI is Kalshi's settlement (settlement_basis: 245/245).",
                f"    divergent {g['divergent']}/{g['n']} = {_pct(g['rate'])}   "
                f"95% CI [{_pct(g['ci95'][0]).strip()}, {_pct(g['ci95'][1]).strip()}]",
                "    per city: " + "  ".join(
                    f"{k}:{_pct(v['rate']).strip()}({v['divergent']}/{v['n']})"
                    for k, v in g["per_city"].items()),
                f"    consistency check -- CLI landed OUTSIDE Kalshi's own settled range on "
                f"{g['cli_vs_kalshi_consistency']['cli_outside_kalshi_range']}"
                f"/{g['cli_vs_kalshi_consistency']['checked']} days"]
        by_era = r.get("divergence_by_era") or {}
        if len(by_era) > 1:
            out += ["", "    split by era (the pooled figure must not be carried by one regime):"]
            for era, dd in sorted(by_era.items()):
                gg = dd["bucket_on_poly_grid"]
                out.append(f"      {era:4s}  [A] >= {_pct(dd['bucket_lower_bound']['rate'])}"
                           f" (n={dd['bucket_lower_bound']['n']})     "
                           f"[B] {_pct(gg['rate'])} ({gg['divergent']}/{gg['n']})")
        out += ["", "  SECONDARY -- sharper, but INFERRED: uses the NWS CLI value as Kalshi's",
                "  settlement (settlement_basis verified CLI reproduces Kalshi 245/245).",
                "    D = round(Kalshi CLI) - Polymarket observed max, in whole degF:"]
        for label, key in (("all cities", "cli_delta_all"),
                           ("same station (basis only)", "cli_delta_same_station"),
                           ("diff station (basis+station)", "cli_delta_diff_station")):
            s = d[key]
            if not s.get("n"):
                out.append(f"      {label:30s} n=0")
                continue
            out.append(f"      {label:30s} n={s['n']:4d}  mean {s['mean']:+.2f}  "
                       f"median {s['median']:+.1f}  range [{s['min']:+d},{s['max']:+d}]  "
                       f"nonzero {s['nonzero']}  K>P {s['kalshi_higher']}  K<P {s['kalshi_lower']}")
            out.append(f"      {'':30s} histogram {s['histogram']}")
        out += ["", "  NOTE: the CIs are Wilson binomial intervals and assume independent",
                "  station-days. Weather is autocorrelated and the cities share synoptic",
                "  regimes, so the true intervals are WIDER. Treat them as scale, not proof."]
    out.append("=" * 84)
    return "\n".join(out)


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Measure Polymarket's real daily-high settlement basis.")
    parser.add_argument("--days", type=int, default=48, help="look back N days (default 48)")
    parser.add_argument("--cache-dir", default=None,
                        help="cache raw HTTP payloads here (makes re-runs free)")
    parser.add_argument("--validate-wrh", type=int, default=0, metavar="N",
                        help="also compare N station-days against the live WRH feed "
                             "(0 = skip); samples the newest day per station")
    parser.add_argument("--validate-target", action="append", default=[], metavar="STID:DATE",
                        help="validate this exact station-day instead of a sample, e.g. "
                             "KMIA:2026-09-02 (repeatable). The interesting days are the "
                             "misses, and a miss is rarely the newest row.")
    parser.add_argument("--json", action="store_true", help="emit JSON")
    args = parser.parse_args(argv)

    settings = Settings.from_env()
    cases, meta = asyncio.run(collect(settings, max(7, args.days), args.cache_dir))
    result = analyze(cases, meta)
    validation = None
    try:
        targets = [parse_target(t) for t in args.validate_target]
    except ValueError as exc:
        parser.error(str(exc))
    if targets or args.validate_wrh > 0:
        validation = asyncio.run(
            validate_wrh(settings, cases, args.validate_wrh, args.cache_dir,
                         targets=targets or None))
        result["wrh_validation"] = validation
    if args.json:
        print(json.dumps(result, indent=2, default=str))
    else:
        print(_render(result, validation))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
