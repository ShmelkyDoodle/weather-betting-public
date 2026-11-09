"""Read-only live quote clients + monitor for the cross-venue basis engine.

This is the network layer that feeds the classifier in :mod:`cross_venue`. It
pulls live top-of-book quotes for each city's daily-high *buckets* and scans the
arb-eligible venue pairs (ForecastEx <-> Polymarket on MIA/AUS/DEN/LAX/PHIL) for
a lockable cross-book gap: ``yes_ask(A) + no_ask(B) < 100c`` net of fees. It
**only reads** -- no orders are ever placed.

Venue realities (2026-07):

* **Polymarket** -- fully reachable unauthenticated via the Gamma API
  (``gamma-api.polymarket.com/events?slug=<event-slug>``). Each city/day event
  has a ``markets`` array, one entry per temperature bucket, carrying the bucket
  label (``groupItemTitle``) and ``bestBid``/``bestAsk`` on the YES token in
  dollars. From those two numbers both marketable prices follow:
  ``YES_ask = bestAsk`` and ``NO_ask = 1 - bestBid`` (buying NO at ``1-p`` is
  selling YES at ``p``). Implemented and live-testable.

* **ForecastEx** -- has *no public quote feed*; its market data is only exposed
  through an authenticated **IBKR Client Portal Web API** session. So the
  ForecastEx client is a credential-gated adapter: when ``ibkr_web_api_base`` is
  unset it reports *not configured* and returns nothing, and the monitor runs
  Polymarket-only. (Bucket edges must also be confirmed before its quotes are
  fungible with Polymarket's -- see the UNVERIFIED rows in :mod:`cross_venue`.)

CLI::

    python -m weather_arb.cross_venue_quotes [--scope us|global]
        [--date YYYY-MM-DD] [--city MIA ...] [--all] [--json]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import date as date_cls, datetime, timezone
from typing import Optional

import aiohttp

from .config import Settings
from .cross_venue import (
    CITIES, PairClass, VENUE_BY_ID, VenueScope, classify_all,
    cross_book_lock_cents,
)
from .ibkr_client import IBKRClient
from .models import Venue

# City key -> Polymarket event-slug fragment.
POLYMARKET_CITY_SLUG: dict[str, str] = {
    "NYC": "nyc", "CHI": "chicago", "MIA": "miami", "AUS": "austin",
    "DEN": "denver", "LAX": "los-angeles", "PHIL": "philadelphia", "LON": "london",
}

# City key -> Kalshi daily-high series ticker (mirrors config.weather_series).
KALSHI_CITY_SERIES: dict[str, str] = {
    "NYC": "KXHIGHNY", "CHI": "KXHIGHCHI", "MIA": "KXHIGHMIA", "AUS": "KXHIGHAUS",
    "DEN": "KXHIGHDEN", "LAX": "KXHIGHLAX", "PHIL": "KXHIGHPHIL",
}
_MONTHS3 = {"JAN": 1, "FEB": 2, "MAR": 3, "APR": 4, "MAY": 5, "JUN": 6,
           "JUL": 7, "AUG": 8, "SEP": 9, "OCT": 10, "NOV": 11, "DEC": 12}


def kalshi_event_date(ticker: str) -> Optional[str]:
    """Settlement date (YYYY-MM-DD) from a Kalshi ticker's ``YYMMMDD`` segment,
    e.g. ``KXHIGHNY-26JUL21-B90.5`` -> ``2026-07-21``; ``None`` if unparseable."""
    parts = ticker.split("-")
    if len(parts) < 2 or len(parts[1]) != 7:
        return None
    tok = parts[1].upper()
    yy, mon, dd = tok[:2], tok[2:5], tok[5:]
    if mon not in _MONTHS3 or not (yy.isdigit() and dd.isdigit()):
        return None
    return f"20{yy}-{_MONTHS3[mon]:02d}-{int(dd):02d}"


def _kalshi_cents(value: object) -> Optional[int]:
    """A Kalshi price field (already cents) -> int in [1, 99], or None (0/absent
    means no resting offer on that side)."""
    try:
        n = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return n if 1 <= n <= 99 else None


def _coerce_float(value: object) -> Optional[float]:
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None

_HTTP_TIMEOUT = aiohttp.ClientTimeout(total=15)


# --------------------------------------------------------------------------- #
# Bucket quote model + parsing
# --------------------------------------------------------------------------- #
@dataclass(frozen=True, slots=True)
class BucketQuote:
    """One venue's top-of-book for one temperature bucket of one city/day.

    ``lo``/``hi`` are the inclusive °F range edges (``None`` = open-ended tail).
    Prices are integer cents [0, 100]; ``None`` on a side means that side is not
    offered (e.g. no resting YES bid -> NO cannot be lifted). Polymarket exposes
    no top-of-book size, so ``size`` is typically ``None`` (price signal only).
    """

    venue: str
    city: str
    lo: Optional[int]
    hi: Optional[int]
    label: str
    yes_ask_cents: Optional[int]
    no_ask_cents: Optional[int]
    size: Optional[int] = None

    @property
    def key(self) -> tuple[Optional[int], Optional[int]]:
        return (self.lo, self.hi)


_RANGE_RE = re.compile(r"(-?\d+)\s*[-–—]\s*(-?\d+)")
_INT_RE = re.compile(r"-?\d+")


def parse_bucket(title: str) -> Optional[tuple[Optional[int], Optional[int]]]:
    """Parse a bucket label into an inclusive ``(lo, hi)`` °F range.

    ``"76-77°F" -> (76, 77)``, ``"67°F or below" -> (None, 67)``,
    ``"86°F or higher" -> (86, None)``, ``"90°F" -> (90, 90)``. Returns ``None``
    if unparseable.
    """
    t = title.lower().replace("°f", "").replace("℉", "").strip()
    if any(p in t for p in ("or below", "or lower", "or less")) or t.startswith("≤"):
        m = _INT_RE.search(t)
        return (None, int(m.group())) if m else None
    if any(p in t for p in ("or higher", "or above", "or more")) or t.startswith("≥"):
        m = _INT_RE.search(t)
        return (int(m.group()), None) if m else None
    m = _RANGE_RE.search(t)
    if m:
        return (int(m.group(1)), int(m.group(2)))
    m = _INT_RE.fullmatch(t)
    return (int(m.group()), int(m.group())) if m else None


def _ask_cents(price: object) -> Optional[int]:
    """A dollar ask (0..1) -> integer cents, or ``None`` if absent/unparseable."""
    if price is None:
        return None
    try:
        return max(0, min(100, round(float(price) * 100)))
    except (TypeError, ValueError):
        return None


def _no_ask_from_bid(best_bid: object) -> Optional[int]:
    """NO marketable ask (cents) = 1 - (best YES bid). ``None`` if no YES bid."""
    if best_bid is None:
        return None
    try:
        return max(0, min(100, round((1.0 - float(best_bid)) * 100)))
    except (TypeError, ValueError):
        return None


# --------------------------------------------------------------------------- #
# Quote clients
# --------------------------------------------------------------------------- #
class QuoteClient(ABC):
    """Read-only per-venue quote source."""

    venue: str

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.available = True
        self.reason = ""

    @abstractmethod
    async def fetch_city(
        self, session: aiohttp.ClientSession, city_key: str, on_date: date_cls
    ) -> list[BucketQuote]:
        ...

    async def close(self) -> None:
        """Release any client-owned resources (default: none)."""
        return None


class PolymarketQuoteClient(QuoteClient):
    """Live Polymarket bucket quotes via the public Gamma API (unauthenticated)."""

    venue = Venue.POLYMARKET.value
    GAMMA_EVENTS = "https://gamma-api.polymarket.com/events"

    def event_slug(self, city_key: str, on_date: date_cls) -> Optional[str]:
        frag = POLYMARKET_CITY_SLUG.get(city_key)
        if not frag:
            return None
        month = on_date.strftime("%B").lower()
        return f"highest-temperature-in-{frag}-on-{month}-{on_date.day}-{on_date.year}"

    async def fetch_city(self, session, city_key, on_date):
        slug = self.event_slug(city_key, on_date)
        if not slug:
            return []
        try:
            async with session.get(self.GAMMA_EVENTS, params={"slug": slug},
                                   timeout=_HTTP_TIMEOUT) as resp:
                if resp.status != 200:
                    return []
                data = await resp.json(content_type=None)
        except (aiohttp.ClientError, asyncio.TimeoutError, json.JSONDecodeError):
            return []
        if not isinstance(data, list) or not data:
            return []
        return self.parse_event(city_key, data[0])

    def parse_event(self, city_key: str, event: dict) -> list[BucketQuote]:
        out: list[BucketQuote] = []
        for m in event.get("markets", []) or []:
            if m.get("closed") or m.get("active") is False:
                continue
            rng = parse_bucket(str(m.get("groupItemTitle", "")))
            if rng is None:
                continue
            out.append(BucketQuote(
                venue=self.venue, city=city_key, lo=rng[0], hi=rng[1],
                label=str(m.get("groupItemTitle", "")),
                yes_ask_cents=_ask_cents(m.get("bestAsk")),
                no_ask_cents=_no_ask_from_bid(m.get("bestBid")),
                size=None,
            ))
        return out


class KalshiQuoteClient(QuoteClient):
    """Live Kalshi daily-high bucket quotes via the public trade-api (no auth).

    Kalshi settles on the NWS CLI with an LST day window, so the classifier keeps
    it *basis* against every venue -- these quotes are for display / eyeballing the
    Kalshi-vs-Polymarket basis, and the lock scan never pairs them. Kalshi quotes
    ``yes_ask``/``no_ask`` directly in cents on each bucket market."""

    venue = Venue.KALSHI.value

    def __init__(self, settings: Settings) -> None:
        super().__init__(settings)
        self.base = settings.kalshi.rest_base_url

    async def fetch_city(self, session, city_key, on_date):
        series = KALSHI_CITY_SERIES.get(city_key)
        if not series:
            return []
        try:
            async with session.get(
                f"{self.base}/markets",
                params={"series_ticker": series, "status": "open", "limit": 200},
                timeout=_HTTP_TIMEOUT,
            ) as resp:
                if resp.status != 200:
                    return []
                data = await resp.json(content_type=None)
        except (aiohttp.ClientError, asyncio.TimeoutError, json.JSONDecodeError):
            return []
        return self.parse_markets(city_key, (data or {}).get("markets") or [], on_date)

    def parse_markets(self, city_key: str, markets: list, on_date) -> list[BucketQuote]:
        want = on_date.isoformat()
        out: list[BucketQuote] = []
        for m in markets:
            ed = kalshi_event_date(str(m.get("ticker", "")))
            if ed is not None and ed != want:      # keep only this day's buckets
                continue
            floor = _coerce_float(m.get("floor_strike"))
            cap = _coerce_float(m.get("cap_strike"))
            lo = round(floor) if floor is not None else None
            hi = round(cap) if cap is not None else None
            label = str(m.get("yes_sub_title") or m.get("subtitle")
                        or m.get("title") or f"{lo}-{hi}")
            out.append(BucketQuote(
                venue=self.venue, city=city_key, lo=lo, hi=hi, label=label,
                yes_ask_cents=_kalshi_cents(m.get("yes_ask")),
                no_ask_cents=_kalshi_cents(m.get("no_ask")), size=None,
            ))
        return out


class ForecastExQuoteClient(QuoteClient):
    """ForecastEx **threshold** quotes via an authenticated IBKR Client Portal API.

    ForecastEx has no public feed, so this is credential-gated on
    ``ibkr_web_api_base``. On IBKR, ForecastEx daily-high contracts are modelled
    as options (``secType=OPT``, exchange ``FORECASTX``) whose *strikes are
    temperature thresholds* -- each contract pays $1 if the day's high is at/above
    its strike. So this client emits one **threshold** BucketQuote per strike
    (``lo=strike, hi=None``, label ``">=X F"``); the monitor later synthesizes the
    2 F bins Polymarket uses (see :func:`synthesize_forecastex_bins`).

    Contract discovery has two paths:

    * **explicit** -- ``forecastex_contracts`` (``WA_FORECASTEX_CONTRACTS_JSON``)
      gives ``city -> [(strike, conid)]`` directly. Reliable; bypasses discovery.
    * **auto** -- with ``forecastex_symbols`` (``WA_FORECASTEX_SYMBOLS_JSON``,
      ``city -> underlying symbol``) it walks ``secdef/search`` -> ``secdef/strikes``
      -> ``secdef/info`` for the date. Best-effort; validate on first live run.

    Everything is read-only and defensive: any gap (no creds, no session, no
    mapping, transport error) yields no quotes plus a human-readable ``reason``.
    The threshold->strike semantics are still UNVERIFIED (see :mod:`cross_venue`),
    so confirm them before trusting a synthesized lock.
    """

    venue = Venue.FORECASTEX.value

    def __init__(self, settings: Settings) -> None:
        super().__init__(settings)
        self.base = settings.ibkr_web_api_base
        self._session: Optional[aiohttp.ClientSession] = None
        self._ibkr: Optional[IBKRClient] = None
        if not self.base:
            self.available = False
            self.reason = ("not configured -- ForecastEx has no public quote feed; "
                           "set IBKR_WEB_API_BASE to an authenticated Client Portal "
                           "Web API session to enable")
        elif not (settings.forecastex_contracts or settings.forecastex_symbols):
            self.available = False
            self.reason = ("gateway set but no contract map: provide "
                           "WA_FORECASTEX_CONTRACTS_JSON (city->[[strike,conid]]) or "
                           "WA_FORECASTEX_SYMBOLS_JSON (city->symbol) to locate contracts")

    async def _client(self) -> Optional[IBKRClient]:
        if self._ibkr is not None:
            return self._ibkr
        self._session = aiohttp.ClientSession(
            headers={"User-Agent": self.settings.http_user_agent}
        )
        ibkr = IBKRClient(self.base, self._session, verify_ssl=self.settings.ibkr_verify_ssl)
        if not await ibkr.ensure_ready():
            self.reason = ("gateway unreachable or not logged in at "
                           f"{self.base} -- start the Client Portal Gateway and log in")
            await self._session.close()
            self._session = None
            return None
        self._ibkr = ibkr
        return ibkr

    async def close(self) -> None:
        if self._session is not None:
            await self._session.close()
            self._session = None
            self._ibkr = None

    async def fetch_city(self, session, city_key, on_date):
        if not self.available:
            return []
        ibkr = await self._client()
        if ibkr is None:
            return []
        contracts = await self._discover(ibkr, city_key, on_date)  # [(strike, conid)]
        if not contracts:
            return []
        by_conid = {cid: strike for strike, cid in contracts}
        snap = await ibkr.snapshot(list(by_conid.keys()))
        out: list[BucketQuote] = []
        for conid, strike in by_conid.items():
            row = snap.get(conid)
            if not row:
                continue
            bid, ask = row.get("bid"), row.get("ask")
            out.append(BucketQuote(
                venue=self.venue, city=city_key, lo=int(round(strike)), hi=None,
                label=f">={int(round(strike))}F",
                yes_ask_cents=ask,
                no_ask_cents=(100 - bid) if bid is not None else None,
                size=row.get("ask_size"),
            ))
        return out

    async def _discover(self, ibkr, city_key, on_date) -> list[tuple[float, int]]:
        explicit = self.settings.forecastex_contracts_for(city_key)
        if explicit:
            return [(s, c) for s, c in explicit]
        symbol = self.settings.forecastex_symbol_for(city_key)
        if not symbol:
            return []
        matches = await ibkr.search_symbol(symbol)
        if not matches:
            return []
        try:
            underlying = int(matches[0]["conid"])
        except (KeyError, ValueError, TypeError):
            return []
        month = on_date.strftime("%b%y").upper()          # e.g. JUL26
        want_maturity = on_date.strftime("%Y%m%d")
        strikes = await ibkr.strikes(underlying, month)
        found: list[tuple[float, int]] = []
        for strike in strikes:
            info = await ibkr.secdef_info(underlying, month, strike)
            for row in info:
                if str(row.get("maturityDate", "")) == want_maturity and row.get("conid"):
                    found.append((strike, int(row["conid"])))
                    break
        return found


# --------------------------------------------------------------------------- #
# ForecastEx threshold -> Polymarket-style bin synthesis
# --------------------------------------------------------------------------- #
def _bin_label(lo: Optional[int], hi: Optional[int]) -> str:
    if lo is None:
        return f"{hi}F or below"
    if hi is None:
        return f"{lo}F or higher"
    return f"{lo}-{hi}F"


def synthesize_forecastex_bins(
    fe_thresholds: list[BucketQuote],
    target_bins: list[tuple[Optional[int], Optional[int]]],
    city: str, venue: str = Venue.FORECASTEX.value,
) -> list[BucketQuote]:
    """Combine ForecastEx ``>= X`` threshold quotes into the 2 F range bins that
    Polymarket lists, so the two venues become bucket-comparable.

    A bin ``[lo, hi]`` (integer highs lo..hi) ``= YES(>=lo) and not YES(>=hi+1)``.
    With a threshold's ask ``a`` and bid ``b`` (in the ``>=`` sense;
    ``NO_ask = 100 - b``)::

        bin YES-ask = a(lo)   + (100 - b(hi+1))     # buy >=lo YES + >=hi+1 NO
        bin NO-ask  = a(hi+1) + (100 - b(lo))       # buy >=hi+1 YES + >=lo NO

    Open tails: ``(None, hi)`` (high <= hi) uses only threshold ``hi+1``;
    ``(lo, None)`` (high >= lo) is threshold ``lo`` itself. A side is emitted only
    when every leg it needs is present. Combined asks may exceed 100 (no lockable
    price) and are kept unclamped so the scan reads them faithfully.

    NOTE: assumes ForecastEx ``>= strike`` semantics on whole-degree strikes --
    the still-UNVERIFIED part; confirm before trusting a synthesized lock.
    """
    thr: dict[int, tuple[Optional[int], Optional[int]]] = {}  # strike -> (bid, ask)
    for q in fe_thresholds:
        if q.lo is None:
            continue
        bid = (100 - q.no_ask_cents) if q.no_ask_cents is not None else None
        thr[q.lo] = (bid, q.yes_ask_cents)

    def yes_ask(x: int) -> Optional[int]:
        t = thr.get(x)
        return t[1] if t else None

    def no_ask(x: int) -> Optional[int]:
        t = thr.get(x)
        return (100 - t[0]) if (t and t[0] is not None) else None

    out: list[BucketQuote] = []
    for lo, hi in target_bins:
        y = n = None
        if lo is not None and hi is not None:
            a_lo, na_hi1 = yes_ask(lo), no_ask(hi + 1)
            a_hi1, na_lo = yes_ask(hi + 1), no_ask(lo)
            if a_lo is not None and na_hi1 is not None:
                y = a_lo + na_hi1
            if a_hi1 is not None and na_lo is not None:
                n = a_hi1 + na_lo
        elif lo is None and hi is not None:
            y, n = no_ask(hi + 1), yes_ask(hi + 1)
        elif lo is not None and hi is None:
            y, n = yes_ask(lo), no_ask(lo)
        if y is None and n is None:
            continue
        out.append(BucketQuote(venue, city, lo, hi, _bin_label(lo, hi), y, n))
    return out


def build_scan_input(
    quotes: dict[str, list[BucketQuote]], cities: list[str],
) -> dict[str, list[BucketQuote]]:
    """Return a copy of ``quotes`` with ForecastEx threshold quotes replaced by
    bins synthesized to the Polymarket bins present per city (so the scan aligns).
    Display keeps the raw thresholds; only the scan sees the synthesized bins.
    """
    fe = quotes.get(Venue.FORECASTEX.value)
    if not fe:
        return quotes
    pm = quotes.get(Venue.POLYMARKET.value, [])
    syn: list[BucketQuote] = []
    for city in cities:
        target = [q.key for q in pm if q.city == city]
        fe_city = [q for q in fe if q.city == city]
        if target and fe_city:
            syn.extend(synthesize_forecastex_bins(fe_city, target, city))
    merged = dict(quotes)
    merged[Venue.FORECASTEX.value] = syn
    return merged


# --------------------------------------------------------------------------- #
# Bucket-aligned cross-book lock scan
# --------------------------------------------------------------------------- #
@dataclass(frozen=True, slots=True)
class Lock:
    city: str
    bucket: str
    venue_a: str
    venue_b: str
    klass: str
    net_cents: int          # per-contract, net of fees; > 0 == arbitrage
    legs: str


def scan_bucket_locks(
    quotes: dict[str, list[BucketQuote]], scope: VenueScope,
    settings: Optional[Settings] = None,
) -> list[Lock]:
    """Cross-book lock scan over arb-eligible venue pairs, bucket by bucket.

    ``quotes`` maps venue id -> its BucketQuotes. Only pairs the classifier deems
    same-underlying are considered (Kalshi-vs-anything is basis, skipped), and
    only buckets with *identical* ``(lo, hi)`` edges on both venues are compared
    -- mismatched bucketing is not fungible. Both lock directions are evaluated;
    every comparable bucket is returned (sorted best-first) so the monitor can
    show near-misses, with ``net_cents > 0`` marking a real lock.
    """
    index: dict[tuple[str, str, tuple], BucketQuote] = {}
    for venue, qs in quotes.items():
        for q in qs:
            index[(venue, q.city, q.key)] = q

    locks: list[Lock] = []
    for v in classify_all(scope):
        if v.klass not in (PairClass.ARB_CONFIRMED, PairClass.ARB_CANDIDATE):
            continue
        a, b = v.venue_a, v.venue_b
        a_keys = [k for k in index if k[0] == a and k[1] == v.city]
        for ak in a_keys:
            qa = index[ak]
            qb = index.get((b, v.city, ak[2]))
            if qb is None:
                continue
            best: Optional[int] = None
            legs = ""
            if qa.yes_ask_cents is not None and qb.no_ask_cents is not None:
                p = cross_book_lock_cents(qa.yes_ask_cents, qb.no_ask_cents, a, b, 1, settings)
                best, legs = p, (f"buy YES@{VENUE_BY_ID[a].label} {qa.yes_ask_cents}c "
                                 f"+ NO@{VENUE_BY_ID[b].label} {qb.no_ask_cents}c")
            if qb.yes_ask_cents is not None and qa.no_ask_cents is not None:
                p = cross_book_lock_cents(qb.yes_ask_cents, qa.no_ask_cents, b, a, 1, settings)
                if best is None or p > best:
                    best, legs = p, (f"buy YES@{VENUE_BY_ID[b].label} {qb.yes_ask_cents}c "
                                     f"+ NO@{VENUE_BY_ID[a].label} {qa.no_ask_cents}c")
            if best is not None:
                locks.append(Lock(v.city, qa.label, a, b, v.klass.value, best, legs))
    locks.sort(key=lambda l: -l.net_cents)
    return locks


# --------------------------------------------------------------------------- #
# Monitor orchestration
# --------------------------------------------------------------------------- #
def candidate_cities(scope: VenueScope) -> list[str]:
    """Cities that have at least one arb-eligible venue pair (dedup, in order)."""
    seen: list[str] = []
    for v in classify_all(scope):
        if v.klass in (PairClass.ARB_CONFIRMED, PairClass.ARB_CANDIDATE) and v.city not in seen:
            seen.append(v.city)
    return seen


def _scope_clients(settings: Settings, scope: VenueScope) -> list[QuoteClient]:
    clients: list[QuoteClient] = [
        KalshiQuoteClient(settings),
        PolymarketQuoteClient(settings),
        ForecastExQuoteClient(settings),
    ]
    return [c for c in clients if scope.includes(VENUE_BY_ID[c.venue].jurisdiction)]


async def gather_quotes(
    settings: Settings, scope: VenueScope, cities: list[str], on_date: date_cls,
) -> tuple[dict[str, list[BucketQuote]], list[tuple[str, bool, str]]]:
    """Fetch bucket quotes for every in-scope venue across ``cities`` concurrently."""
    quotes: dict[str, list[BucketQuote]] = {}
    statuses: list[tuple[str, bool, str]] = []
    clients = _scope_clients(settings, scope)
    try:
        async with aiohttp.ClientSession(
            headers={"User-Agent": settings.http_user_agent}
        ) as session:
            for client in clients:
                if not client.available:
                    quotes[client.venue] = []
                    statuses.append((client.venue, False, client.reason))
                    continue
                batches = await asyncio.gather(
                    *(client.fetch_city(session, c, on_date) for c in cities)
                )
                qs = [q for batch in batches for q in batch]
                quotes[client.venue] = qs
                note = f"{len(qs)} bucket-quotes across {len(cities)} cities"
                if client.reason:
                    note += f" ({client.reason})"
                statuses.append((client.venue, bool(qs) or not client.reason, note))
    finally:
        for client in clients:
            await client.close()
    return quotes, statuses


def _render(
    quotes: dict[str, list[BucketQuote]], statuses: list[tuple[str, bool, str]],
    locks: list[Lock], cities: list[str], on_date: date_cls, scope: VenueScope,
) -> str:
    out: list[str] = []
    out.append("=" * 74)
    out.append(f"  CROSS-VENUE LIVE QUOTE MONITOR    scope={scope.value.upper()}  date={on_date}")
    out.append("  read-only -- lockable gap = yes_ask(A) + no_ask(B) < 100c, net of fees")
    out.append("=" * 74)

    out.append("")
    out.append("VENUE FEEDS:")
    for venue, ok, note in statuses:
        out.append(f"  [{'OK ' if ok else 'OFF'}] {VENUE_BY_ID[venue].label:<24} {note}")

    for city in cities:
        rows = {v: sorted((q for q in qs if q.city == city),
                          key=lambda q: (q.lo is None, q.lo if q.lo is not None else 0))
                for v, qs in quotes.items()}
        if not any(rows.values()):
            continue
        out.append("")
        out.append(f"{city}:")
        for venue, qs in rows.items():
            if not qs:
                continue
            out.append(f"  {VENUE_BY_ID[venue].label}:")
            for q in qs:
                ya = f"{q.yes_ask_cents}c" if q.yes_ask_cents is not None else " -"
                na = f"{q.no_ask_cents}c" if q.no_ask_cents is not None else " -"
                out.append(f"      {q.label:<16} YES-ask {ya:>4}   NO-ask {na:>4}")

    out.append("")
    out.append("-" * 74)
    hits = [l for l in locks if l.net_cents > 0]
    out.append(f"  LOCKS FOUND ({len(hits)}):")
    if hits:
        for l in hits:
            out.append(f"    + {l.city} {l.bucket}: {l.legs} -> lock {l.net_cents}c/contract")
    else:
        out.append("    (none) -- no bucket priced for a cross-book lock right now")
        if locks:
            best = locks[0]
            out.append(f"    tightest cross-book bucket: {best.city} {best.bucket} "
                       f"({best.net_cents:+d}c/contract)")
        if len(quotes.get(Venue.FORECASTEX.value, [])) == 0:
            out.append("    note: a lock needs two books; ForecastEx feed is off "
                       "(see VENUE FEEDS) so only Polymarket is priced.")
    out.append("=" * 74)
    return "\n".join(out)


def _parse_date(raw: Optional[str]) -> date_cls:
    if not raw:
        return datetime.now(timezone.utc).date()
    return datetime.strptime(raw, "%Y-%m-%d").date()


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Live read-only cross-venue quote monitor (nothing is placed)."
    )
    parser.add_argument("--scope", choices=[s.value for s in VenueScope], default=None,
                        help="us (default) or global; overrides WA_VENUE_SCOPE")
    parser.add_argument("--date", default=None, help="YYYY-MM-DD (default: today UTC)")
    parser.add_argument("--city", action="append", default=None,
                        help="city key (repeatable); default = arb-candidate cities")
    parser.add_argument("--all", action="store_true",
                        help="include every known city, not just candidates")
    parser.add_argument("--json", action="store_true", help="emit JSON")
    args = parser.parse_args(argv)

    settings = Settings.from_env()
    try:
        scope = VenueScope(args.scope or settings.venue_scope)
    except ValueError:
        scope = VenueScope.US
    on_date = _parse_date(args.date)

    if args.city:
        cities = [c.upper() for c in args.city]
    elif args.all:
        cities = [c for c, _ in CITIES]
    else:
        cities = candidate_cities(scope)

    quotes, statuses = asyncio.run(gather_quotes(settings, scope, cities, on_date))
    # Synthesize ForecastEx bins to match Polymarket's before scanning; display
    # (below) keeps the raw ForecastEx thresholds.
    locks = scan_bucket_locks(build_scan_input(quotes, cities), scope, settings)

    if args.json:
        print(json.dumps({
            "scope": scope.value, "date": str(on_date), "cities": cities,
            "feeds": [{"venue": v, "ok": ok, "note": n} for v, ok, n in statuses],
            "quotes": [
                {"venue": q.venue, "city": q.city, "bucket": q.label,
                 "lo": q.lo, "hi": q.hi, "yes_ask_cents": q.yes_ask_cents,
                 "no_ask_cents": q.no_ask_cents}
                for qs in quotes.values() for q in qs
            ],
            "locks": [
                {"city": l.city, "bucket": l.bucket, "pair": f"{l.venue_a}<->{l.venue_b}",
                 "net_cents": l.net_cents, "legs": l.legs}
                for l in locks if l.net_cents > 0
            ],
        }, indent=2))
    else:
        print(_render(quotes, statuses, locks, cities, on_date, scope))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
