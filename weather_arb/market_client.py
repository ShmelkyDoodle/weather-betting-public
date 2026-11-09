"""Prediction-market client: Kalshi REST + WebSocket (limit orders only).

The concrete :class:`KalshiClient` implements the full surface the engine needs:

* Authenticated REST (RSA-PSS request signing) for orders, cancels and balance.
* Public REST for market metadata and order-book snapshots.
* A resilient WebSocket feed that maintains live order books from
  ``orderbook_snapshot`` + ``orderbook_delta`` messages.
* Token-bucket rate limiting and bounded retry/backoff on 429/5xx.

Only **limit** orders can be constructed (:class:`~weather_arb.models.OrderType`
has a single member) and the client refuses anything else, satisfying the hard
"never send market orders" requirement.

When ``settings.dry_run`` is enabled (the default when no credentials are
present), order placement/cancellation are simulated and journaled locally while
market-data reads still hit the live public API, so the whole pipeline can run
end-to-end without risking capital.

The abstract :class:`MarketClient` base makes adding a Polymarket CLOB client a
matter of implementing the same methods.
"""

from __future__ import annotations

import abc
import asyncio
import base64
import logging
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Optional
from urllib.parse import urlsplit

import aiohttp
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from . import timeutils
from .config import KalshiConfig, Settings
from .models import (
    Action,
    LimitOrder,
    LimitOrderRequest,
    OrderBook,
    OrderStatus,
    OrderType,
    PriceLevel,
    Side,
    Venue,
)

log = logging.getLogger(__name__)


class MarketClientError(RuntimeError):
    """Base error for market-client failures."""


class OrderRejectedError(MarketClientError):
    """Raised when the exchange rejects an order request."""


class RateLimitedError(MarketClientError):
    """Raised after retries are exhausted against persistent 429s."""


@dataclass(frozen=True, slots=True)
class MarketInfo:
    """Live, venue-reported metadata for a market."""

    ticker: str
    status: str
    title: str
    close_time_utc: Optional[datetime]


# ---------------------------------------------------------------------- #
# Rate limiting
# ---------------------------------------------------------------------- #
class TokenBucketRateLimiter:
    """Asyncio-safe token bucket.

    Refills at ``rate`` tokens/second up to ``burst`` capacity.  ``acquire``
    blocks until a token is available; the internal lock also serializes callers
    so request ordering is preserved.
    """

    def __init__(self, rate: float, burst: int) -> None:
        self._rate = float(rate)
        self._capacity = float(burst)
        self._tokens = float(burst)
        self._updated = time.monotonic()
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        async with self._lock:
            while True:
                now = time.monotonic()
                self._tokens = min(
                    self._capacity, self._tokens + (now - self._updated) * self._rate
                )
                self._updated = now
                if self._tokens >= 1.0:
                    self._tokens -= 1.0
                    return
                await asyncio.sleep((1.0 - self._tokens) / self._rate)


# ---------------------------------------------------------------------- #
# Request signing
# ---------------------------------------------------------------------- #
class KalshiSigner:
    """Builds Kalshi RSA-PSS auth headers for a request.

    The signed message is ``timestamp_ms + HTTP_METHOD + request_path`` (path
    only, no query string), signed with the account's RSA private key using
    PSS/SHA-256 and base64-encoded.
    """

    def __init__(self, key_id: str, private_key_path: str) -> None:
        self._key_id = key_id
        with open(private_key_path, "rb") as handle:
            key = serialization.load_pem_private_key(handle.read(), password=None)
        if not isinstance(key, rsa.RSAPrivateKey):
            raise MarketClientError("Kalshi private key must be an RSA key")
        self._key: rsa.RSAPrivateKey = key

    def headers(self, method: str, path: str) -> dict[str, str]:
        timestamp_ms = str(int(time.time() * 1000))
        message = f"{timestamp_ms}{method.upper()}{path}".encode("utf-8")
        signature = self._key.sign(
            message,
            padding.PSS(
                mgf=padding.MGF1(hashes.SHA256()),
                salt_length=padding.DIGEST_LENGTH,
            ),
            hashes.SHA256(),
        )
        return {
            "KALSHI-ACCESS-KEY": self._key_id,
            "KALSHI-ACCESS-SIGNATURE": base64.b64encode(signature).decode("ascii"),
            "KALSHI-ACCESS-TIMESTAMP": timestamp_ms,
        }


# ---------------------------------------------------------------------- #
# Abstract client
# ---------------------------------------------------------------------- #
class MarketClient(abc.ABC):
    """Interface every venue client must implement."""

    venue: Venue

    @abc.abstractmethod
    async def get_orderbook(self, ticker: str, depth: int = 32) -> OrderBook: ...

    @abc.abstractmethod
    async def get_market(self, ticker: str) -> MarketInfo: ...

    @abc.abstractmethod
    async def get_markets(
        self,
        *,
        series_ticker: Optional[str] = None,
        event_ticker: Optional[str] = None,
        status: Optional[str] = None,
        max_pages: int = 10,
    ) -> list[dict[str, Any]]: ...

    @abc.abstractmethod
    async def get_market_result(self, ticker: str) -> Optional[str]: ...

    async def get_market_settlement(
        self, ticker: str
    ) -> tuple[Optional[str], Optional[datetime]]:
        """``(result, settled_at_utc)``. Deliberately NOT abstract.

        Every offline test builds a stub client, so making this abstract would
        break them all to add a field that is pure telemetry. The default keeps
        the existing behaviour and simply reports no timestamp; concrete clients
        override it. ``market_results.settled_at_utc`` was NULL on all 1,470 rows
        because nothing ever supplied it.
        """
        return await self.get_market_result(ticker), None

    @abc.abstractmethod
    async def get_market_rules(self, ticker: str) -> Optional[str]: ...

    @abc.abstractmethod
    async def place_limit_order(self, request: LimitOrderRequest) -> LimitOrder: ...

    @abc.abstractmethod
    async def cancel_order(self, exchange_order_id: str) -> bool: ...

    @abc.abstractmethod
    async def get_open_exchange_orders(
        self, ticker: Optional[str] = None
    ) -> list[LimitOrder]: ...

    @abc.abstractmethod
    async def get_order_by_id(self, exchange_order_id: str) -> Optional[LimitOrder]: ...

    @abc.abstractmethod
    async def get_balance_usd(self) -> float: ...

    @abc.abstractmethod
    def get_cached_orderbook(self, ticker: str) -> Optional[OrderBook]: ...

    @abc.abstractmethod
    async def run_websocket(
        self, stop_event: asyncio.Event, tickers: list[str]
    ) -> None: ...

    @abc.abstractmethod
    async def close(self) -> None: ...


# ---------------------------------------------------------------------- #
# Kalshi implementation
# ---------------------------------------------------------------------- #
class KalshiClient(MarketClient):
    """Concurrent Kalshi trade-api v2 client."""

    venue = Venue.KALSHI

    _RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504})
    _MAX_RETRIES = 4

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._cfg: KalshiConfig = settings.kalshi
        self._dry_run = settings.dry_run
        self._rate_limiter = TokenBucketRateLimiter(
            self._cfg.rate_limit_per_second, self._cfg.rate_limit_burst
        )
        self._signer: Optional[KalshiSigner] = None
        if self._cfg.has_credentials:
            assert self._cfg.api_key_id and self._cfg.private_key_path
            self._signer = KalshiSigner(
                self._cfg.api_key_id, self._cfg.private_key_path
            )
        self._session: Optional[aiohttp.ClientSession] = None
        # ticker -> {"yes": {price:size}, "no": {price:size}, "ts": datetime}
        self._books: dict[str, dict[str, Any]] = {}
        self._books_lock = asyncio.Lock()

    # ------------------------------------------------------------------ #
    # Session lifecycle
    # ------------------------------------------------------------------ #
    async def _ensure_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            timeout = aiohttp.ClientTimeout(total=self._cfg.request_timeout_seconds)
            self._session = aiohttp.ClientSession(
                timeout=timeout,
                headers={"User-Agent": self._settings.http_user_agent,
                         "Accept": "application/json"},
            )
        return self._session

    async def close(self) -> None:
        if self._session is not None and not self._session.closed:
            await self._session.close()
            self._session = None

    # ------------------------------------------------------------------ #
    # Low-level signed/unsigned request with retry + rate limiting
    # ------------------------------------------------------------------ #
    async def _request(
        self,
        method: str,
        path: str,
        *,
        authed: bool,
        params: Optional[dict[str, Any]] = None,
        json_body: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        session = await self._ensure_session()
        url = f"{self._cfg.rest_base_url}{path}"
        sign_path = urlsplit(url).path  # path only, per Kalshi signing spec

        last_error: Optional[Exception] = None
        for attempt in range(self._MAX_RETRIES):
            headers: dict[str, str] = {}
            if authed:
                if self._signer is None:
                    raise MarketClientError(
                        "authenticated request requires Kalshi credentials"
                    )
                headers.update(self._signer.headers(method, sign_path))

            await self._rate_limiter.acquire()
            try:
                async with session.request(
                    method, url, params=params, json=json_body, headers=headers
                ) as resp:
                    text = await resp.text()
                    if resp.status in self._RETRYABLE_STATUS:
                        last_error = MarketClientError(
                            f"{resp.status} on {method} {path}: {text[:200]}"
                        )
                        await self._backoff(attempt, resp.headers.get("Retry-After"))
                        continue
                    if resp.status >= 400:
                        raise OrderRejectedError(
                            f"{resp.status} on {method} {path}: {text[:300]}"
                        )
                    return _loads(text)
            except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                last_error = exc
                await self._backoff(attempt, None)

        if isinstance(last_error, MarketClientError):
            raise RateLimitedError(str(last_error))
        raise MarketClientError(f"request failed after retries: {last_error}")

    async def _backoff(self, attempt: int, retry_after: Optional[str]) -> None:
        if retry_after is not None:
            try:
                await asyncio.sleep(min(10.0, float(retry_after)))
                return
            except ValueError:
                pass
        await asyncio.sleep(min(8.0, 0.5 * (2 ** attempt)))

    # ------------------------------------------------------------------ #
    # Market data (public)
    # ------------------------------------------------------------------ #
    async def get_orderbook(self, ticker: str, depth: int = 32) -> OrderBook:
        payload = await self._request(
            "GET",
            f"/markets/{ticker}/orderbook",
            authed=False,
            params={"depth": depth},
        )
        return _orderbook_from_kalshi(ticker, payload)

    async def get_market(self, ticker: str) -> MarketInfo:
        payload = await self._request("GET", f"/markets/{ticker}", authed=False)
        market = payload.get("market") or {}
        close_raw = market.get("close_time")
        close_time = (
            timeutils.parse_isoformat_utc(str(close_raw)) if close_raw else None
        )
        return MarketInfo(
            ticker=ticker,
            status=str(market.get("status", "unknown")),
            title=str(market.get("title", "")),
            close_time_utc=close_time,
        )

    async def get_markets(
        self,
        *,
        series_ticker: Optional[str] = None,
        event_ticker: Optional[str] = None,
        status: Optional[str] = None,
        max_pages: int = 10,
    ) -> list[dict[str, Any]]:
        """List raw market dicts (public), following pagination cursors."""
        markets: list[dict[str, Any]] = []
        cursor: Optional[str] = None
        for _ in range(max_pages):
            params: dict[str, Any] = {"limit": 200}
            if series_ticker is not None:
                params["series_ticker"] = series_ticker
            if event_ticker is not None:
                params["event_ticker"] = event_ticker
            if status is not None:
                params["status"] = status
            if cursor:
                params["cursor"] = cursor
            payload = await self._request("GET", "/markets", authed=False, params=params)
            batch = payload.get("markets") or []
            markets.extend(batch)
            cursor = payload.get("cursor") or None
            if not cursor or not batch:
                break
        return markets

    async def get_market_rules(self, ticker: str) -> Optional[str]:
        """Return the market's ``rules_primary`` settlement text (public)."""
        payload = await self._request("GET", f"/markets/{ticker}", authed=False)
        market = payload.get("market") or {}
        rules = str(market.get("rules_primary") or market.get("rules_secondary") or "")
        return rules or None

    async def get_market_result(self, ticker: str) -> Optional[str]:
        """Return the settlement result (``"yes"``/``"no"``) or ``None`` if unsettled."""
        payload = await self._request("GET", f"/markets/{ticker}", authed=False)
        market = payload.get("market") or {}
        result = str(market.get("result", "")).lower()
        if result in ("yes", "no"):
            return result
        # Some settled markets expose result only via status transitions.
        status = str(market.get("status", "")).lower()
        if status in ("settled", "finalized", "determined") and result in ("yes", "no"):
            return result
        return None

    async def get_market_settlement(
        self, ticker: str
    ) -> tuple[Optional[str], Optional[datetime]]:
        """Result plus the exchange's own settlement time, when it publishes one.

        Kalshi has used several field names for this, so all the plausible ones
        are tried rather than assuming; an unparseable or absent value simply
        yields ``None`` and the result is still returned.
        """
        payload = await self._request("GET", f"/markets/{ticker}", authed=False)
        market = payload.get("market") or {}
        result = str(market.get("result", "")).lower()
        if result not in ("yes", "no"):
            return None, None
        for key in ("settled_time", "settlement_time", "determination_time",
                    "close_time", "expiration_time"):
            raw = market.get(key)
            if not raw:
                continue
            try:
                return result, timeutils.parse_isoformat_utc(str(raw))
            except ValueError:
                continue
        return result, None

    # ------------------------------------------------------------------ #
    # Orders (authenticated / simulated in dry-run)
    # ------------------------------------------------------------------ #
    async def place_limit_order(self, request: LimitOrderRequest) -> LimitOrder:
        # Hard guarantee: only limit orders ever leave this process.
        if request.order_type is not OrderType.LIMIT:
            raise OrderRejectedError("only limit orders are permitted")
        if not (1 <= request.price_cents <= 99):
            raise OrderRejectedError(
                f"limit price {request.price_cents} outside tradeable [1, 99]"
            )
        if request.count <= 0:
            raise OrderRejectedError("order count must be positive")

        now = timeutils.utc_now()
        if self._dry_run:
            log.info(
                "DRY-RUN limit order (not transmitted)",
                extra={"context": _order_context(request)},
            )
            return LimitOrder(
                client_order_id=request.client_order_id,
                ticker=request.ticker,
                venue=self.venue,
                side=request.side,
                action=request.action,
                price_cents=request.price_cents,
                count=request.count,
                strategy=request.strategy,
                status=OrderStatus.RESTING,
                created_at_utc=now,
                updated_at_utc=now,
                exchange_order_id=f"dry-{request.client_order_id}",
                filled_count=0,
            )

        body: dict[str, Any] = {
            "ticker": request.ticker,
            "client_order_id": request.client_order_id,
            "side": request.side.value,
            "action": request.action.value,
            "count": request.count,
            "type": OrderType.LIMIT.value,
        }
        if request.side is Side.YES:
            body["yes_price"] = request.price_cents
        else:
            body["no_price"] = request.price_cents

        payload = await self._request(
            "POST", "/portfolio/orders", authed=True, json_body=body
        )
        order = payload.get("order") or {}
        parsed = _order_from_kalshi(order, fallback=request, created_at=now)
        log.info(
            "limit order placed",
            extra={"context": {**_order_context(request),
                               "exchange_order_id": parsed.exchange_order_id,
                               "status": parsed.status.value}},
        )
        return parsed

    async def cancel_order(self, exchange_order_id: str) -> bool:
        if self._dry_run or exchange_order_id.startswith("dry-"):
            log.info(
                "DRY-RUN cancel (not transmitted)",
                extra={"context": {"exchange_order_id": exchange_order_id}},
            )
            return True
        try:
            await self._request(
                "DELETE", f"/portfolio/orders/{exchange_order_id}", authed=True
            )
            return True
        except OrderRejectedError as exc:
            # A 404 means the order is already gone (filled/canceled) -> treat as
            # success for reconciliation purposes.
            if "404" in str(exc):
                return True
            log.warning(
                "cancel failed",
                extra={"context": {"exchange_order_id": exchange_order_id,
                                   "error": str(exc)}},
            )
            return False

    async def get_open_exchange_orders(
        self, ticker: Optional[str] = None
    ) -> list[LimitOrder]:
        if self._dry_run:
            return []
        orders: list[LimitOrder] = []
        cursor: Optional[str] = None
        while True:
            params: dict[str, Any] = {"status": "resting", "limit": 200}
            if ticker is not None:
                params["ticker"] = ticker
            if cursor:
                params["cursor"] = cursor
            payload = await self._request(
                "GET", "/portfolio/orders", authed=True, params=params
            )
            for raw in payload.get("orders", []):
                orders.append(_order_from_kalshi(raw, created_at=timeutils.utc_now()))
            cursor = payload.get("cursor") or None
            if not cursor:
                break
        return orders

    async def get_order_by_id(self, exchange_order_id: str) -> Optional[LimitOrder]:
        if self._dry_run or exchange_order_id.startswith("dry-"):
            return None
        try:
            payload = await self._request(
                "GET", f"/portfolio/orders/{exchange_order_id}", authed=True
            )
        except OrderRejectedError as exc:
            if "404" in str(exc):
                return None
            raise
        order = payload.get("order")
        if not order:
            return None
        return _order_from_kalshi(order, created_at=timeutils.utc_now())

    async def get_balance_usd(self) -> float:
        if self._dry_run or self._signer is None:
            return self._settings.risk.account_equity_usd
        payload = await self._request("GET", "/portfolio/balance", authed=True)
        # Kalshi reports balance in cents.
        return float(payload.get("balance", 0)) / 100.0

    # ------------------------------------------------------------------ #
    # WebSocket order-book feed
    # ------------------------------------------------------------------ #
    def get_cached_orderbook(self, ticker: str) -> Optional[OrderBook]:
        state = self._books.get(ticker)
        if state is None:
            return None
        yes = tuple(
            PriceLevel(price, size)
            for price, size in sorted(state["yes"].items(), reverse=True)
            if size > 0
        )
        no = tuple(
            PriceLevel(price, size)
            for price, size in sorted(state["no"].items(), reverse=True)
            if size > 0
        )
        return OrderBook(ticker=ticker, timestamp=state["ts"], yes_bids=yes, no_bids=no)

    async def run_websocket(
        self, stop_event: asyncio.Event, tickers: list[str]
    ) -> None:
        """Maintain live order books until ``stop_event`` is set (auto-reconnect).

        When there is nothing to stream (no credentials, or no tickers), this
        parks on ``stop_event`` and returns only on shutdown -- it must remain a
        long-lived task so the supervisor doesn't mistake an early return for a
        crashed loop and tear the process down.
        """
        if self._signer is None:
            log.warning("websocket disabled: no credentials (REST order-book polling only)")
            await stop_event.wait()
            return
        if not tickers:
            log.info("websocket idle: no tickers to subscribe")
            await stop_event.wait()
            return

        backoff = 1.0
        while not stop_event.is_set():
            try:
                await self._ws_session(stop_event, tickers)
                backoff = 1.0  # clean exit -> reset backoff
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - reconnect on any failure
                log.exception("websocket session error; reconnecting")
                try:
                    await asyncio.wait_for(stop_event.wait(), timeout=backoff)
                except asyncio.TimeoutError:
                    pass
                backoff = min(30.0, backoff * 2)

    async def _ws_session(
        self, stop_event: asyncio.Event, tickers: list[str]
    ) -> None:
        assert self._signer is not None
        session = await self._ensure_session()
        ws_path = urlsplit(self._cfg.ws_url).path
        headers = self._signer.headers("GET", ws_path)
        async with session.ws_connect(self._cfg.ws_url, headers=headers) as ws:
            await ws.send_json(
                {
                    "id": 1,
                    "cmd": "subscribe",
                    "params": {
                        "channels": ["orderbook_delta"],
                        "market_tickers": tickers,
                    },
                }
            )
            log.info(
                "websocket subscribed",
                extra={"context": {"tickers": tickers}},
            )
            async for msg in ws:
                if stop_event.is_set():
                    await ws.close()
                    break
                if msg.type == aiohttp.WSMsgType.TEXT:
                    await self._handle_ws_message(_loads(msg.data))
                elif msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                    break

    async def _handle_ws_message(self, message: dict[str, Any]) -> None:
        mtype = message.get("type")
        body = message.get("msg") or {}
        if mtype == "orderbook_snapshot":
            await self._apply_snapshot(body)
        elif mtype == "orderbook_delta":
            await self._apply_delta(body)
        elif mtype == "error":
            log.warning("websocket error message", extra={"context": {"msg": body}})

    async def _apply_snapshot(self, body: dict[str, Any]) -> None:
        ticker = body.get("market_ticker")
        if not ticker:
            return
        # Same dual encoding as REST (see _orderbook_from_kalshi). The WS feed is
        # inert without credentials, but it must not carry the identical defect.
        dollars = bool(body.get("yes_dollars") or body.get("no_dollars"))
        raw_yes = body.get("yes_dollars") if dollars else body.get("yes", [])
        raw_no = body.get("no_dollars") if dollars else body.get("no", [])
        yes: dict[int, int] = {}
        no: dict[int, int] = {}
        for target, rows in ((yes, raw_yes), (no, raw_no)):
            for row in rows or []:
                lvl = _level_from_pair(row, dollars=dollars)
                if lvl is not None:
                    target[lvl.price_cents] = lvl.size
        async with self._books_lock:
            self._books[ticker] = {"yes": yes, "no": no, "ts": timeutils.utc_now()}

    async def _apply_delta(self, body: dict[str, Any]) -> None:
        ticker = body.get("market_ticker")
        side = body.get("side")
        price = body.get("price")
        delta = body.get("delta")
        if not ticker or side not in ("yes", "no") or price is None or delta is None:
            return
        async with self._books_lock:
            state = self._books.setdefault(
                ticker, {"yes": {}, "no": {}, "ts": timeutils.utc_now()}
            )
            levels: dict[int, int] = state[side]
            new_size = levels.get(int(price), 0) + int(delta)
            if new_size <= 0:
                levels.pop(int(price), None)
            else:
                levels[int(price)] = new_size
            state["ts"] = timeutils.utc_now()


# ---------------------------------------------------------------------- #
# Parsing helpers
# ---------------------------------------------------------------------- #
def _level_from_pair(pair: Any, *, dollars: bool) -> Optional[PriceLevel]:
    """One ``[price, size]`` pair from either order-book encoding.

    ``dollars`` selects the newer ``orderbook_fp`` encoding, where both fields
    are decimal STRINGS: price in dollars (``"0.4000"`` = 40c) and size as a
    decimal that is sometimes fractional (``"20.29"``). Sizes are floored to
    whole contracts because that is the only quantity we can actually order; a
    level that floors to zero is dropped rather than recorded as free size.
    """
    try:
        price, size = pair[0], pair[1]
        cents = round(float(price) * 100) if dollars else int(price)
        count = int(float(size))                      # floor: partial lots are not takeable
    except (TypeError, ValueError, IndexError):
        return None
    if count <= 0 or not (0 <= cents <= 100):
        return None
    return PriceLevel(cents, count)


def _orderbook_from_kalshi(ticker: str, payload: dict[str, Any]) -> OrderBook:
    """Normalize either order-book encoding Kalshi serves.

    ⚠ THIS SILENTLY RETURNED AN EMPTY BOOK FOR WEEKS. The endpoint used to reply
    ``{"orderbook": {"yes": [[price_cents, size], ...], "no": [...]}}``. It now
    replies ``{"orderbook_fp": {"yes_dollars": [["0.4000", "947.00"], ...],
    "no_dollars": [...]}}`` and serves NOTHING under the old key -- no request
    parameter brings it back. Reading ``payload["orderbook"]`` therefore yielded
    ``{}``, every level vanished, and `best_yes_ask`/`best_no_ask` were always
    ``None``.

    The consequence was not a crash but a plausible-looking non-event: with no
    ask, Strategy 1 always took the priced-out branch and Strategy 2 never found
    a qualifying price, so BOTH strategies logged zero signals for the entire
    paper run. That was read as "the market is always fully priced" when the real
    cause was that we could not see the market at all.

    This is the second time this exact migration has bitten the project -- the
    candlestick fields moved to ``volume_fp``/``open_interest_fp`` and were read
    as ``volume``/``open_interest``, showing zero volume everywhere. Both keys are
    accepted here so a rollback in either direction is a non-event.
    """
    fp = payload.get("orderbook_fp")
    legacy = payload.get("orderbook")
    if isinstance(fp, dict) and (fp.get("yes_dollars") or fp.get("no_dollars")):
        raw_yes, raw_no, dollars = fp.get("yes_dollars"), fp.get("no_dollars"), True
    elif isinstance(legacy, dict):
        raw_yes, raw_no, dollars = legacy.get("yes"), legacy.get("no"), False
    else:
        raw_yes, raw_no, dollars = None, None, False

    def levels(rows: Any) -> tuple[PriceLevel, ...]:
        parsed = (_level_from_pair(r, dollars=dollars) for r in (rows or []))
        return tuple(lvl for lvl in parsed if lvl is not None)

    return OrderBook(
        ticker=ticker,
        timestamp=timeutils.utc_now(),
        yes_bids=tuple(sorted(levels(raw_yes),
                              key=lambda lvl: lvl.price_cents, reverse=True)),
        no_bids=tuple(sorted(levels(raw_no),
                             key=lambda lvl: lvl.price_cents, reverse=True)),
    )


_STATUS_MAP: dict[str, OrderStatus] = {
    "resting": OrderStatus.RESTING,
    "open": OrderStatus.RESTING,
    "pending": OrderStatus.PENDING,
    "executed": OrderStatus.FILLED,
    "filled": OrderStatus.FILLED,
    "canceled": OrderStatus.CANCELED,
    "cancelled": OrderStatus.CANCELED,
    "rejected": OrderStatus.REJECTED,
}


def _map_status(raw: str, remaining: Optional[int], count: Optional[int]) -> OrderStatus:
    status = _STATUS_MAP.get(raw.lower())
    if status is not None:
        if status is OrderStatus.RESTING and remaining is not None and count is not None:
            if remaining <= 0:
                return OrderStatus.FILLED
            if remaining < count:
                return OrderStatus.PARTIALLY_FILLED
        return status
    return OrderStatus.PENDING


def _order_from_kalshi(
    raw: dict[str, Any],
    *,
    fallback: Optional[LimitOrderRequest] = None,
    created_at: Optional[datetime] = None,
) -> LimitOrder:
    now = created_at or timeutils.utc_now()
    side_raw = str(raw.get("side", fallback.side.value if fallback else "yes"))
    action_raw = str(raw.get("action", fallback.action.value if fallback else "buy"))
    count = int(raw.get("initial_count", raw.get("count", fallback.count if fallback else 0)))
    remaining = raw.get("remaining_count")
    remaining_int = int(remaining) if remaining is not None else None
    filled = count - remaining_int if remaining_int is not None else 0

    side = Side(side_raw)
    if side is Side.YES:
        price = int(raw.get("yes_price", fallback.price_cents if fallback else 0))
    else:
        price = int(raw.get("no_price", fallback.price_cents if fallback else 0))

    strategy = fallback.strategy if fallback else _infer_strategy(raw)
    return LimitOrder(
        client_order_id=str(
            raw.get("client_order_id", fallback.client_order_id if fallback else "")
        ),
        ticker=str(raw.get("ticker", fallback.ticker if fallback else "")),
        venue=Venue.KALSHI,
        side=side,
        action=Action(action_raw),
        price_cents=price,
        count=count,
        strategy=strategy,
        status=_map_status(str(raw.get("status", "pending")), remaining_int, count),
        created_at_utc=now,
        updated_at_utc=now,
        exchange_order_id=raw.get("order_id"),
        filled_count=max(0, filled),
    )


def _infer_strategy(raw: dict[str, Any]) -> "Strategy":
    from .models import Strategy

    cid = str(raw.get("client_order_id", ""))
    if cid.startswith("s1-"):
        return Strategy.LOCK_IN
    if cid.startswith("s2-"):
        return Strategy.MODEL_EDGE
    return Strategy.MANUAL


def _order_context(request: LimitOrderRequest) -> dict[str, Any]:
    return {
        "client_order_id": request.client_order_id,
        "ticker": request.ticker,
        "side": request.side.value,
        "action": request.action.value,
        "price_cents": request.price_cents,
        "count": request.count,
        "strategy": request.strategy.value,
    }


def _loads(text: str) -> dict[str, Any]:
    import json

    if not text:
        return {}
    data = json.loads(text)
    return data if isinstance(data, dict) else {"data": data}
