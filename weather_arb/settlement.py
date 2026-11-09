"""Settlement reconciliation: closes the loop on paper (and live) trades.

Once a daily weather market settles, this reconciler fetches the official
YES/NO result from the exchange, records it, and logs the realized P&L for the
fills we booked against it.  It is what turns a month of dry-run activity into a
concrete, auditable track record: :mod:`weather_arb.report` reads the same
``trades`` × ``market_results`` join to produce cumulative performance.
"""

from __future__ import annotations

import asyncio
import logging

from .config import Settings
from .db import Database
from .fees import fee_cents
from .market_client import MarketClient, MarketClientError
from .pnl import trade_pnl_cents

log = logging.getLogger(__name__)


class SettlementReconciler:
    """Polls for settled markets we have traded and records their outcomes."""

    def __init__(
        self, *, db: Database, client: MarketClient, settings: Settings
    ) -> None:
        self._db = db
        self._client = client
        self._settings = settings

    async def run(self, stop_event: asyncio.Event) -> None:
        log.info(
            "settlement reconciler started",
            extra={"context": {"interval_s": self._settings.settlement_poll_seconds}},
        )
        while not stop_event.is_set():
            try:
                await self.reconcile_once()
            except asyncio.CancelledError:
                raise
            except MarketClientError as exc:
                log.warning(
                    "settlement poll transient error",
                    extra={"context": {"error": str(exc)}},
                )
            except Exception:  # noqa: BLE001 - never kill the loop
                log.exception("settlement reconciliation failed (unexpected)")
            try:
                await asyncio.wait_for(
                    stop_event.wait(),
                    timeout=self._settings.settlement_poll_seconds,
                )
            except asyncio.TimeoutError:
                pass

    async def reconcile_once(self) -> int:
        """Record results for any traded-but-unsettled markets. Returns count settled."""
        traded = await self._db.traded_tickers_without_result()
        # Also grade every market we merely PREDICTED. Shadow predictions are the
        # whole measurement apparatus (Brier leaderboard -> WA_TRADING_MODEL=auto ->
        # which model actually trades), and they need an outcome per market whether
        # or not we filled anything. In dry-run nothing fills, so restricting this
        # to traded tickers left market_results permanently empty and every
        # consumer of it silently dead.
        predicted = await self._db.predicted_tickers_without_result(
            limit=self._settings.settlement_grade_batch
        )
        seen: set[str] = set()
        tickers = [t for t in (*traded, *predicted) if not (t in seen or seen.add(t))]

        settled = 0
        for ticker in tickers:
            try:
                result, settled_at = await self._client.get_market_settlement(ticker)
            except MarketClientError:
                continue          # transient/unknown ticker: retry next pass
            if result not in ("yes", "no"):
                continue
            await self._db.record_market_result(
                ticker, result, settled_at_utc=settled_at)
            settled += 1
            if ticker in set(traded):
                await self._log_settlement(ticker, result)
        if settled:
            log.info("settlements recorded",
                     extra={"context": {"count": settled, "graded_only": settled - len(traded)}})

        graded = await self.grade_lockin_events()
        if graded:
            log.info("lock-in detections graded",
                     extra={"context": {"count": graded}})
        return settled

    async def grade_lockin_events(self, limit: int = 500) -> int:
        """Attach official settlement to EVERY recorded detection.

        The reason this is a first-class step rather than a side effect of P&L:
        `backtest --strategy 1` graded only detections that cleared the 98c price
        ceiling -- 1 of 421 in a 30-day run -- so a 3% error rate in the lock-in
        rule went unmeasured for weeks while the project recorded "421
        detections, 0 wrong calls" as verified ground truth.

        A detection is a detection whether or not it was tradeable. Grading is
        one-way: `grade_lockin_event` refuses to overwrite an existing grade.
        """
        pending = await self._db.ungraded_lockin_events(limit=limit)
        if not pending:
            return 0
        graded = 0
        for event in pending:
            ticker = str(event["ticker"])
            result = await self._db.get_market_result(ticker)
            if result not in ("yes", "no"):
                # Not settled yet, or settled but not yet recorded. Either way it
                # is picked up on a later pass -- events are never dropped.
                continue
            settled_at = await self._db.get_market_settled_at(ticker)
            await self._db.grade_lockin_event(
                int(event["id"]), result=result, settled_at_utc=settled_at,
                correct=str(event["side"]) == result,
            )
            graded += 1
        return graded

    async def _log_settlement(self, ticker: str, result: str) -> None:
        trades = await self._db.trades_for_ticker(ticker)
        coeff = self._settings.kalshi_fee_coefficient
        gross_cents = sum(
            trade_pnl_cents(
                side=t.side.value,
                action=t.action.value,
                price_cents=t.price_cents,
                count=t.count,
                result=result,
            )
            for t in trades
        )
        fees = sum(fee_cents(t.price_cents, t.count, coeff) for t in trades)
        log.info(
            "market settled",
            extra={"context": {
                "ticker": ticker,
                "result": result,
                "fills": len(trades),
                "gross_pnl_usd": round(gross_cents / 100.0, 2),
                "fees_usd": round(fees / 100.0, 2),
                "net_pnl_usd": round((gross_cents - fees) / 100.0, 2),
            }},
        )
