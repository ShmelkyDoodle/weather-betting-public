"""ForecastEx via IBKR: snapshot parse, threshold->bin synthesis, synthesized lock."""
import asyncio, sys
import os as _osp; sys.path.insert(0, _osp.path.dirname(_osp.path.dirname(_osp.path.abspath(__file__))))
from weather_arb.config import Settings
from weather_arb.models import Venue
from weather_arb import cross_venue as _cv
from weather_arb.cross_venue import VenueScope
from weather_arb.ibkr_client import IBKRClient, parse_price_cents, parse_size
from weather_arb.cross_venue_quotes import (
    BucketQuote, ForecastExQuoteClient, synthesize_forecastex_bins,
    build_scan_input, scan_bucket_locks,
)


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


def test_ibkr_parse():
    check("price '0.52' -> 52c", parse_price_cents("0.52") == 52)
    check("price float 0.52 -> 52c", parse_price_cents(0.52) == 52)
    check("price prefixed 'C0.5' -> 50c", parse_price_cents("C0.5") == 50)
    check("price None -> None", parse_price_cents(None) is None)
    check("price '' -> None", parse_price_cents("") is None)
    check("size '10' -> 10", parse_size("10") == 10)
    rows = [{"conid": 123, "84": "0.50", "86": "0.53", "85": "10", "88": "8"}]
    snap = IBKRClient.parse_snapshot(rows)
    check("snapshot bid=50", snap[123]["bid"] == 50)
    check("snapshot ask=53", snap[123]["ask"] == 53)
    check("snapshot ask_size=10 / bid_size=8", snap[123]["ask_size"] == 10 and snap[123]["bid_size"] == 8)


def _thr(strike, bid, ask):
    # A ForecastEx '>= strike' threshold as a raw BucketQuote (NO_ask = 100 - bid).
    return BucketQuote(_F, "LAX", strike, None, f">={strike}F",
                       yes_ask_cents=ask, no_ask_cents=(100 - bid))


def test_synthesis_math():
    # >=80 bid90/ask91 ; >=82 bid5/ask6
    fe = [_thr(80, 90, 91), _thr(82, 5, 6)]
    bins = synthesize_forecastex_bins(fe, [(80, 81)], "LAX")
    b = bins[0]
    # yes = a(80)+100-b(82) = 91+95 = 186 ; no = a(82)+100-b(80) = 6+10 = 16
    check("mid-bin YES-ask synthesized (186)", b.yes_ask_cents == 186)
    check("mid-bin NO-ask synthesized (16)", b.no_ask_cents == 16)
    check("bin key + label", b.key == (80, 81) and b.label == "80-81F")
    # Lower tail (None,67) uses threshold 68: yes=no_ask(68)=100-1=99 ; no=a(68)=2
    fe2 = [_thr(68, 1, 2)]
    low = synthesize_forecastex_bins(fe2, [(None, 67)], "LAX")[0]
    check("tail-below YES=99 NO=2", low.yes_ask_cents == 99 and low.no_ask_cents == 2)
    # Upper tail (86,None) is threshold 86 itself: yes=a(86)=7 ; no=100-b(86)=96
    fe3 = [_thr(86, 4, 7)]
    high = synthesize_forecastex_bins(fe3, [(86, None)], "LAX")[0]
    check("tail-above YES=7 NO=96", high.yes_ask_cents == 7 and high.no_ask_cents == 96)
    # Missing leg -> bin skipped entirely.
    only80 = synthesize_forecastex_bins([_thr(80, 90, 91)], [(80, 81)], "LAX")
    check("missing >=82 leg -> bin skipped", only80 == [])


def test_build_scan_input_and_lock():
    s = Settings()
    pm = [BucketQuote(_P, "LAX", 80, 81, "80-81°F", yes_ask_cents=70, no_ask_cents=32)]
    fe = [_thr(80, 90, 91), _thr(82, 5, 6)]     # -> synth bin 80-81: yes186 / no16
    merged = build_scan_input({_P: pm, _F: fe}, ["LAX"])
    fe_bins = merged[_F]
    check("FE thresholds replaced by aligned bin", len(fe_bins) == 1 and fe_bins[0].key == (80, 81))
    # Against the real registry the pair is basis, so the scan finds nothing.
    check("real registry scans no synthesized lock",
          scan_bucket_locks(merged, VenueScope.US, s) == [])
    with _synthetic_arb_pair("LAX"):
        locks = scan_bucket_locks(merged, VenueScope.US, s)
        hits = [l for l in locks if l.net_cents > 0]
        # buy YES@Polymarket 70c + NO@ForecastEx 16c = 86c -> 14c gross - 1c FE fee = 13c
        check("synthesized lock found", len(hits) == 1)
        check("lock nets 13c/contract", hits and hits[0].net_cents == 13)
        check("lock legs = PM YES + FE NO",
              hits and "YES@Polymarket" in hits[0].legs and "NO@" in hits[0].legs)


def test_forecastex_gating():
    off = ForecastExQuoteClient(Settings())
    check("off without gateway", off.available is False and "not configured" in off.reason)
    no_map = ForecastExQuoteClient(Settings(ibkr_web_api_base="https://localhost:5000/v1/api"))
    check("gateway but no contract map -> off", no_map.available is False and "no contract map" in no_map.reason)
    ok = ForecastExQuoteClient(Settings(
        ibkr_web_api_base="https://localhost:5000/v1/api",
        forecastex_symbols=(("LAX", "FF"),)))
    check("gateway + symbol map -> available", ok.available is True)


async def main():
    test_ibkr_parse(); test_synthesis_math(); test_build_scan_input_and_lock()
    test_forecastex_gating()
    print("\n" + ("ALL PASS" if not failures else f"FAILURES: {failures}"))
    sys.exit(1 if failures else 0)

asyncio.run(main())
