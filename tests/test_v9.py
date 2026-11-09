"""Cross-venue basis monitor: scope toggle, pair classification, lock math."""
import asyncio, sys
import os as _osp; sys.path.insert(0, _osp.path.dirname(_osp.path.dirname(_osp.path.abspath(__file__))))
from weather_arb.config import Settings
from weather_arb.models import Venue
from weather_arb import cross_venue as cv
from weather_arb.cross_venue import (
    VenueScope, PairClass, Quote, classify_all, stations_for, history_for,
    cross_book_lock_cents, est_fee_cents, scan_quotes,
    DataSource, DayWindow, VenueStation,
)

failures = []
def check(n, c):
    print(("PASS" if c else "FAIL"), "-", n)
    if not c: failures.append(n)


def _pair(city, a, b, scope=VenueScope.US):
    for v in classify_all(scope):
        if v.city == city and {v.venue_a, v.venue_b} == {a, b}:
            return v
    return None


def test_scope_toggle():
    us = stations_for("LON", VenueScope.US)
    glob = stations_for("LON", VenueScope.GLOBAL)
    check("US scope excludes UK/EU books at London", all(s.venue == Venue.POLYMARKET.value for s in us))
    check("global scope adds Betfair + Smarkets", {Venue.BETFAIR.value, Venue.SMARKETS.value} <=
          {s.venue for s in glob})
    # No UK/EU venue ever leaks into a US-scope city.
    us_venues = {s.venue for c, _ in cv.CITIES for s in stations_for(c, VenueScope.US)}
    check("US scope never surfaces Betfair/Smarkets",
          Venue.BETFAIR.value not in us_venues and Venue.SMARKETS.value not in us_venues)


def test_classification():
    K, F, P = Venue.KALSHI.value, Venue.FORECASTEX.value, Venue.POLYMARKET.value
    # Kalshi differs from everyone on source + day-window => always basis.
    check("Kalshi<->ForecastEx (MIA) is basis", _pair("MIA", K, F).klass is PairClass.BASIS)
    check("Kalshi<->Polymarket (MIA) is basis", _pair("MIA", K, P).klass is PairClass.BASIS)
    # ForecastEx<->Polymarket used to be an arb candidate on the shared-airport
    # cities.  It is not any more: Polymarket is OBSERVED on the NWS WRH hourly
    # timeseries (polymarket_basis, 281/282 vs its own payouts) while ForecastEx's
    # source is a 2026-07-22 reading of Weather Underground.  Different source =>
    # basis, and the reason must SAY source so nobody reads it as a station issue.
    mia = _pair("MIA", F, P)
    check("ForecastEx<->Polymarket (MIA) is basis (source)", mia.klass is PairClass.BASIS)
    check("MIA basis reason cites source", any("source" in r for r in mia.reasons))
    check("MIA basis reason does NOT cite station (they share KMIA)",
          not any("station" in r for r in mia.reasons))
    # NYC / CHI / DEN are the station outliers: Polymarket uses LaGuardia, O'Hare
    # and Buckley SFB against Kalshi's Central Park, Midway and Denver Intl.
    for city, note in (("NYC", "KLGA"), ("CHI", "KORD"), ("DEN", "KBKF")):
        v = _pair(city, K, P)
        check(f"Kalshi<->Polymarket ({city}) is basis (station {note})",
              v.klass is PairClass.BASIS and any("station" in r for r in v.reasons))
    # Denver specifically: the registry used to claim KDEN, which was never right.
    den_pm = [s for s in stations_for("DEN", VenueScope.US) if s.venue == P]
    check("Polymarket DEN station is KBKF", den_pm and den_pm[0].station == "KBKF")
    check("Polymarket DEN source is NWS WRH", den_pm and den_pm[0].data_source is DataSource.NWS_WRH)
    check("Polymarket DEN is verified (payout-scored)", den_pm and den_pm[0].verified)
    # Philadelphia: Polymarket lists no market at all, so every pair with it is NONE
    # -- NOT a basis, which would imply there is something on the other side to price.
    phil_pm = [s for s in stations_for("PHIL", VenueScope.US) if s.venue == P]
    check("Polymarket PHIL is NOT_LISTED", phil_pm and phil_pm[0].data_source is DataSource.NOT_LISTED)
    check("PHIL has no station", phil_pm and phil_pm[0].station == "")
    check("Kalshi<->Polymarket (PHIL) is NONE", _pair("PHIL", K, P).klass is PairClass.NONE)
    check("ForecastEx<->Polymarket (PHIL) is NONE", _pair("PHIL", F, P).klass is PairClass.NONE)
    # There is currently NO lockable pair anywhere in scope.  This is the headline
    # consequence of the 2026-09-08 registry fix and it must not regress silently.
    cand_cities = {v.city for v in classify_all(VenueScope.US)
                   if v.klass in (PairClass.ARB_CONFIRMED, PairClass.ARB_CANDIDATE)}
    check("no lockable-arb candidates remain", cand_cities == set())
    # London: Polymarket vs a novelty book has no comparable bucket.
    lon = _pair("LON", P, Venue.BETFAIR.value, VenueScope.GLOBAL)
    check("Polymarket<->Betfair (LON) has no comparable market", lon.klass is PairClass.NONE)


def test_superseded_history_is_kept_but_not_classified():
    """The WU->WRH switch is history, not an overwrite -- and history never trades."""
    P = Venue.POLYMARKET.value
    cur = {s.city: s for s in
           (x for c, _ in cv.CITIES for x in stations_for(c, VenueScope.US)) if s.venue == P}
    hist = {c: history_for(c, VenueScope.US) for c, _ in cv.CITIES}
    check("Polymarket WU era retained for the six listed cities",
          {c for c, rows in hist.items() if any(r.venue == P for r in rows)}
          == {"NYC", "CHI", "MIA", "AUS", "DEN", "LAX"})
    check("every superseded row has both dates",
          all(r.effective_from and r.superseded_on
              for rows in hist.values() for r in rows))
    check("superseded rows are not current", all(not r.is_current
                                                 for rows in hist.values() for r in rows))
    # The station is IDENTICAL across the two eras everywhere -- that is precisely
    # how we know DEN=KDEN was an error rather than a stale-but-once-true value.
    same = all(cur[c].station == r.station
               for c, rows in hist.items() for r in rows if r.venue == P and c in cur)
    check("station unchanged across the source switch (only the source moved)", same)
    check("history says WU, current says WRH",
          all(r.data_source is DataSource.WU_METAR for rows in hist.values()
              for r in rows if r.venue == P)
          and all(cur[c].data_source is DataSource.NWS_WRH for c in
                  ("NYC", "CHI", "MIA", "AUS", "DEN", "LAX")))
    # A superseded row must never reach the classifier.
    check("stations_for excludes history",
          all(s.is_current for c, _ in cv.CITIES for s in stations_for(c, VenueScope.GLOBAL)))


def test_lock_math():
    F, P = Venue.FORECASTEX.value, Venue.POLYMARKET.value
    s = Settings()
    # Polymarket has no per-trade fee; ForecastEx charges per contract/side.
    check("polymarket fee == 0", est_fee_cents(P, 50, 10, s) == 0)
    check("forecastex fee == per*count", est_fee_cents(F, 50, 10, s) == 10 * s.forecastex_fee_cents_per_contract)
    # YES@40 (ForecastEx) + NO@47 (Polymarket) = 87c -> 13c gross, minus 1c/ct FE fee.
    net = cross_book_lock_cents(40, 47, F, P, count=1, settings=s)
    check("lock nets 13 - 1 = 12c", net == 12)
    # Sum >= 100 never locks.
    check("no lock when legs sum to 100", cross_book_lock_cents(55, 45, F, P, count=1, settings=s) < 0)


def test_scan_only_scans_eligible_pairs():
    K, F, P = Venue.KALSHI.value, Venue.FORECASTEX.value, Venue.POLYMARKET.value
    s = Settings()
    quotes = {
        (F, "MIA"): Quote(F, "MIA", "FE-MIA", yes_ask=40, no_ask=62, size=100),
        (P, "MIA"): Quote(P, "MIA", "PM-MIA", yes_ask=55, no_ask=47, size=80),
        # A Kalshi quote that *looks* lockable against Polymarket -- must be ignored
        # because Kalshi<->Polymarket is basis, not the same underlying.
        (K, "MIA"): Quote(K, "MIA", "K-MIA", yes_ask=30, no_ask=30, size=100),
    }
    # Against the REAL registry there is no same-underlying pair left, so a book
    # that looks locked must produce nothing.  Two prices summing under 100c across
    # venues that settle on different definitions is not an arbitrage.
    check("real registry yields no locks (no same-underlying pair exists)",
          scan_quotes(quotes, VenueScope.US, s) == [])

    # The lock math itself still has to work, so exercise it against a synthetic
    # registry where two venues genuinely DO match on every axis.
    real = cv.STATION_MAP
    try:
        cv.STATION_MAP = tuple(
            [r for r in real if not (r.city == "MIA" and r.venue in (F, P))]
            + [VenueStation(F, "MIA", "KMIA", DataSource.NWS_WRH, DayWindow.LOCAL, 1.0, True),
               VenueStation(P, "MIA", "KMIA", DataSource.NWS_WRH, DayWindow.LOCAL, 1.0, True)]
        )
        opps = scan_quotes(quotes, VenueScope.US, s)
        check("finds exactly one lock", len(opps) == 1)
        check("lock is the ForecastEx<->Polymarket pair",
              opps and "forecastex<->polymarket" == opps[0]["pair"])
        check("never locks a Kalshi (basis) pair", all("kalshi" not in o["pair"] for o in opps))
        # min(100,80)=80 contracts * (13 gross - 1 fee) = 960c.
        check("net profit sized to min book (80 ct * 12c)",
              opps and opps[0]["net_profit_cents"] == 80 * 12)
    finally:
        cv.STATION_MAP = real
    check("registry restored after the synthetic scan", cv.STATION_MAP is real)


async def main():
    test_scope_toggle(); test_classification(); test_lock_math()
    test_superseded_history_is_kept_but_not_classified()
    test_scan_only_scans_eligible_pairs()
    print("\n" + ("ALL PASS" if not failures else f"FAILURES: {failures}"))
    sys.exit(1 if failures else 0)

asyncio.run(main())
