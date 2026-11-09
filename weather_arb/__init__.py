"""Weather arbitrage trading system.

A highly concurrent, asyncio-based engine that executes low-risk weather
derivative arbitrage on prediction markets (Kalshi / Polymarket) using official
meteorological observations (METAR/ASOS) and short-range forecasts (HRRR/NBM).

The package is intentionally modular; each module owns a single concern:

* :mod:`weather_arb.config`        -- typed configuration + environment loading.
* :mod:`weather_arb.timeutils`     -- UTC-first time helpers and LST climate-day logic.
* :mod:`weather_arb.models`        -- immutable domain models and enums.
* :mod:`weather_arb.logging_setup` -- structured JSON logging.
* :mod:`weather_arb.db`            -- aiosqlite (WAL) ledger and reconciliation store.
* :mod:`weather_arb.metar_ingest`  -- real-time METAR/ASOS poller with glitch protection.
* :mod:`weather_arb.forecast_ingest` -- HRRR/NBM gridpoint forecast + probability model.
* :mod:`weather_arb.market_client` -- Kalshi REST + WebSocket client (limit orders only).
* :mod:`weather_arb.risk_manager`  -- bankroll caps, limit-only enforcement, sizing.
* :mod:`weather_arb.arb_engine`    -- Strategy 1 (lock-in) + Strategy 2 (model edge).
* :mod:`weather_arb.main`          -- orchestrator, reconciliation, graceful shutdown.
"""

from __future__ import annotations

__all__ = ["__version__"]

__version__ = "1.0.0"
