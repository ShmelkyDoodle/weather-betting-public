"""Tests for verify_mappings classifier + discovery self-guard."""
import asyncio, sys
import os as _osp; sys.path.insert(0, _osp.path.dirname(_osp.path.dirname(_osp.path.abspath(__file__))))

from weather_arb.config import Settings
from weather_arb.verify_mappings import (classify_rules, detect_authority, EXPECTED_AUTHORITY,
                                         STATUS_OK, STATUS_MISMATCH, STATUS_REVIEW)
from weather_arb.market_discovery import MarketDiscovery, MarketRegistry

failures = []
def check(name, cond):
    print(("PASS" if cond else "FAIL"), "-", name)
    if not cond: failures.append(name)

# Live wording as of 2026-08-21: station given only as its CLI product code, and
# settled "according to The Weather Company" (Kalshi moved off the NWS CLI on
# 2026-08-14). Discovery blocks any series whose authority is not TWC.
TWC = "according to The Weather Company, then the market resolves to Yes."
NY_RULES = f"If the maximum temperature recorded at New York City (CLINYC) for Aug 22, 2026 {TWC}"
CHI_MID  = f"If the maximum temperature recorded at Chicago (CLIMDW) for Aug 22, 2026 {TWC}"
CHI_OHARE = f"If the maximum temperature recorded at Chicago (CLIORD) for Aug 22, 2026 {TWC}"
# Pre-2026-08-14 wording, kept so the plain-English matcher stays covered.
NY_OLD  = "If the highest temperature recorded in Central Park, New York for July 22 ... resolves Yes."
CHI_OLD = "If the highest temperature recorded at Chicago Midway, IL for July 22 ... resolves Yes."

def test_classify():
    check("CLINYC -> KNYC ok", classify_rules(NY_RULES, "KNYC")[0] == STATUS_OK)
    check("CLIMDW -> KMDW ok", classify_rules(CHI_MID, "KMDW")[0] == STATUS_OK)
    check("central park (old wording) -> KNYC ok", classify_rules(NY_OLD, "KNYC")[0] == STATUS_OK)
    check("midway (old wording) -> KMDW ok", classify_rules(CHI_OLD, "KMDW")[0] == STATUS_OK)
    check("TWC authority detected", detect_authority(NY_RULES) == EXPECTED_AUTHORITY)
    check("NWS authority detected",
          detect_authority("... according to the National Weather Service") == "NWS")
    check("unknown authority -> None", detect_authority("... according to Acme Corp") is None)
    # Kalshi moved Chicago to O'Hare but we still map KMDW -> mismatch (names KORD)
    st, detail = classify_rules(CHI_OHARE, "KMDW")
    check("ohare vs KMDW -> mismatch", st == STATUS_MISMATCH and "KORD" in (detail or ""))
    # Unknown wording -> review (not a hard mismatch)
    check("unknown -> review", classify_rules("some airport somewhere", "KMDW")[0] == STATUS_REVIEW)
    # Wrong-city mismatch
    check("miami rules vs KNYC -> mismatch",
          classify_rules("recorded at Miami International Airport", "KNYC")[0] == STATUS_MISMATCH)


class StubClient:
    """Minimal duck-typed client exposing only what discovery.refresh uses."""
    def __init__(self, rules_by_series): self.rules = rules_by_series
    async def get_markets(self, series_ticker=None, status=None, **kw):
        return [{"ticker": f"{series_ticker}-26JUL22-B85.5", "status": "active",
                 "floor_strike": 85.5, "strike_type": "greater_or_equal", "close_time": None}]
    async def get_market_rules(self, ticker):
        return self.rules.get(ticker.split("-")[0], "")


async def test_discovery_blocks_mismatch():
    settings = Settings(weather_series=(("KXHIGHNY", "KNYC"), ("KXHIGHCHI", "KMDW")))
    reg = MarketRegistry()
    # NY rules wrongly point at O'Hare-style text for KNYC? No -- simulate NY OK,
    # but Chicago drifted to O'Hare while we still map KMDW -> should be blocked.
    client = StubClient({"KXHIGHNY": NY_RULES, "KXHIGHCHI": CHI_OHARE})
    disc = MarketDiscovery(client=client, settings=settings, registry=reg)  # type: ignore[arg-type]
    await disc.refresh()
    tickers = reg.tickers()
    check("clean series (NY) is tradeable", any(t.startswith("KXHIGHNY") for t in tickers))
    check("mismatched series (CHI) is blocked", not any(t.startswith("KXHIGHCHI") for t in tickers))
    check("CHI recorded in _blocked", "KXHIGHCHI" in disc._blocked)

    # Now Kalshi 'fixes' it back to Midway -> next-day refresh re-enables it.
    client.rules["KXHIGHCHI"] = CHI_MID
    disc._verified_on.clear()  # force re-verification
    await disc.refresh()
    check("CHI re-enabled after fix", any(t.startswith("KXHIGHCHI") for t in reg.tickers()))
    check("CHI removed from _blocked", "KXHIGHCHI" not in disc._blocked)


async def test_discovery_blocks_authority_change():
    """A silent change of settlement authority must stop trading, not warn-and-continue.

    This is the 2026-08-14 regression: Kalshi moved NWS CLI -> The Weather Company
    and the engine kept trading for a week. Our T_max is verified to match TWC's
    published daily max; against any other authority that equality is unproven."""
    settings = Settings(weather_series=(("KXHIGHNY", "KNYC"),))
    reg = MarketRegistry()
    nws_era = "If the maximum temperature recorded at New York City (CLINYC) ... " \
              "according to the National Weather Service, then the market resolves to Yes."
    client = StubClient({"KXHIGHNY": nws_era})
    disc = MarketDiscovery(client=client, settings=settings, registry=reg)  # type: ignore[arg-type]
    await disc.refresh()
    check("non-TWC authority blocks the series",
          not any(t.startswith("KXHIGHNY") for t in reg.tickers()))
    check("authority change recorded in _blocked", "KXHIGHNY" in disc._blocked)

    # Station is right but the authority is unparseable -> still blocked (fail closed).
    client.rules["KXHIGHNY"] = "maximum temperature at New York City (CLINYC) per Acme Corp."
    disc._verified_on.clear()
    await disc.refresh()
    check("unrecognised authority also blocks",
          not any(t.startswith("KXHIGHNY") for t in reg.tickers()))

    # Back to TWC -> tradeable again.
    client.rules["KXHIGHNY"] = NY_RULES
    disc._verified_on.clear()
    await disc.refresh()
    check("TWC authority re-enables the series",
          any(t.startswith("KXHIGHNY") for t in reg.tickers()))


async def main():
    test_classify()
    await test_discovery_blocks_mismatch()
    await test_discovery_blocks_authority_change()
    print("\n" + ("ALL PASS" if not failures else f"FAILURES: {failures}"))
    sys.exit(1 if failures else 0)

asyncio.run(main())
