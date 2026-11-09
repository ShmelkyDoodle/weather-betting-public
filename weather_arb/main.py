"""Main orchestrator: wiring, reconciliation, supervision and graceful shutdown.

Run with::

    python -m weather_arb.main

Startup sequence
----------------
1. Load :class:`~weather_arb.config.Settings` from the environment and configure
   structured JSON logging.
2. Open the WAL SQLite ledger and create the schema.
3. **Reconcile** the persisted ``open_limit_orders`` against live exchange state
   so a crash mid-session cannot leave phantom or unmanaged orders.
4. Launch the background loops -- METAR poller, market WebSocket feed and the arb
   evaluator -- and supervise them until a shutdown signal arrives.

Shutdown sequence (SIGINT / SIGTERM)
------------------------------------
1. Signal the ``stop_event`` so every loop drains and exits its next iteration.
2. Cancel the background tasks (stops new orders from being generated).
3. Cancel any still-resting limit orders on the exchange (configurable).
4. Close the WebSocket/HTTP sessions and the database connection.
"""

from __future__ import annotations

import asyncio
import logging
import signal

import aiohttp

from .alerts import AlertManager
from .arb_engine import ArbitrageEngine
from .book_recorder import BookRecorder
from .climatology import ClimatologyClient
from .config import Settings
from .db import Database
from .forecast_ingest import ForecastClient
from .intraday import IntradayCurves
from .logging_setup import configure_logging
from .market_client import KalshiClient, MarketClient, MarketClientError
from .market_discovery import MarketDiscovery, MarketRegistry
from .metar_ingest import MetarPoller
from .nws_obs import NwsObservationPoller
from .onemin_ingest import OneMinutePoller
from .mos_ingest import MosClient
from .models import LimitOrder, OrderStatus, Strategy, Trade
from .report import build_summary
from .risk_manager import RiskManager
from .settlement import SettlementReconciler
from . import timeutils

log = logging.getLogger(__name__)


class Application:
    """Owns process-wide resources and the run/shutdown lifecycle."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._stop_event = asyncio.Event()
        self._db = Database(settings.db_path)
        self._weather_session: aiohttp.ClientSession | None = None
        self._client: MarketClient | None = None
        self._tasks: list[asyncio.Task[None]] = []

    # ------------------------------------------------------------------ #
    # Lifecycle
    # ------------------------------------------------------------------ #
    async def run(self) -> None:
        configure_logging(self._settings.log_level)
        log.info(
            "starting weather-arb",
            extra={"context": {
                "dry_run": self._settings.dry_run,
                "stations": [s.icao for s in self._settings.stations],
                "markets": [m.ticker for m in self._settings.markets],
            }},
        )
        # Make the live posture impossible to miss in the log. dry_run is global,
        # so the only thing separating "Strategy 1 only" from "everything trades"
        # is allow_live_model_edge.
        if not self._settings.dry_run:
            cleared = ["lock_in"] + (
                ["model_edge"] if self._settings.allow_live_model_edge else [])
            log.warning(
                "LIVE TRADING ENABLED — real money at risk",
                extra={"context": {
                    "strategies_cleared_live": cleared,
                    "model_edge_blocked": not self._settings.allow_live_model_edge,
                }},
            )
        try:
            await self._db.connect()
            self._weather_session = _build_weather_session(self._settings)
            self._client = KalshiClient(self._settings)

            risk = RiskManager(settings=self._settings, db=self._db)
            mos = MosClient(session=self._weather_session, settings=self._settings)
            climo = ClimatologyClient(session=self._weather_session, settings=self._settings)
            # Curves build lazily in the background on first use, so this never
            # delays startup or an evaluation cycle.
            intraday = IntradayCurves(session=self._weather_session, settings=self._settings)
            forecast = ForecastClient(
                session=self._weather_session, settings=self._settings,
                mos=mos, climo=climo, intraday=intraday,
            )
            registry = MarketRegistry(self._settings.markets)
            discovery: MarketDiscovery | None = None
            if self._settings.enable_discovery:
                discovery = MarketDiscovery(
                    client=self._client, settings=self._settings, registry=registry
                )
                # Populate markets before the engine starts evaluating.
                try:
                    await discovery.refresh()
                except MarketClientError:
                    log.exception("initial market discovery failed; starting empty")

            # Discovery-experiment substrate. Observation only: it samples
            # order books on a control/armed/event schedule and records
            # detections, and it reaches no order path. Built before the engine
            # because the engine hands it event follow-ups to schedule.
            recorder = BookRecorder(
                settings=self._settings, db=self._db, client=self._client,
                registry=registry, risk=risk,
            )
            engine = ArbitrageEngine(
                settings=self._settings,
                db=self._db,
                client=self._client,
                risk=risk,
                forecast=forecast,
                registry=registry,
                # Strategy 1 fires rarely and is invisible in dry-run (the ledger's
                # `trades` table can never fill -- see scripts/query_lockins.sql),
                # so a lock-in pushes to the phone the moment it clears risk.
                alerts=AlertManager(self._settings),
                recorder=recorder,
            )
            poller = MetarPoller(
                session=self._weather_session,
                db=self._db,
                settings=self._settings,
                on_new_high=engine.on_new_high,
            )
            # Secondary observation feed. SHADOW ONLY -- its detections are
            # recorded and arm the recorder; they can never reach _submit.
            nws5 = NwsObservationPoller(
                session=self._weather_session, db=self._db,
                settings=self._settings,
                on_shadow_lockin=engine.observe_shadow_lockin,
            )
            onemin = None
            if self._settings.enable_1min_lockin:
                onemin = OneMinutePoller(
                    session=self._weather_session,
                    db=self._db,
                    settings=self._settings,
                    on_new_high=engine.on_new_high,
                )
            settlement = SettlementReconciler(
                db=self._db, client=self._client, settings=self._settings
            )

            await self._reconcile(self._client)
            self._install_signal_handlers()
            await self._supervise(poller, onemin, engine, self._client, discovery,
                                  settlement, registry, recorder, nws5)
        finally:
            await self._shutdown()

    async def _supervise(
        self,
        poller: MetarPoller,
        onemin: OneMinutePoller | None,
        engine: ArbitrageEngine,
        client: MarketClient,
        discovery: MarketDiscovery | None,
        settlement: SettlementReconciler,
        registry: MarketRegistry,
        recorder: BookRecorder,
        nws5: NwsObservationPoller,
    ) -> None:
        tickers = registry.tickers()
        self._tasks = [
            asyncio.create_task(poller.run(self._stop_event), name="metar_poller"),
            asyncio.create_task(engine.run(self._stop_event), name="arb_evaluator"),
            asyncio.create_task(
                client.run_websocket(self._stop_event, tickers), name="market_feed"
            ),
            asyncio.create_task(
                settlement.run(self._stop_event), name="settlement_reconciler"
            ),
            asyncio.create_task(
                recorder.run(self._stop_event), name="book_recorder"
            ),
            asyncio.create_task(
                nws5.run(self._stop_event), name="nws_5min_poller"
            ),
        ]
        if onemin is not None:
            self._tasks.append(
                asyncio.create_task(onemin.run(self._stop_event), name="onemin_augmenter")
            )
        if discovery is not None:
            self._tasks.append(
                asyncio.create_task(discovery.run(self._stop_event), name="market_discovery")
            )
        self._tasks.append(
            asyncio.create_task(self._heartbeat_loop(registry), name="heartbeat")
        )
        # Block until a shutdown signal arrives or any supervised loop exits.
        waiter = asyncio.create_task(self._stop_event.wait(), name="stop_waiter")
        done, _pending = await asyncio.wait(
            {*self._tasks, waiter}, return_when=asyncio.FIRST_COMPLETED
        )
        waiter.cancel()
        for task in done:
            if task is waiter or task.cancelled():
                continue
            exc = task.exception()
            if exc is not None:
                log.error(
                    "supervised task exited with error",
                    extra={"context": {"task": task.get_name(), "error": str(exc)}},
                )
        self._stop_event.set()

    async def _heartbeat_loop(self, registry: MarketRegistry) -> None:
        """Emit a periodic one-line status + running net-P&L summary."""
        interval = self._settings.heartbeat_seconds
        while not self._stop_event.is_set():
            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=interval)
            except asyncio.TimeoutError:
                await self._emit_heartbeat(registry)

    async def _emit_heartbeat(self, registry: MarketRegistry) -> None:
        try:
            open_orders = await self._db.get_open_orders()
            # Read-only summary on a worker thread (WAL permits concurrent reads).
            summary = await asyncio.to_thread(
                build_summary,
                self._settings.db_path,
                fee_coefficient=self._settings.kalshi_fee_coefficient,
            )
            totals = summary["totals"]
            log.info(
                "heartbeat",
                extra={"context": {
                    "markets_tracked": len(registry.all()),
                    "working_orders": len(open_orders),
                    "fills": totals["fills"],
                    "settled_markets": totals["settled_markets"],
                    "net_pnl_usd": round(totals["net_usd"], 2),
                }},
            )
        except Exception:  # noqa: BLE001 - heartbeat must never disrupt trading
            log.exception("heartbeat failed")

    async def _shutdown(self) -> None:
        if self._stop_event.is_set() is False:
            self._stop_event.set()

        # Stop generating new orders first.
        for task in self._tasks:
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)

        # Cancel any still-resting limit orders so we leave no unmanaged exposure.
        if self._settings.cancel_orders_on_shutdown and self._client is not None:
            try:
                await self._cancel_resting_orders(self._client)
            except Exception:  # noqa: BLE001 - best-effort during shutdown
                log.exception("failed cancelling resting orders on shutdown")

        if self._client is not None:
            await self._client.close()
        if self._weather_session is not None and not self._weather_session.closed:
            await self._weather_session.close()
        await self._db.close()
        log.info("shutdown complete")

    def _install_signal_handlers(self) -> None:
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, self._on_signal, sig)
            except NotImplementedError:
                # Fallback for platforms without loop signal support.
                signal.signal(sig, lambda *_: self._stop_event.set())

    def _on_signal(self, sig: signal.Signals) -> None:
        log.info("shutdown signal received", extra={"context": {"signal": sig.name}})
        self._stop_event.set()

    # ------------------------------------------------------------------ #
    # Crash-recovery reconciliation
    # ------------------------------------------------------------------ #
    async def _reconcile(self, client: MarketClient) -> None:
        """Bring the ledger's open orders into agreement with the exchange."""
        open_orders = await self._db.get_open_orders()
        log.info(
            "reconciling open orders",
            extra={"context": {"db_open": len(open_orders),
                               "dry_run": self._settings.dry_run}},
        )
        if self._settings.dry_run:
            # No live exchange state to reconcile against; nothing simulated
            # order can be trusted across restarts, so retire them.
            for order in open_orders:
                await self._db.set_order_status(
                    order.client_order_id, OrderStatus.CANCELED
                )
            return

        try:
            resting = await client.get_open_exchange_orders()
        except MarketClientError:
            log.exception("could not fetch exchange orders; skipping reconciliation")
            return
        resting_by_client = {o.client_order_id: o for o in resting if o.client_order_id}
        resting_by_exchange = {o.exchange_order_id: o for o in resting if o.exchange_order_id}
        seen_client_ids: set[str] = set()

        for order in open_orders:
            live = None
            if order.exchange_order_id and order.exchange_order_id in resting_by_exchange:
                live = resting_by_exchange[order.exchange_order_id]
            elif order.client_order_id in resting_by_client:
                live = resting_by_client[order.client_order_id]

            if live is not None:
                seen_client_ids.add(live.client_order_id)
                await self._db.set_order_status(
                    order.client_order_id,
                    live.status,
                    filled_count=live.filled_count,
                    exchange_order_id=live.exchange_order_id,
                )
                continue

            # Not resting on the exchange: query directly to distinguish
            # filled-away vs. never-placed / already-canceled.
            await self._resolve_missing_order(client, order)

        # Adopt orphan exchange orders (resting but absent from the ledger) so
        # they are tracked and can be cancelled on shutdown.
        for client_id, live in resting_by_client.items():
            if client_id in seen_client_ids:
                continue
            if await self._db.get_order(client_id) is not None:
                continue
            log.warning(
                "adopting orphan exchange order",
                extra={"context": {"client_order_id": client_id,
                                   "exchange_order_id": live.exchange_order_id}},
            )
            live.strategy = Strategy.MANUAL
            await self._db.upsert_open_order(live)

    async def _resolve_missing_order(
        self, client: MarketClient, order: LimitOrder
    ) -> None:
        """Determine the fate of a DB-open order not currently resting."""
        live = None
        if order.exchange_order_id:
            try:
                live = await client.get_order_by_id(order.exchange_order_id)
            except MarketClientError:
                live = None

        if live is None:
            # Never acknowledged or long gone; retire it from the open set.
            await self._db.set_order_status(order.client_order_id, OrderStatus.CANCELED)
            log.info(
                "reconciled order as closed (not found on exchange)",
                extra={"context": {"client_order_id": order.client_order_id}},
            )
            return

        await self._db.set_order_status(
            order.client_order_id,
            live.status,
            filled_count=live.filled_count,
            exchange_order_id=live.exchange_order_id,
        )
        if live.status is OrderStatus.FILLED and live.filled_count > 0:
            if not await self._db.has_trade(order.client_order_id):
                await self._db.record_trade(
                    Trade(
                        client_order_id=live.client_order_id,
                        exchange_order_id=live.exchange_order_id,
                        venue=live.venue,
                        ticker=live.ticker,
                        side=live.side,
                        action=live.action,
                        price_cents=live.price_cents,
                        count=live.filled_count,
                        strategy=live.strategy,
                        executed_at_utc=timeutils.utc_now(),
                    )
                )

    async def _cancel_resting_orders(self, client: MarketClient) -> None:
        open_orders = await self._db.get_open_orders()
        if not open_orders:
            return
        log.info(
            "cancelling resting orders on shutdown",
            extra={"context": {"count": len(open_orders)}},
        )
        for order in open_orders:
            if not order.exchange_order_id:
                await self._db.set_order_status(
                    order.client_order_id, OrderStatus.CANCELED
                )
                continue
            ok = await client.cancel_order(order.exchange_order_id)
            if ok:
                await self._db.set_order_status(
                    order.client_order_id, OrderStatus.CANCELED
                )


def _build_weather_session(settings: Settings) -> aiohttp.ClientSession:
    """HTTP session for NOAA/NWS/IEM endpoints (no auth, shared UA + timeout)."""
    timeout = aiohttp.ClientTimeout(total=20.0)
    return aiohttp.ClientSession(
        timeout=timeout,
        headers={
            "User-Agent": settings.http_user_agent,
            "Accept": "application/json, application/geo+json",
        },
    )


async def amain() -> None:
    settings = Settings.from_env()
    app = Application(settings)
    await app.run()


def main() -> None:
    try:
        asyncio.run(amain())
    except KeyboardInterrupt:
        # A second Ctrl-C during shutdown lands here; exit quietly.
        pass


if __name__ == "__main__":
    main()
