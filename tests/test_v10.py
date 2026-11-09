"""Cross-venue live quote clients: bucket parsing, Polymarket parse, lock scan."""
import asyncio, sys
from datetime import date
import os as _osp; sys.path.insert(0, _osp.path.dirname(_osp.path.dirname(_osp.path.abspath(__file__))))
from weather_arb.config import Settings
from weather_arb.models import Venue
from weather_arb import cross_venue as _cv
from weather_arb.cross_venue import VenueScope
from weather_arb.cross_venue_quotes import (
    parse_bucket, _ask_cents, _no_ask_from_bid, BucketQuote,
    PolymarketQuoteClient, ForecastExQuoteClient, KalshiQuoteClient,
    kalshi_event_date, scan_bucket_locks,
)
from datetime import date


# The registry no longer contains ANY same-underlying venue pair (see test_v9 and
# cross_venue's 2026-09-08 provenance note), so the bucket-lock scanner correctly
# finds nothing against it.  The scanner's own math still has to be covered, so
# these tests run it against a synthetic registry in which two venues genuinely do
# match on every axis.  Patching is scoped and restored.
class _synthetic_arb_pair:
    """Temporarily make ForecastEx and Polymarket same-underlying at ``city``."""

    def __init__(self, city):
        self.city = city

    def __enter__(self):
        self._real = _cv.STATION_MAP
        keep = [r for r in self._real
                if not (r.city == self.city and r.venue in (_F, _P))]
        _cv.STATION_MAP = tuple(keep + [
            _cv.VenueStation(_F, self.city, "SYNTH", _cv.DataSource.NWS_WRH,
                             _cv.DayWindow.LOCAL, 1.0, True),
            _cv.VenueStation(_P, self.city, "SYNTH", _cv.DataSource.NWS_WRH,
                             _cv.DayWindow.LOCAL, 1.0, True),
        ])
        return self

    def __exit__(self, *exc):
        _cv.STATION_MAP = self._real
        return False

failures = []
def check(n, c):
    print(("PASS" if c else "FAIL"), "-", n)
    if not c: failures.append(n)

_P, _F = Venue.POLYMARKET.value, Venue.FORECASTEX.value

# A stubbed Gamma event (shape mirrors the real API: YES-token bestBid/bestAsk).
EVENT = {"markets": [
    {"groupItemTitle": "76-77°F", "closed": False, "active": True, "bestBid": 0.6, "bestAsk": 0.68},
    {"groupItemTitle": "74-75°F", "closed": False, "active": True, "bestBid": 0.051, "bestAsk": 0.083},
    {"groupItemTitle": "67°F or below", "closed": False, "active": True, "bestBid": None, "bestAsk": 0.001},
    {"groupItemTitle": "86°F or higher", "closed": True, "active": True, "bestBid": None, "bestAsk": 0.5},
]}


def test_parse_bucket():
    check("range 76-77", parse_bucket("76-77°F") == (76, 77))
    check("tail below", parse_bucket("67°F or below") == (None, 67))
    check("tail higher", parse_bucket("86°F or higher") == (86, None))
    check("single value", parse_bucket("90°F") == (90, 90))
    check("unparseable -> None", parse_bucket("very hot") is None)


def test_price_helpers():
    check("ask 0.68 -> 68c", _ask_cents(0.68) == 68)
    check("ask None -> None", _ask_cents(None) is None)
    check("ask str '0.5' -> 50c", _ask_cents("0.5") == 50)
    check("no_ask from bid 0.6 -> 40c", _no_ask_from_bid(0.6) == 40)
    check("no_ask from bid 0.051 -> 95c", _no_ask_from_bid(0.051) == 95)
    check("no_ask None bid -> None", _no_ask_from_bid(None) is None)


def test_slug_and_parse_event():
    c = PolymarketQuoteClient(Settings())
    check("slug builder",
          c.event_slug("LAX", date(2026, 7, 22)) == "highest-temperature-in-los-angeles-on-july-22-2026")
    qs = c.parse_event("LAX", EVENT)
    check("closed bucket skipped (3 of 4)", len(qs) == 3)
    by = {q.label: q for q in qs}
    check("76-77 YES-ask=68c", by["76-77°F"].yes_ask_cents == 68)
    check("76-77 NO-ask=40c (1-bid)", by["76-77°F"].no_ask_cents == 40)
    check("67-below has no NO side (no YES bid)", by["67°F or below"].no_ask_cents is None)
    check("keys parsed", by["76-77°F"].key == (76, 77))


def test_kalshi_parse():
    check("ticker date parse", kalshi_event_date("KXHIGHNY-26JUL21-B90.5") == "2026-07-21")
    check("bad ticker -> None", kalshi_event_date("KXHIGHNY") is None)
    c = KalshiQuoteClient(Settings())
    markets = [
        {"ticker": "KXHIGHDEN-26JUL23-B90", "floor_strike": 90, "cap_strike": 91,
         "yes_sub_title": "90-91°F", "yes_ask": 45, "no_ask": 58},
        {"ticker": "KXHIGHDEN-26JUL23-T94", "floor_strike": 94, "cap_strike": None,
         "yes_sub_title": "94°F or above", "yes_ask": 0, "no_ask": 99},   # yes_ask 0 -> None
        {"ticker": "KXHIGHDEN-26JUL24-B90", "floor_strike": 90, "cap_strike": 91,
         "yes_ask": 30, "no_ask": 72},                                    # wrong day -> filtered
    ]
    qs = c.parse_markets("DEN", markets, date(2026, 7, 23))
    check("filters to the requested day (2 of 3)", len(qs) == 2)
    b = {q.label: q for q in qs}
    check("range bucket lo/hi + asks", b["90-91°F"].key == (90, 91)
          and b["90-91°F"].yes_ask_cents == 45 and b["90-91°F"].no_ask_cents == 58)
    check("open tail hi=None", b["94°F or above"].hi is None)
    check("yes_ask 0 -> None (no offer)", b["94°F or above"].yes_ask_cents is None)


def test_forecastex_gating():
    off = ForecastExQuoteClient(Settings())
    check("ForecastEx off without creds", off.available is False and "not configured" in off.reason)
    on = ForecastExQuoteClient(Settings(
        ibkr_web_api_base="https://localhost:5000/v1/api",
        forecastex_symbols=(("LAX", "FF"),)))
    check("ForecastEx available with gateway + contract map", on.available is True)


def test_scan_bucket_locks():
    s = Settings()
    # LAX is arb-eligible (ForecastEx<->Polymarket). Aligned bucket 76-77 locks:
    # YES@FE 55c + NO@PM 40c = 95c -> 5c gross - 1c FE fee = 4c.
    pm = [
        BucketQuote(_P, "LAX", 76, 77, "76-77°F", yes_ask_cents=68, no_ask_cents=40),
        BucketQuote(_P, "LAX", 78, 79, "78-79°F", yes_ask_cents=30, no_ask_cents=72),
        # NYC is a BASIS pair -- must never be scanned even if it would "lock".
        BucketQuote(_P, "NYC", 80, 81, "80-81°F", yes_ask_cents=10, no_ask_cents=10),
    ]
    fe = [
        BucketQuote(_F, "LAX", 76, 77, "76-77°F", yes_ask_cents=55, no_ask_cents=47),
        # Non-aligned bucket edges -> not fungible, no comparison.
        BucketQuote(_F, "LAX", 77, 78, "77-78°F", yes_ask_cents=1, no_ask_cents=1),
        BucketQuote(_F, "NYC", 80, 81, "80-81°F", yes_ask_cents=10, no_ask_cents=10),
    ]
    # Against the real registry: nothing, because no venue pair is same-underlying.
    check("real registry scans no bucket locks",
          scan_bucket_locks({_P: pm, _F: fe}, VenueScope.US, s) == [])
    with _synthetic_arb_pair("LAX"):
        locks = scan_bucket_locks({_P: pm, _F: fe}, VenueScope.US, s)
        hits = [l for l in locks if l.net_cents > 0]
        check("exactly one lock", len(hits) == 1)
        check("lock is LAX 76-77", hits and hits[0].city == "LAX" and hits[0].bucket == "76-77°F")
        check("lock nets 4c/contract", hits and hits[0].net_cents == 4)
        check("basis city NYC never scanned", all(l.city != "NYC" for l in locks))
        check("misaligned FE 77-78 not compared", all(l.bucket != "77-78°F" for l in locks))


async def main():
    test_parse_bucket(); test_price_helpers(); test_slug_and_parse_event()
    test_kalshi_parse(); test_forecastex_gating(); test_scan_bucket_locks()
    print("\n" + ("ALL PASS" if not failures else f"FAILURES: {failures}"))
    sys.exit(1 if failures else 0)

asyncio.run(main())
