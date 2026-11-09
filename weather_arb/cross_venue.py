"""Cross-venue basis monitor -- where the *same* weather bet is priced on more
than one exchange, and whether the two are truly the same underlying.

Motivation
----------
Kalshi is not the only place to bet a city's daily high. IBKR's **ForecastEx**
and **Polymarket** (US access via QCEX) run comparable daily-high markets, and
UK/EU exchanges (**Betfair**, **Smarkets**) run novelty weather books. A naive
trader assumes "NYC high today" is one event -- it is not. Each venue can differ
on three axes that decide Yes/No:

* **settlement station** -- Kalshi NYC = Central Park (KNYC); Polymarket NYC =
  LaGuardia (KLGA). Kalshi CHI = Midway (KMDW); Polymarket CHI = O'Hare (KORD).
  Kalshi DEN = Denver Intl (KDEN); Polymarket DEN = **Buckley SFB (KBKF)**, a
  different airfield 20 miles away.
* **data source** -- Kalshi settles on the next-day NWS **Climate Report (CLI)**
  (quality-controlled, captures 1-min / 6-hour highs); Polymarket settles on the
  **NWS WRH "Hourly Data" timeseries** (routine METARs + SPECIs, Temp column
  rounded half-up to a whole degree F), having settled on **Weather Underground**
  before ~2026-08-21; ForecastEx's source was read as Weather Underground on
  2026-07-22 and has not been re-checked since.
* **trading-day window** -- Kalshi uses **Local Standard Time year-round** (no
  DST); the other venues use the local **civil** day, i.e. the DST-aware wall
  clock. That is what :class:`DayWindow.LOCAL` means -- there is deliberately no
  separate ``CIVIL`` member, because a synonym would make otherwise-identical
  rows compare as different and manufacture a basis difference that is not there.

Provenance of the Polymarket rows (2026-09-08)
----------------------------------------------
Every Polymarket row below is **OBSERVED**, not inferred. ``weather_arb.
polymarket_basis`` scored candidate bases against Polymarket's *own resolved
payouts* over 282 city-days and identified the basis at 281/282 = 99.6%; the
per-city station is read off each event's own ``resolutionSource`` rather than
guessed. Two corrections to what this table used to say, and they are different
kinds of wrong:

* **DEN was never KDEN.** ``resolutionSource`` names KBKF in *both* eras
  (``.../us/co/aurora/KBKF`` in July, ``?site=kbkf`` in September). That entry
  was simply wrong, not stale.
* **The WU source WAS right, and went stale.** Polymarket switched from Weather
  Underground to NWS WRH around 2026-08-21. The superseded rows are kept in
  :data:`STATION_MAP` with ``superseded_on`` set rather than overwritten, so the
  switch stays visible -- a venue that changed its source once can change it
  again, which is exactly why ForecastEx's July reading is no longer trusted.
* **Philadelphia is not listed by Polymarket at all**, in either era. It is kept
  as an explicit ``NOT_LISTED`` row so the absence is stated rather than implied
  by omission.

Consequences
------------
Kalshi differs from every other venue on *source* and *day-window*, so
Kalshi-vs-other is always a **basis** (relative-value) trade, never a lock.

A lock needs two venues matching on *all* axes -- and as of 2026-09-08 there are
**none**. ForecastEx vs Polymarket used to be the candidate set on the cities
sharing an airport station, but Polymarket now settles on NWS WRH while the
registry's ForecastEx rows still say Weather Underground on the strength of a
2026-07-22 pass. Those are not known to be the same measurement, so every pair
classifies as BASIS. Empirically the two agreed on 192/192 WU-era city-days, so
the pair is *plausibly* still lockable -- but that is a reason to go pull
ForecastEx's live contract terms, not a reason for this table to assert it.

What this module is
-------------------
A static, verifiable registry + classifier (offline, like :mod:`verify_mappings`
but across venues), plus pure helpers for the cross-book lock math so a live
quote feed can be layered on later. It does **not** trade and is entirely
reference-only; it never touches the Kalshi order path.

``verified=True`` means the mapping has been checked against that venue's *live*
settlement rules or its own settled outcomes. Treat ``ARB_CANDIDATE`` as "same
measurement, still confirm identical bucket edges + both stations before you
believe the lock", not as a green light.

CLI::

    python -m weather_arb.cross_venue [--scope us|global] [--json]
"""

from __future__ import annotations

import argparse
import enum
import json
from dataclasses import dataclass
from typing import Optional

from .config import Settings
from .fees import fee_cents
from .models import Venue


class Jurisdiction(str, enum.Enum):
    US = "us"
    UK_EU = "uk_eu"


class VenueScope(str, enum.Enum):
    """CLI/`WA_VENUE_SCOPE` toggle: US-only venues, or add UK/EU exchanges."""

    US = "us"
    GLOBAL = "global"

    def includes(self, j: Jurisdiction) -> bool:
        return self is VenueScope.GLOBAL or j is Jurisdiction.US


class DataSource(str, enum.Enum):
    NWS_CLI = "nws_cli"        # next-day NWS Climate Report (quality-controlled)
    NWS_WRH = "nws_wrh"        # NWS WRH "Hourly Data" timeseries (Synoptic-backed):
                               # routine METARs + SPECIs, MADIS sub-hourly rows
                               # excluded, Temp column = air_temp rounded half-up
                               # to a whole degF.  See weather_arb.polymarket_basis.
    WU_METAR = "wu_metar"      # Weather Underground daily table (hourly-METAR maxima)
    NOVELTY = "novelty"        # bookmaker novelty market (no daily-high bucket)
    NOT_LISTED = "not_listed"  # venue runs no daily-high market for this city


# The sources that actually settle a daily-high bucket.  NOVELTY and NOT_LISTED
# do not, so a pair involving one of them is PairClass.NONE rather than a basis.
DAILY_HIGH_SOURCES: frozenset[DataSource] = frozenset(
    {DataSource.NWS_CLI, DataSource.NWS_WRH, DataSource.WU_METAR}
)


class DayWindow(str, enum.Enum):
    LST = "lst"                # Local Standard Time year-round (no DST) -- Kalshi
    LOCAL = "local"            # the local CIVIL day: DST-aware wall clock, which is
                               # what Polymarket's feed returns (obtimezone=local)
                               # and what "all times on this day" means for it.
                               # Deliberately NOT split into a separate CIVIL member:
                               # LOCAL already denotes exactly this, and a synonym
                               # would make identical rows compare as different.
    NA = "na"


class PairClass(str, enum.Enum):
    ARB_CONFIRMED = "arb_confirmed"    # identical definition, both sides verified
    ARB_CANDIDATE = "arb_candidate"    # identical definition, station(s) unverified
    BASIS = "basis"                    # same city, differs on >=1 axis (rel-value)
    NONE = "none"                      # no comparable daily-high market on a side


# --------------------------------------------------------------------------- #
# Venue registry
# --------------------------------------------------------------------------- #
@dataclass(frozen=True, slots=True)
class VenueInfo:
    id: str
    label: str
    jurisdiction: Jurisdiction
    regulator: str
    us_accessible: bool
    fee_note: str
    access_note: str


VENUES: tuple[VenueInfo, ...] = (
    VenueInfo(Venue.KALSHI.value, "Kalshi", Jurisdiction.US, "CFTC (DCM)", True,
              "trade fee = 0.07*C*P*(1-P), rounded up (near-zero at 98c)",
              "your live venue"),
    VenueInfo(Venue.FORECASTEX.value, "IBKR ForecastEx", Jurisdiction.US, "CFTC (DCM/DCO)", True,
              "per-contract commission (~1c/side, tier-dependent) -- confirm",
              "via Interactive Brokers; CFTC-regulated"),
    VenueInfo(Venue.POLYMARKET.value, "Polymarket (US: QCEX)", Jurisdiction.US, "CFTC via QCX/QCEX", True,
              "no per-trade fee; cost is the bid/ask spread + gas",
              "US access via QCEX/Polymarket US (fully collateralized)"),
    VenueInfo(Venue.BETFAIR.value, "Betfair Exchange", Jurisdiction.UK_EU, "UK Gambling Commission", False,
              "commission on net winnings (~2-5%), not per-contract",
              "UK/EU only; novelty weather books, not US city highs"),
    VenueInfo(Venue.SMARKETS.value, "Smarkets", Jurisdiction.UK_EU, "UK GC / Malta", False,
              "commission on net winnings (~2%)",
              "UK/EU only; novelty weather books, not US city highs"),
)

VENUE_BY_ID: dict[str, VenueInfo] = {v.id: v for v in VENUES}


# --------------------------------------------------------------------------- #
# Per-venue, per-city settlement mapping (the verifiable table)
# --------------------------------------------------------------------------- #
@dataclass(frozen=True, slots=True)
class VenueStation:
    """How one venue settles one city's daily high, over one span of time.

    ``station`` is the reference ICAO (or a descriptor for novelty books, or ``""``
    when the venue lists no such market); ``rounding_f`` is the settlement
    granularity in degrees F (1.0 = whole degree). ``verified`` mirrors
    :mod:`verify_mappings`: True only when the mapping has been checked against
    that venue's *live* settlement rules or its own settled outcomes.

    A row is **historical** once ``superseded_on`` is set, and is then excluded
    from :func:`stations_for` and from pair classification.  Superseded rows are
    kept rather than deleted: a venue that changed its settlement source once can
    change it again, and the fact that it did is itself the finding.  Both dates
    are ISO ``YYYY-MM-DD``; ``effective_from`` is the first day this mapping is
    known to hold, ``superseded_on`` the first day it no longer does.
    """

    venue: str
    city: str
    station: str
    data_source: DataSource
    day_window: DayWindow
    rounding_f: float
    verified: bool
    note: str = ""
    effective_from: str = ""
    superseded_on: str = ""

    @property
    def is_current(self) -> bool:
        return not self.superseded_on

    @property
    def offers_daily_high(self) -> bool:
        return bool(self.station) and self.data_source in DAILY_HIGH_SOURCES


# Canonical cities. The US seven the tracker already trades, plus London to show
# where the UK/EU person's venues actually overlap anything (Polymarket, not the
# US books).
CITIES: tuple[tuple[str, str], ...] = (
    ("NYC", "New York"), ("CHI", "Chicago"), ("MIA", "Miami"), ("AUS", "Austin"),
    ("DEN", "Denver"), ("LAX", "Los Angeles"), ("PHIL", "Philadelphia"),
    ("LON", "London"),
)

_K = Venue.KALSHI.value
_F = Venue.FORECASTEX.value
_P = Venue.POLYMARKET.value
_B = Venue.BETFAIR.value
_S = Venue.SMARKETS.value
# Provenance, per venue:
#
#   Kalshi     -- verified continuously by verify_mappings against live rules.
#   Polymarket -- OBSERVED 2026-09-08 by weather_arb.polymarket_basis: the basis was
#                 scored against Polymarket's OWN resolved payouts (281/282 = 99.6%
#                 over 282 city-days) and each station is read off that event's own
#                 `resolutionSource`, in BOTH eras.  Not inferred, not spot-checked.
#   ForecastEx -- read once on 2026-07-22 and NOT re-checked.  Polymarket changed
#                 its source under us inside this very window, so a July reading is
#                 no longer evidence about today; these rows are verified=False.
#
# The Polymarket WU-era rows are retained below with `superseded_on` set.  Note the
# two corrections are different in kind: the SOURCE (WU) was correct and went stale
# on ~2026-08-21, whereas the DEN STATION (KDEN) was never right -- resolutionSource
# names KBKF in the WU era too.
_FE_AIRPORT = ("main-airport station (inferred); confirm in ForecastEx contract terms")
_FE_STALE = ("source read as Weather Underground on 2026-07-22 and NOT re-checked; "
             "Polymarket switched source inside this window, so treat as stale")
_PM_OBS = ("station + basis OBSERVED via polymarket_basis (payout-scored, 281/282); "
           "WRH 'Hourly Data' = routine METAR + SPECI, half-up to whole degF")
_PM_WU_ERA = ("superseded: Polymarket settled on the Weather Underground daily table "
              "until ~2026-08-21, then switched to the NWS WRH timeseries")
_WRH_ERA = "2026-08-21"      # first day the WRH source is observed in resolutionSource
_WU_ERA = "2026-07-22"       # first day of available Polymarket history

STATION_MAP: tuple[VenueStation, ...] = (
    # ---- New York ---- Kalshi = Central Park; Polymarket = LaGuardia (warmer) ----
    VenueStation(_K, "NYC", "KNYC", DataSource.NWS_CLI, DayWindow.LST, 0.0, True, "Central Park (verified)"),
    VenueStation(_F, "NYC", "KNYC", DataSource.WU_METAR, DayWindow.LOCAL, 1.0, False,
                 "Central Park, confirmed via FE contract terms 2026-07-22; " + _FE_STALE),
    VenueStation(_P, "NYC", "KLGA", DataSource.NWS_WRH, DayWindow.LOCAL, 1.0, True,
                 "LaGuardia -- " + _PM_OBS, _WRH_ERA),
    # ---- Chicago ---- Kalshi = Midway; Polymarket = O'Hare ----
    VenueStation(_K, "CHI", "KMDW", DataSource.NWS_CLI, DayWindow.LST, 0.0, True, "Midway (verified)"),
    VenueStation(_F, "CHI", "KMDW", DataSource.WU_METAR, DayWindow.LOCAL, 1.0, False,
                 "Midway KMDW, confirmed via FE contract text 2026-07-22; " + _FE_STALE),
    VenueStation(_P, "CHI", "KORD", DataSource.NWS_WRH, DayWindow.LOCAL, 1.0, True,
                 "O'Hare -- " + _PM_OBS, _WRH_ERA),
    # ---- Miami / Austin / LAX: Kalshi and Polymarket share the STATION, but no
    #      longer the source, so these are basis rather than arb candidates ----
    VenueStation(_K, "MIA", "KMIA", DataSource.NWS_CLI, DayWindow.LST, 0.0, True),
    VenueStation(_F, "MIA", "KMIA", DataSource.WU_METAR, DayWindow.LOCAL, 1.0, False, _FE_AIRPORT + "; " + _FE_STALE),
    VenueStation(_P, "MIA", "KMIA", DataSource.NWS_WRH, DayWindow.LOCAL, 1.0, True, _PM_OBS, _WRH_ERA),
    VenueStation(_K, "AUS", "KAUS", DataSource.NWS_CLI, DayWindow.LST, 0.0, True),
    VenueStation(_F, "AUS", "KAUS", DataSource.WU_METAR, DayWindow.LOCAL, 1.0, False, _FE_AIRPORT + "; " + _FE_STALE),
    VenueStation(_P, "AUS", "KAUS", DataSource.NWS_WRH, DayWindow.LOCAL, 1.0, True, _PM_OBS, _WRH_ERA),
    VenueStation(_K, "LAX", "KLAX", DataSource.NWS_CLI, DayWindow.LST, 0.0, True),
    VenueStation(_F, "LAX", "KLAX", DataSource.WU_METAR, DayWindow.LOCAL, 1.0, False, _FE_AIRPORT + "; " + _FE_STALE),
    VenueStation(_P, "LAX", "KLAX", DataSource.NWS_WRH, DayWindow.LOCAL, 1.0, True, _PM_OBS, _WRH_ERA),
    # ---- Denver ---- NOT a shared station: Polymarket settles at Buckley SFB ----
    VenueStation(_K, "DEN", "KDEN", DataSource.NWS_CLI, DayWindow.LST, 0.0, True, "Denver Intl"),
    VenueStation(_F, "DEN", "KDEN", DataSource.WU_METAR, DayWindow.LOCAL, 1.0, False, _FE_AIRPORT + "; " + _FE_STALE),
    VenueStation(_P, "DEN", "KBKF", DataSource.NWS_WRH, DayWindow.LOCAL, 1.0, True,
                 "Buckley Space Force Base, ~20mi from KDEN -- " + _PM_OBS
                 + "; this table previously said KDEN, which was never correct",
                 _WRH_ERA),
    # ---- Philadelphia ---- Kalshi and ForecastEx only; Polymarket lists no market ----
    VenueStation(_K, "PHIL", "KPHL", DataSource.NWS_CLI, DayWindow.LST, 0.0, True),
    VenueStation(_F, "PHIL", "KPHL", DataSource.WU_METAR, DayWindow.LOCAL, 1.0, False, _FE_AIRPORT + "; " + _FE_STALE),
    VenueStation(_P, "PHIL", "", DataSource.NOT_LISTED, DayWindow.NA, 0.0, True,
                 "Polymarket lists NO Philadelphia daily-high market -- checked across "
                 "both eras via the Gamma API (2026-07-22..2026-09-07); nothing to lock against",
                 _WU_ERA),
    # ---- London: only the global venues; Polymarket bins vs bookmaker novelty ----
    VenueStation(_P, "LON", "EGLC", DataSource.WU_METAR, DayWindow.LOCAL, 1.0, False,
                 "London City -- NOT re-checked in the 2026-09-08 pass, which covered "
                 "US cities only; source may have switched to WRH like the US rows"),
    VenueStation(_B, "LON", "UK-novelty", DataSource.NOVELTY, DayWindow.NA, 0.0, False,
                 "novelty weather books (hottest day / records) -- not a daily-high bucket"),
    VenueStation(_S, "LON", "UK-novelty", DataSource.NOVELTY, DayWindow.NA, 0.0, False,
                 "novelty weather books -- not a daily-high bucket"),

    # ---- SUPERSEDED (kept as history; excluded from stations_for/classify_all) ----
    # Polymarket's Weather-Underground era.  The stations are IDENTICAL to the rows
    # above in every city -- only the source changed -- which is how we know the
    # KDEN entry was an error rather than an out-of-date fact.
    VenueStation(_P, "NYC", "KLGA", DataSource.WU_METAR, DayWindow.LOCAL, 1.0, True,
                 _PM_WU_ERA, _WU_ERA, _WRH_ERA),
    VenueStation(_P, "CHI", "KORD", DataSource.WU_METAR, DayWindow.LOCAL, 1.0, True,
                 _PM_WU_ERA, _WU_ERA, _WRH_ERA),
    VenueStation(_P, "MIA", "KMIA", DataSource.WU_METAR, DayWindow.LOCAL, 1.0, True,
                 _PM_WU_ERA, _WU_ERA, _WRH_ERA),
    VenueStation(_P, "AUS", "KAUS", DataSource.WU_METAR, DayWindow.LOCAL, 1.0, True,
                 _PM_WU_ERA, _WU_ERA, _WRH_ERA),
    VenueStation(_P, "LAX", "KLAX", DataSource.WU_METAR, DayWindow.LOCAL, 1.0, True,
                 _PM_WU_ERA, _WU_ERA, _WRH_ERA),
    VenueStation(_P, "DEN", "KBKF", DataSource.WU_METAR, DayWindow.LOCAL, 1.0, True,
                 "Buckley SFB even in the WU era (resolutionSource: .../us/co/aurora/KBKF) "
                 "-- " + _PM_WU_ERA, _WU_ERA, _WRH_ERA),
)


def _venue_order() -> dict[str, int]:
    return {v.id: i for i, v in enumerate(VENUES)}


def stations_for(city: str, scope: VenueScope) -> list[VenueStation]:
    """The **currently effective** in-scope venue mappings for a city.

    Superseded rows are excluded: classifying against a mapping the venue has
    already moved off would be worse than having no row at all.  Reach them with
    :func:`history_for`.
    """
    order = _venue_order()
    rows = [
        s for s in STATION_MAP
        if s.city == city and s.is_current
        and scope.includes(VENUE_BY_ID[s.venue].jurisdiction)
    ]
    return sorted(rows, key=lambda s: order[s.venue])


def history_for(city: str, scope: VenueScope) -> list[VenueStation]:
    """Superseded mappings for a city -- what a venue used to settle on."""
    order = _venue_order()
    rows = [
        s for s in STATION_MAP
        if s.city == city and not s.is_current
        and scope.includes(VENUE_BY_ID[s.venue].jurisdiction)
    ]
    return sorted(rows, key=lambda s: (order[s.venue], s.superseded_on))


# --------------------------------------------------------------------------- #
# Classification
# --------------------------------------------------------------------------- #
def classify_pair(a: VenueStation, b: VenueStation) -> tuple[PairClass, list[str]]:
    """Classify two venues' settlement of the same city.

    Returns ``(class, reasons)``. ``reasons`` lists the axes that differ (for
    BASIS) or the caveats to clear (for an ARB_* candidate).
    """
    if not a.offers_daily_high or not b.offers_daily_high:
        return PairClass.NONE, ["one side has no comparable daily-high bucket"]

    diffs: list[str] = []
    if a.station != b.station:
        diffs.append(f"station {a.station} != {b.station}")
    if a.data_source != b.data_source:
        diffs.append(f"source {a.data_source.value} != {b.data_source.value}")
    if a.day_window != b.day_window:
        diffs.append(f"day-window {a.day_window.value} != {b.day_window.value}")
    if a.rounding_f != b.rounding_f:
        diffs.append(f"rounding {a.rounding_f:g}F != {b.rounding_f:g}F")
    if diffs:
        return PairClass.BASIS, diffs

    caveat = ["confirm identical bucket edges before locking"]
    if a.verified and b.verified:
        return PairClass.ARB_CONFIRMED, caveat
    return PairClass.ARB_CANDIDATE, caveat + ["station(s) not yet verified vs live rules"]


@dataclass(frozen=True, slots=True)
class PairVerdict:
    city: str
    venue_a: str
    venue_b: str
    klass: PairClass
    reasons: tuple[str, ...]


def classify_all(scope: VenueScope) -> list[PairVerdict]:
    """Every in-scope venue pair, per city, classified."""
    out: list[PairVerdict] = []
    for city, _name in CITIES:
        rows = stations_for(city, scope)
        for i in range(len(rows)):
            for j in range(i + 1, len(rows)):
                klass, reasons = classify_pair(rows[i], rows[j])
                out.append(PairVerdict(city, rows[i].venue, rows[j].venue, klass, tuple(reasons)))
    return out


# --------------------------------------------------------------------------- #
# Cross-book lock math (pure; ready for a live quote feed)
# --------------------------------------------------------------------------- #
@dataclass(frozen=True, slots=True)
class Quote:
    """A one-sided top-of-book snapshot for one contract on one venue.

    Prices are integer cents in [1, 99]; ``yes_ask`` is the cost to *buy* YES,
    ``no_ask`` the cost to *buy* NO. ``size`` is the fillable contract count.
    """

    venue: str
    city: str
    ticker: str
    yes_ask: int
    no_ask: int
    size: int


def est_fee_cents(venue: str, price_cents: int, count: int, settings: Optional[Settings] = None) -> int:
    """Estimated taker fee (whole cents) for a fill on a given venue."""
    if venue == Venue.KALSHI.value:
        coeff = settings.kalshi_fee_coefficient if settings else 0.07
        return fee_cents(price_cents, count, coeff)
    if venue == Venue.FORECASTEX.value:
        per = settings.forecastex_fee_cents_per_contract if settings else 1
        return per * count
    # Polymarket: no per-trade fee (cost is the spread, already in the ask).
    return 0


def cross_book_lock_cents(
    yes_ask_a: int, no_ask_b: int,
    venue_a: str, venue_b: str, count: int = 1,
    settings: Optional[Settings] = None,
) -> int:
    """Net locked profit (cents) from buying YES on A and NO on B for the same
    contract. Positive => arbitrage after fees. (Buying both sides guarantees a
    $1 payout on exactly one, so cost < $1 net of fees is a lock.)
    """
    gross = (100 - (yes_ask_a + no_ask_b)) * count
    fees = est_fee_cents(venue_a, yes_ask_a, count, settings) + \
        est_fee_cents(venue_b, no_ask_b, count, settings)
    return gross - fees


def scan_quotes(
    quotes: dict[tuple[str, str], Quote], scope: VenueScope,
    settings: Optional[Settings] = None,
) -> list[dict]:
    """Find lockable cross-book arbitrage among ARB_* eligible venue pairs.

    ``quotes`` is keyed by ``(venue_id, city)``. Only pairs the classifier deems
    same-underlying are considered (Kalshi-vs-other is basis, never a lock). Both
    lock directions are evaluated; net-positive opportunities are returned
    sorted by profit. Caller must ensure the two quotes are the *same bucket*.
    """
    opps: list[dict] = []
    for v in classify_all(scope):
        if v.klass not in (PairClass.ARB_CONFIRMED, PairClass.ARB_CANDIDATE):
            continue
        qa = quotes.get((v.venue_a, v.city))
        qb = quotes.get((v.venue_b, v.city))
        if qa is None or qb is None:
            continue
        count = min(qa.size, qb.size)
        if count <= 0:
            continue
        # Two constructions: YES@a+NO@b, or NO@a+YES@b.
        p1 = cross_book_lock_cents(qa.yes_ask, qb.no_ask, v.venue_a, v.venue_b, count, settings)
        p2 = cross_book_lock_cents(qb.yes_ask, qa.no_ask, v.venue_b, v.venue_a, count, settings)
        best, legs = (p1, f"buy YES@{v.venue_a} + NO@{v.venue_b}") if p1 >= p2 \
            else (p2, f"buy YES@{v.venue_b} + NO@{v.venue_a}")
        if best > 0:
            opps.append({
                "city": v.city, "pair": f"{v.venue_a}<->{v.venue_b}",
                "klass": v.klass.value, "legs": legs,
                "count": count, "net_profit_cents": best,
            })
    opps.sort(key=lambda o: -o["net_profit_cents"])
    return opps


# --------------------------------------------------------------------------- #
# Rendering / CLI
# --------------------------------------------------------------------------- #
_KLASS_TAG = {
    PairClass.ARB_CONFIRMED: "ARB!", PairClass.ARB_CANDIDATE: "ARB?",
    PairClass.BASIS: "basis", PairClass.NONE: "----",
}


def _render(scope: VenueScope) -> str:
    out: list[str] = []
    out.append("=" * 74)
    out.append(f"  CROSS-VENUE BASIS MONITOR        scope = {scope.value.upper()}")
    out.append("  same weather bet, different exchanges -- is it really the same underlying?")
    out.append("=" * 74)

    # Venue roster in scope.
    out.append("")
    out.append("VENUES IN SCOPE:")
    for v in VENUES:
        if not scope.includes(v.jurisdiction):
            continue
        flag = "US-ok" if v.us_accessible else "UK/EU"
        out.append(f"  * {v.label:<24} [{flag}] {v.regulator}")
        out.append(f"      {v.access_note}")

    # Per-city mapping + pair classification.
    verdicts = classify_all(scope)
    for city, name in CITIES:
        rows = stations_for(city, scope)
        if not rows:
            continue
        out.append("")
        out.append(f"{name} ({city}):")
        for s in rows:
            vf = "verified" if s.verified else "UNVERIFIED"
            src = s.data_source.value
            win = s.day_window.value
            extra = f"  -- {s.note}" if s.note else ""
            out.append(f"    {VENUE_BY_ID[s.venue].label:<24} {(s.station or '(none)'):<11} "
                       f"{src:<10} {win:<5} [{vf}]{extra}")
        for h in history_for(city, scope):
            out.append(f"      (was) {VENUE_BY_ID[h.venue].label:<18} {(h.station or '(none)'):<11} "
                       f"{h.data_source.value:<10} {h.day_window.value:<5} "
                       f"[until {h.superseded_on}]  -- {h.note}")
        for v in (x for x in verdicts if x.city == city):
            la = VENUE_BY_ID[v.venue_a].label
            lb = VENUE_BY_ID[v.venue_b].label
            tag = _KLASS_TAG[v.klass]
            why = ("; ".join(v.reasons)) if v.reasons else ""
            out.append(f"      [{tag}] {la} <-> {lb}: {why}")

    # Summary: the hunt list.
    cand = [v for v in verdicts if v.klass in (PairClass.ARB_CONFIRMED, PairClass.ARB_CANDIDATE)]
    out.append("")
    out.append("-" * 74)
    out.append(f"  LOCKABLE-ARB CANDIDATES ({len(cand)}):  same station+source+window+rounding")
    if cand:
        for v in cand:
            out.append(f"    - {v.city}: {VENUE_BY_ID[v.venue_a].label} <-> {VENUE_BY_ID[v.venue_b].label}"
                       f"  ({_KLASS_TAG[v.klass]})")
        out.append("  Next: pull each venue's LIVE rules to confirm station + bucket edges,")
        out.append("  then diff live quotes -- yes_ask(A) + no_ask(B) < 100c (net fees) locks it.")
    else:
        out.append("    (none in this scope)")
        out.append("  The ForecastEx<->Polymarket set emptied on 2026-09-08: Polymarket is")
        out.append("  OBSERVED on the NWS WRH hourly timeseries, while ForecastEx's source is")
        out.append("  only a 2026-07-22 reading of Weather Underground. WU and WRH agreed on")
        out.append("  192/192 WU-era city-days, so the pair may well still lock -- but that is")
        out.append("  a reason to pull ForecastEx's live contract terms, not to assert it here.")
    out.append("  Kalshi is basis-only vs every venue (NWS-CLI source + LST day-window);")
    out.append("  trade those as relative-value/model edge, not as a riskless lock.")
    out.append("=" * 74)
    return "\n".join(out)


def _as_json(scope: VenueScope) -> str:
    verdicts = classify_all(scope)
    return json.dumps({
        "scope": scope.value,
        "venues": [v.id for v in VENUES if scope.includes(v.jurisdiction)],
        "mappings": [
            {"city": s.city, "venue": s.venue, "station": s.station,
             "data_source": s.data_source.value, "day_window": s.day_window.value,
             "rounding_f": s.rounding_f, "verified": s.verified, "note": s.note,
             "effective_from": s.effective_from, "superseded_on": s.superseded_on,
             "current": s.is_current}
            for s in STATION_MAP if scope.includes(VENUE_BY_ID[s.venue].jurisdiction)
        ],
        "pairs": [
            {"city": v.city, "venue_a": v.venue_a, "venue_b": v.venue_b,
             "class": v.klass.value, "reasons": list(v.reasons)}
            for v in verdicts
        ],
    }, indent=2)


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Cross-venue basis monitor: is the same weather bet the same "
                    "underlying across exchanges, and where can it lock?"
    )
    parser.add_argument("--scope", choices=[s.value for s in VenueScope], default=None,
                        help="us (default) or global (adds UK/EU exchanges); "
                             "overrides WA_VENUE_SCOPE")
    parser.add_argument("--json", action="store_true", help="emit JSON")
    args = parser.parse_args(argv)

    settings = Settings.from_env()
    raw = args.scope or settings.venue_scope
    try:
        scope = VenueScope(raw)
    except ValueError:
        scope = VenueScope.US

    print(_as_json(scope) if args.json else _render(scope))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
