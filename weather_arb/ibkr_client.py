"""Minimal read-only IBKR Client Portal Web API (CPAPI) client.

ForecastEx has no public quote feed; its market data is only reachable through an
authenticated IBKR **Client Portal Gateway** running locally (the user logs in at
``https://localhost:5000``; the REST API then lives at ``.../v1/api``). This
client wraps just the read-only endpoints the cross-venue monitor needs:

* ``ensure_ready``  -- ``GET /iserver/accounts`` (initialises the brokerage
  session; must be hit once before market data works);
* ``tickle``        -- ``GET /tickle`` keep-alive;
* ``search_symbol`` -- ``GET /iserver/secdef/search`` (find the underlying conid);
* ``strikes``       -- ``GET /iserver/secdef/strikes`` (temperature strikes);
* ``secdef_info``   -- ``GET /iserver/secdef/info`` (resolve a strike -> conid);
* ``snapshot``      -- ``GET /iserver/marketdata/snapshot`` (bid=84, ask=86,
  ask-size=85, bid-size=88); the first call only primes the stream, so we poll.

Every call is defensive: any transport/HTTP/JSON error returns ``None`` (or an
empty result) rather than raising, so a missing/unauthenticated gateway just
means "no ForecastEx quotes", never a crashed monitor. **Nothing here places or
modifies orders.**

The localhost gateway serves a self-signed certificate, so TLS verification is
off by default (``ibkr_verify_ssl``); the client only ever talks to the base URL
the user configures.
"""

from __future__ import annotations

import asyncio
import re
from typing import Optional

import aiohttp

# CPAPI market-data field tags.
FIELD_LAST = "31"
FIELD_BID = "84"
FIELD_ASK_SIZE = "85"
FIELD_ASK = "86"
FIELD_BID_SIZE = "88"
SNAPSHOT_FIELDS = (FIELD_LAST, FIELD_BID, FIELD_ASK_SIZE, FIELD_ASK, FIELD_BID_SIZE)

_NUM_RE = re.compile(r"-?\d+\.?\d*")
_HTTP_TIMEOUT = aiohttp.ClientTimeout(total=15)


def parse_price_cents(value: object) -> Optional[int]:
    """A CPAPI price field (dollars, possibly a prefixed string) -> integer cents.

    Event-contract prices live in [0, 1]; we return cents without clamping so
    synthesized combinations that exceed 100 stay faithful. ``None`` if absent.
    """
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return round(float(value) * 100)
    m = _NUM_RE.search(str(value))
    return round(float(m.group()) * 100) if m else None


def parse_size(value: object) -> Optional[int]:
    if value is None or value == "":
        return None
    m = _NUM_RE.search(str(value))
    return int(float(m.group())) if m else None


class IBKRClient:
    """Read-only CPAPI wrapper bound to one aiohttp session."""

    def __init__(self, base_url: str, session: aiohttp.ClientSession,
                 verify_ssl: bool = False) -> None:
        self.base = base_url.rstrip("/")
        self.session = session
        self._ssl = None if verify_ssl else False  # aiohttp: ssl=False disables verify
        self._ready = False

    async def _get(self, path: str, params: Optional[dict] = None) -> Optional[object]:
        url = f"{self.base}{path}"
        try:
            async with self.session.get(url, params=params, ssl=self._ssl,
                                        timeout=_HTTP_TIMEOUT) as resp:
                if resp.status != 200:
                    return None
                return await resp.json(content_type=None)
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError):
            return None

    async def ensure_ready(self) -> bool:
        """Initialise the brokerage session once. Returns True if authenticated."""
        if self._ready:
            return True
        data = await self._get("/iserver/accounts")
        # A populated 'accounts' list means the gateway is logged in.
        self._ready = bool(isinstance(data, dict) and data.get("accounts"))
        return self._ready

    async def tickle(self) -> bool:
        return await self._get("/tickle") is not None

    async def search_symbol(self, symbol: str) -> list[dict]:
        data = await self._get("/iserver/secdef/search", {"symbol": symbol})
        return data if isinstance(data, list) else []

    async def strikes(self, conid: int, month: str,
                      exchange: str = "FORECASTX", sectype: str = "OPT") -> list[float]:
        data = await self._get("/iserver/secdef/strikes", {
            "conid": conid, "exchange": exchange, "sectype": sectype, "month": month,
        })
        if not isinstance(data, dict):
            return []
        # ForecastEx uses the 'call' leg for the '>= strike' (YES) contract.
        raw = data.get("call") or data.get("put") or []
        out: list[float] = []
        for s in raw:
            try:
                out.append(float(s))
            except (TypeError, ValueError):
                continue
        return out

    async def secdef_info(self, conid: int, month: str, strike: float,
                          right: str = "C", exchange: str = "FORECASTX",
                          sectype: str = "OPT") -> list[dict]:
        data = await self._get("/iserver/secdef/info", {
            "conid": conid, "exchange": exchange, "sectype": sectype,
            "month": month, "strike": strike, "right": right,
        })
        return data if isinstance(data, list) else []

    async def snapshot(self, conids: list[int],
                       fields: tuple[str, ...] = SNAPSHOT_FIELDS,
                       prime_delay: float = 1.0) -> dict[int, dict]:
        """Top-of-book snapshot for ``conids`` -> ``{conid: {bid,ask,bid_size,ask_size}}``.

        The first CPAPI snapshot call only opens the stream, so we call once to
        prime, wait briefly, then read.
        """
        if not conids:
            return {}
        params = {"conids": ",".join(str(c) for c in conids), "fields": ",".join(fields)}
        await self._get("/iserver/marketdata/snapshot", params)  # prime the stream
        await asyncio.sleep(prime_delay)
        data = await self._get("/iserver/marketdata/snapshot", params)
        rows = data if isinstance(data, list) else []
        return self.parse_snapshot(rows)

    @staticmethod
    def parse_snapshot(rows: list[dict]) -> dict[int, dict]:
        out: dict[int, dict] = {}
        for row in rows:
            conid = row.get("conid")
            if conid is None:
                continue
            out[int(conid)] = {
                "bid": parse_price_cents(row.get(FIELD_BID)),
                "ask": parse_price_cents(row.get(FIELD_ASK)),
                "bid_size": parse_size(row.get(FIELD_BID_SIZE)),
                "ask_size": parse_size(row.get(FIELD_ASK_SIZE)),
            }
        return out
