"""Async SQLite ledger (``aiosqlite``) with WAL journaling.

Responsibilities
----------------
* Persist the running daily ``T_max`` per station/climate-day (atomic upsert).
* Journal every observation (``station_logs``) for audit + backtesting.
* Track working limit orders (``open_limit_orders``) so the process can
  reconcile against the exchange after a crash.
* Append realized fills to an immutable ``trades`` ledger.

Concurrency model
-----------------
A single :class:`aiosqlite.Connection` is shared process-wide.  ``aiosqlite``
funnels every statement through one background thread, so operations are already
serialized; we additionally guard *multi-statement* writes with an
:class:`asyncio.Lock` to keep them atomic with respect to each other.  WAL mode
(`journal_mode=WAL`) lets the poller keep writing observations while the
reconciler reads, and ``synchronous=NORMAL`` is the recommended durability/
throughput trade-off for WAL.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import Optional

import aiosqlite

from .calibration import CalibrationMap, deserialize, serialize

from . import timeutils
from .models import (
    Action,
    LimitOrder,
    OrderStatus,
    Side,
    Strategy,
    Trade,
    Venue,
)

log = logging.getLogger(__name__)


_SCHEMA = """
CREATE TABLE IF NOT EXISTS daily_tmax (
    icao             TEXT NOT NULL,
    climate_date     TEXT NOT NULL,
    tmax_f           REAL NOT NULL,
    observed_at_utc  TEXT NOT NULL,
    updated_at_utc   TEXT NOT NULL,
    PRIMARY KEY (icao, climate_date)
);

CREATE TABLE IF NOT EXISTS station_logs (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    icao             TEXT NOT NULL,
    observed_at_utc  TEXT NOT NULL,
    climate_date     TEXT NOT NULL,
    temp_f           REAL,
    dewpoint_f       REAL,
    raw_metar        TEXT,
    accepted         INTEGER NOT NULL DEFAULT 1,
    inserted_at_utc  TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_station_logs_icao_ts
    ON station_logs (icao, observed_at_utc);

CREATE TABLE IF NOT EXISTS open_limit_orders (
    client_order_id   TEXT PRIMARY KEY,
    exchange_order_id TEXT,
    venue             TEXT NOT NULL,
    ticker            TEXT NOT NULL,
    side              TEXT NOT NULL,
    action            TEXT NOT NULL,
    price_cents       INTEGER NOT NULL,
    count             INTEGER NOT NULL,
    filled_count      INTEGER NOT NULL DEFAULT 0,
    status            TEXT NOT NULL,
    strategy          TEXT NOT NULL,
    model_prob        REAL,
    created_at_utc    TEXT NOT NULL,
    updated_at_utc    TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_open_orders_ticker
    ON open_limit_orders (ticker, status);

CREATE TABLE IF NOT EXISTS trades (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    client_order_id   TEXT,
    exchange_order_id TEXT,
    venue             TEXT NOT NULL,
    ticker            TEXT NOT NULL,
    side              TEXT NOT NULL,
    action            TEXT NOT NULL,
    price_cents       INTEGER NOT NULL,
    count             INTEGER NOT NULL,
    notional_usd      REAL NOT NULL,
    strategy          TEXT NOT NULL,
    model_prob        REAL,
    executed_at_utc   TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_trades_ticker
    ON trades (ticker, executed_at_utc);

CREATE TABLE IF NOT EXISTS market_results (
    ticker           TEXT PRIMARY KEY,
    result           TEXT NOT NULL,
    settled_at_utc   TEXT,
    recorded_at_utc  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS model_predictions (
    ticker           TEXT NOT NULL,
    model            TEXT NOT NULL,
    climate_date     TEXT,
    prob             REAL NOT NULL,
    forecast_high_f  REAL,
    sigma_f          REAL,
    created_at_utc   TEXT NOT NULL,
    PRIMARY KEY (ticker, model)
);

CREATE TABLE IF NOT EXISTS daily_forecasts (
    icao             TEXT NOT NULL,
    model            TEXT NOT NULL,
    climate_date     TEXT NOT NULL,
    forecast_high_f  REAL NOT NULL,
    recorded_at_utc  TEXT NOT NULL,
    PRIMARY KEY (icao, model, climate_date)
);

-- Append-only time series of shadow predictions (throttled), so the Strategy-2
-- backtest can enter at the first snapshot an edge appears (full lead time)
-- rather than only the last stored view. model_predictions stays the latest view.
CREATE TABLE IF NOT EXISTS model_prediction_history (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker           TEXT NOT NULL,
    model            TEXT NOT NULL,
    climate_date     TEXT,
    prob             REAL NOT NULL,
    forecast_high_f  REAL,
    sigma_f          REAL,
    created_at_utc   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_pred_hist_tmc
    ON model_prediction_history (ticker, model, created_at_utc);

-- Cached per-model Strategy-2 backtest P&L (written by `backtest --strategy 2
-- --persist`), read by the auto_pnl trading-model selector.
CREATE TABLE IF NOT EXISTS model_pnl (
    model            TEXT PRIMARY KEY,
    trades           INTEGER NOT NULL,
    net_usd          REAL NOT NULL,
    hit_rate         REAL,
    updated_at_utc   TEXT NOT NULL
);

-- Cached per-model isotonic probability recalibration (written by `backtest
-- --strategy 2 --calibrate --persist`; see weather_arb.calibration). Read by the
-- live engine when WA_ENABLE_PROBABILITY_CALIBRATION is on.
CREATE TABLE IF NOT EXISTS model_calibration (
    model            TEXT PRIMARY KEY,
    breakpoints      TEXT NOT NULL,
    n_samples        INTEGER NOT NULL,
    updated_at_utc   TEXT NOT NULL
);

-- ===================================================================== --
-- Strategy-1 discovery experiment (2026-08-26). Observation-only tables.
--
-- These exist because the system ran five weeks and accumulated ZERO
-- observations of the quantity the whole thesis depends on: the price and
-- depth available at the instant a lock-in becomes provable. Nothing here
-- feeds a trading decision; it is a measurement substrate.
-- ===================================================================== --

-- One IMMUTABLE row per (ticker, climate_date, detection source). Written the
-- first time the rule calls a market certain -- BEFORE any gate -- so the
-- denominator is detections, not tradeable detections. The old
-- `backtest --strategy 1` graded only the ~1-in-421 that cleared the price
-- ceiling, which is exactly how "421 detections, 0 wrong calls" survived
-- unchallenged while the true error rate was ~3%.
--
-- Settlement columns are the ONLY ones ever updated after insert.
CREATE TABLE IF NOT EXISTS lockin_events (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker                TEXT NOT NULL,
    series                TEXT NOT NULL,
    icao                  TEXT NOT NULL,
    climate_date          TEXT NOT NULL,
    -- 'metar' = the hourly feed that drives trading; 'nws5min' = the free
    -- secondary 5-minute product (shadow only, never trades).
    source                TEXT NOT NULL,
    side                  TEXT NOT NULL,      -- yes | no
    action                TEXT NOT NULL,      -- buy
    strike_type           TEXT NOT NULL,      -- greater | less | between
    floor_strike_f        REAL,
    cap_strike_f          REAL,
    boundary_f            REAL NOT NULL,      -- the strike crossed
    certainty_bound_f     REAL NOT NULL,      -- observed max at which certainty begins
    observed_tmax_f       REAL NOT NULL,
    lockin_margin_f       REAL NOT NULL,      -- the configured margin at detection time
    margin_f              REAL NOT NULL,      -- observed - boundary_f (strike-relative)
    excess_f              REAL NOT NULL,      -- observed - certainty_bound_f (<0 = fired early)
    observation_at_utc    TEXT,               -- the observation's VALID time
    ingest_at_utc         TEXT,               -- when we received it
    detected_at_utc       TEXT NOT NULL,      -- when the rule fired
    observation_age_s     REAL,               -- detected - observation valid time
    -- Filled in by the settlement reconciler. Every detection is graded.
    settled_result        TEXT,               -- yes | no
    settled_at_utc        TEXT,
    call_correct          INTEGER,            -- 1 | 0 | NULL until settled
    graded_at_utc         TEXT,
    UNIQUE (ticker, climate_date, source)
);
CREATE INDEX IF NOT EXISTS ix_lockin_events_detected ON lockin_events(detected_at_utc);
CREATE INDEX IF NOT EXISTS ix_lockin_events_ungraded
    ON lockin_events(call_correct) WHERE call_correct IS NULL;

-- Append-only order-book samples. Written for event windows AND on a control
-- cadence for every tracked market, so "is the discount event-driven or just
-- normal microstructure?" is answerable rather than assumed.
--
-- The RAW ladder is stored, not only derived metrics. Every derived quantity in
-- this project that was stored without its inputs (daily_tmax in place, `prob`
-- without its truncation bound) later had to be reconstructed by inversion.
CREATE TABLE IF NOT EXISTS book_snapshots (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker                TEXT NOT NULL,
    event_id              INTEGER,            -- NULL for control samples
    -- control | armed | signal | offset | settle_check
    reason                TEXT NOT NULL,
    offset_seconds        REAL,               -- signed, vs the event; NULL if control
    requested_at_utc      TEXT NOT NULL,
    received_at_utc       TEXT NOT NULL,
    round_trip_ms         REAL NOT NULL,
    -- The gate the live engine WOULD have reached with this book, so the
    -- rejection reason is reconstructable at every instant, not just the first.
    gate_outcome          TEXT,
    best_yes_bid_cents    INTEGER,
    best_yes_bid_size     INTEGER,
    best_yes_ask_cents    INTEGER,
    best_yes_ask_size     INTEGER,
    best_no_bid_cents     INTEGER,
    best_no_bid_size      INTEGER,
    best_no_ask_cents     INTEGER,
    best_no_ask_size      INTEGER,
    spread_cents          INTEGER,
    yes_depth_total       INTEGER NOT NULL,
    no_depth_total        INTEGER NOT NULL,
    yes_depth_to_ceiling  INTEGER NOT NULL,   -- sweepable YES size at <= ceiling
    no_depth_to_ceiling   INTEGER NOT NULL,
    ceiling_cents         INTEGER NOT NULL,
    n_yes_levels          INTEGER NOT NULL,
    n_no_levels           INTEGER NOT NULL,
    market_status         TEXT,
    ladder_json           TEXT NOT NULL,      -- {"yes_bids":[[p,s]...],"no_bids":[...]}
    FOREIGN KEY (event_id) REFERENCES lockin_events(id)
);
CREATE INDEX IF NOT EXISTS ix_book_snapshots_event
    ON book_snapshots(event_id, offset_seconds);
CREATE INDEX IF NOT EXISTS ix_book_snapshots_ticker_time
    ON book_snapshots(ticker, received_at_utc);

-- Executable economics per (snapshot, side, size). Denormalised so the analysis
-- is a GROUP BY rather than a JSON walk. Written for event and armed snapshots;
-- control snapshots keep the raw ladder and are swept on demand.
--
-- target_size = -1 means "the whole available ladder".
CREATE TABLE IF NOT EXISTS sweep_metrics (
    snapshot_id           INTEGER NOT NULL,
    side                  TEXT NOT NULL,      -- the side we would BUY
    target_size           INTEGER NOT NULL,
    fillable_size         INTEGER NOT NULL,   -- obtainable at <= ceiling
    book_cleared          INTEGER NOT NULL,   -- 1 = ladder could not supply target
    top_ask_cents         INTEGER,
    vwap_cents            REAL,
    slippage_cents        REAL,               -- vwap - top_ask: the depth illusion
    fee_cents             REAL NOT NULL,
    net_if_correct_usd    REAL NOT NULL,
    net_if_wrong_usd      REAL NOT NULL,
    PRIMARY KEY (snapshot_id, side, target_size),
    FOREIGN KEY (snapshot_id) REFERENCES book_snapshots(id)
);

-- Every observation from every source, kept so the 5-minute feed can be graded
-- against METAR rather than trusted. Its 1-degC quantization (1.8F) is COARSER
-- than the +-0.08F METAR error that caused the lock-in defect, so it must be
-- proven not to manufacture false crossings before it is allowed near a trade.
CREATE TABLE IF NOT EXISTS source_observations (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    icao                  TEXT NOT NULL,
    source                TEXT NOT NULL,      -- metar | nws5min | nws_hourly
    climate_date          TEXT NOT NULL,
    observed_at_utc       TEXT NOT NULL,
    received_at_utc       TEXT NOT NULL,
    publication_age_s     REAL NOT NULL,      -- received - observed
    temp_f                REAL,
    raw_value             TEXT,               -- as served, before conversion
    precision_f           REAL,               -- 0.18 tenths-degC, 1.8 whole-degC
    running_max_f         REAL,               -- this source's running max after it
    UNIQUE (icao, source, observed_at_utc)
);
CREATE INDEX IF NOT EXISTS ix_source_obs_day
    ON source_observations(icao, climate_date, source);
"""


class Database:
    """Async wrapper around the SQLite ledger.

    Use as an async context manager::

        async with Database(path) as db:
            await db.update_tmax(...)
    """

    def __init__(self, path: str) -> None:
        self._path = path
        self._conn: Optional[aiosqlite.Connection] = None
        self._write_lock = asyncio.Lock()

    # ------------------------------------------------------------------ #
    # Lifecycle
    # ------------------------------------------------------------------ #
    async def __aenter__(self) -> "Database":
        await self.connect()
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()

    async def connect(self) -> None:
        """Open the connection, apply PRAGMAs, and create the schema."""
        conn = await aiosqlite.connect(self._path)
        conn.row_factory = aiosqlite.Row
        # Durability / concurrency tuning.
        await conn.execute("PRAGMA journal_mode=WAL;")
        await conn.execute("PRAGMA synchronous=NORMAL;")
        await conn.execute("PRAGMA foreign_keys=ON;")
        await conn.execute("PRAGMA busy_timeout=5000;")
        await conn.executescript(_SCHEMA)
        await conn.commit()
        self._conn = conn
        await self._migrate()
        log.info(
            "database ready",
            extra={"context": {"path": self._path, "journal_mode": "WAL"}},
        )

    async def _migrate(self) -> None:
        """Additive migrations so ledgers created by older versions keep working."""
        for table, column, decl in (
            ("open_limit_orders", "model_prob", "REAL"),
            ("trades", "model_prob", "REAL"),
        ):
            if column not in await self._column_names(table):
                await self._c.execute(
                    f"ALTER TABLE {table} ADD COLUMN {column} {decl}"
                )
        await self._c.commit()

    async def _column_names(self, table: str) -> set[str]:
        async with self._c.execute(f"PRAGMA table_info({table})") as cur:
            rows = await cur.fetchall()
        return {str(row["name"]) for row in rows}

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    @property
    def _c(self) -> aiosqlite.Connection:
        if self._conn is None:
            raise RuntimeError("Database.connect() must be awaited before use")
        return self._conn

    # ------------------------------------------------------------------ #
    # Observations + running T_max
    # ------------------------------------------------------------------ #
    async def record_observation(
        self,
        *,
        icao: str,
        observed_at_utc: datetime,
        climate_date: str,
        temp_f: Optional[float],
        dewpoint_f: Optional[float],
        raw_metar: str,
        accepted: bool,
    ) -> None:
        """Append one observation to the audit log (never overwrites)."""
        await self._c.execute(
            """
            INSERT INTO station_logs
                (icao, observed_at_utc, climate_date, temp_f, dewpoint_f,
                 raw_metar, accepted, inserted_at_utc)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                icao,
                timeutils.isoformat_utc(observed_at_utc),
                climate_date,
                temp_f,
                dewpoint_f,
                raw_metar,
                1 if accepted else 0,
                timeutils.isoformat_utc(timeutils.utc_now()),
            ),
        )
        await self._c.commit()

    async def update_tmax(
        self,
        *,
        icao: str,
        climate_date: str,
        temp_f: float,
        observed_at_utc: datetime,
    ) -> float:
        """Atomically raise the stored ``T_max`` and return the current value.

        Uses an ``ON CONFLICT ... DO UPDATE`` upsert so concurrent writers can
        never lose a higher reading (SQLite evaluates ``MAX`` server-side).  The
        ``observed_at_utc`` is only advanced when this reading actually sets a
        new high.
        """
        now = timeutils.isoformat_utc(timeutils.utc_now())
        obs = timeutils.isoformat_utc(observed_at_utc)
        async with self._write_lock:
            await self._c.execute(
                """
                INSERT INTO daily_tmax
                    (icao, climate_date, tmax_f, observed_at_utc, updated_at_utc)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(icao, climate_date) DO UPDATE SET
                    observed_at_utc = CASE
                        WHEN excluded.tmax_f > daily_tmax.tmax_f
                        THEN excluded.observed_at_utc
                        ELSE daily_tmax.observed_at_utc END,
                    tmax_f = MAX(daily_tmax.tmax_f, excluded.tmax_f),
                    updated_at_utc = excluded.updated_at_utc
                """,
                (icao, climate_date, temp_f, obs, now),
            )
            await self._c.commit()
        current = await self.get_tmax(icao, climate_date)
        # ``current`` cannot be None immediately after an upsert.
        return current if current is not None else temp_f

    async def get_tmax(self, icao: str, climate_date: str) -> Optional[float]:
        """Return the stored running max for a station/day, or ``None``."""
        async with self._c.execute(
            "SELECT tmax_f FROM daily_tmax WHERE icao = ? AND climate_date = ?",
            (icao, climate_date),
        ) as cur:
            row = await cur.fetchone()
        return float(row["tmax_f"]) if row is not None else None

    # ------------------------------------------------------------------ #
    # Open limit orders
    # ------------------------------------------------------------------ #
    async def upsert_open_order(self, order: LimitOrder) -> None:
        """Insert or update a working order keyed by ``client_order_id``."""
        async with self._write_lock:
            await self._c.execute(
                """
                INSERT INTO open_limit_orders
                    (client_order_id, exchange_order_id, venue, ticker, side,
                     action, price_cents, count, filled_count, status, strategy,
                     model_prob, created_at_utc, updated_at_utc)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(client_order_id) DO UPDATE SET
                    exchange_order_id = excluded.exchange_order_id,
                    price_cents       = excluded.price_cents,
                    count             = excluded.count,
                    filled_count      = excluded.filled_count,
                    status            = excluded.status,
                    updated_at_utc    = excluded.updated_at_utc
                """,
                (
                    order.client_order_id,
                    order.exchange_order_id,
                    order.venue.value,
                    order.ticker,
                    order.side.value,
                    order.action.value,
                    order.price_cents,
                    order.count,
                    order.filled_count,
                    order.status.value,
                    order.strategy.value,
                    order.model_prob,
                    timeutils.isoformat_utc(order.created_at_utc),
                    timeutils.isoformat_utc(order.updated_at_utc),
                ),
            )
            await self._c.commit()

    async def set_order_status(
        self,
        client_order_id: str,
        status: OrderStatus,
        *,
        filled_count: Optional[int] = None,
        exchange_order_id: Optional[str] = None,
    ) -> None:
        """Update the status (and optionally fill count / exchange id) of an order."""
        now = timeutils.isoformat_utc(timeutils.utc_now())
        async with self._write_lock:
            if filled_count is not None and exchange_order_id is not None:
                await self._c.execute(
                    """UPDATE open_limit_orders
                       SET status=?, filled_count=?, exchange_order_id=?, updated_at_utc=?
                       WHERE client_order_id=?""",
                    (status.value, filled_count, exchange_order_id, now, client_order_id),
                )
            elif filled_count is not None:
                await self._c.execute(
                    """UPDATE open_limit_orders
                       SET status=?, filled_count=?, updated_at_utc=?
                       WHERE client_order_id=?""",
                    (status.value, filled_count, now, client_order_id),
                )
            elif exchange_order_id is not None:
                await self._c.execute(
                    """UPDATE open_limit_orders
                       SET status=?, exchange_order_id=?, updated_at_utc=?
                       WHERE client_order_id=?""",
                    (status.value, exchange_order_id, now, client_order_id),
                )
            else:
                await self._c.execute(
                    """UPDATE open_limit_orders
                       SET status=?, updated_at_utc=?
                       WHERE client_order_id=?""",
                    (status.value, now, client_order_id),
                )
            await self._c.commit()

    async def get_open_orders(
        self, *, ticker: Optional[str] = None
    ) -> list[LimitOrder]:
        """Return all non-terminal orders, optionally filtered by ticker."""
        open_states = (
            OrderStatus.PENDING.value,
            OrderStatus.RESTING.value,
            OrderStatus.PARTIALLY_FILLED.value,
        )
        placeholders = ",".join("?" for _ in open_states)
        query = (
            f"SELECT * FROM open_limit_orders WHERE status IN ({placeholders})"
        )
        params: list[object] = list(open_states)
        if ticker is not None:
            query += " AND ticker = ?"
            params.append(ticker)
        async with self._c.execute(query, params) as cur:
            rows = await cur.fetchall()
        return [_row_to_order(row) for row in rows]

    async def get_order(self, client_order_id: str) -> Optional[LimitOrder]:
        async with self._c.execute(
            "SELECT * FROM open_limit_orders WHERE client_order_id = ?",
            (client_order_id,),
        ) as cur:
            row = await cur.fetchone()
        return _row_to_order(row) if row is not None else None

    # ------------------------------------------------------------------ #
    # Trades ledger + exposure
    # ------------------------------------------------------------------ #
    async def record_trade(self, trade: Trade) -> None:
        """Append an immutable fill record."""
        async with self._write_lock:
            await self._c.execute(
                """
                INSERT INTO trades
                    (client_order_id, exchange_order_id, venue, ticker, side,
                     action, price_cents, count, notional_usd, strategy,
                     model_prob, executed_at_utc)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    trade.client_order_id,
                    trade.exchange_order_id,
                    trade.venue.value,
                    trade.ticker,
                    trade.side.value,
                    trade.action.value,
                    trade.price_cents,
                    trade.count,
                    trade.notional_usd,
                    trade.strategy.value,
                    trade.model_prob,
                    timeutils.isoformat_utc(trade.executed_at_utc),
                ),
            )
            await self._c.commit()

    # ------------------------------------------------------------------ #
    # Settlement results (for paper-trading P&L)
    # ------------------------------------------------------------------ #
    async def record_market_result(
        self, ticker: str, result: str, *, settled_at_utc: Optional[datetime] = None
    ) -> None:
        """Persist the settled YES/NO outcome of a market (idempotent upsert)."""
        settled = timeutils.isoformat_utc(settled_at_utc) if settled_at_utc else None
        async with self._write_lock:
            await self._c.execute(
                """
                INSERT INTO market_results (ticker, result, settled_at_utc, recorded_at_utc)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(ticker) DO UPDATE SET
                    result = excluded.result,
                    settled_at_utc = excluded.settled_at_utc
                """,
                (ticker, result, settled, timeutils.isoformat_utc(timeutils.utc_now())),
            )
            await self._c.commit()

    async def record_model_prediction(
        self,
        *,
        ticker: str,
        model: str,
        climate_date: str,
        prob: float,
        forecast_high_f: Optional[float],
        sigma_f: Optional[float],
        history_min_interval_s: float = 1800.0,
    ) -> None:
        """Journal one forecast model's P(YES) for a market.

        Keeps ``model_predictions`` as the latest view (upsert per model, for
        Brier/report), and appends a throttled snapshot to
        ``model_prediction_history`` -- at most once per ``history_min_interval_s``
        per (ticker, model) -- so the Strategy-2 backtest sees the lead-time series.
        """
        now_iso = timeutils.isoformat_utc(timeutils.utc_now())
        async with self._write_lock:
            await self._c.execute(
                """
                INSERT INTO model_predictions
                    (ticker, model, climate_date, prob, forecast_high_f, sigma_f, created_at_utc)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(ticker, model) DO UPDATE SET
                    climate_date=excluded.climate_date, prob=excluded.prob,
                    forecast_high_f=excluded.forecast_high_f, sigma_f=excluded.sigma_f
                """,
                (ticker, model, climate_date, prob, forecast_high_f, sigma_f, now_iso),
            )
            if await self._should_snapshot(ticker, model, now_iso, history_min_interval_s):
                await self._c.execute(
                    """
                    INSERT INTO model_prediction_history
                        (ticker, model, climate_date, prob, forecast_high_f, sigma_f, created_at_utc)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (ticker, model, climate_date, prob, forecast_high_f, sigma_f, now_iso),
                )
            await self._c.commit()

    async def _should_snapshot(self, ticker: str, model: str, now_iso: str,
                               min_interval_s: float) -> bool:
        """True if enough time has passed since the last history snapshot for this
        (ticker, model) -- always True for the first snapshot or a non-positive
        interval."""
        if min_interval_s <= 0:
            return True
        async with self._c.execute(
            "SELECT MAX(created_at_utc) AS last FROM model_prediction_history "
            "WHERE ticker = ? AND model = ?",
            (ticker, model),
        ) as cur:
            row = await cur.fetchone()
        last = row["last"] if row is not None else None
        if not last:
            return True
        try:
            elapsed = (timeutils.parse_isoformat_utc(now_iso)
                       - timeutils.parse_isoformat_utc(str(last))).total_seconds()
        except ValueError:
            return True
        return elapsed >= min_interval_s

    async def record_daily_forecast(
        self, *, icao: str, model: str, climate_date: str, forecast_high_f: float
    ) -> None:
        """Journal a model's raw (uncorrected) daily-high forecast for a station/day."""
        async with self._write_lock:
            await self._c.execute(
                """
                INSERT INTO daily_forecasts (icao, model, climate_date, forecast_high_f, recorded_at_utc)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(icao, model, climate_date) DO NOTHING
                """,
                (icao, model, climate_date, forecast_high_f,
                 timeutils.isoformat_utc(timeutils.utc_now())),
            )
            await self._c.commit()

    async def recent_bias(
        self,
        icao: str,
        model: str,
        before_date: str,
        *,
        max_days: int = 21,
        min_samples: int = 5,
    ) -> Optional[float]:
        """Rolling mean forecast error (realized - forecast, °F) for a station/model.

        Uses only *settled* past days (climate_date < before_date) where both a
        forecast and a realized daily max exist. Returns ``None`` until at least
        ``min_samples`` days are available. A positive value means the model runs
        cold (add it to future forecasts); negative means it runs hot.
        """
        async with self._c.execute(
            """
            SELECT (t.tmax_f - f.forecast_high_f) AS err
            FROM daily_forecasts f
            JOIN daily_tmax t ON t.icao = f.icao AND t.climate_date = f.climate_date
            WHERE f.icao = ? AND f.model = ? AND f.climate_date < ?
            ORDER BY f.climate_date DESC
            LIMIT ?
            """,
            (icao, model, before_date, max_days),
        ) as cur:
            rows = await cur.fetchall()
        errors = [float(r["err"]) for r in rows if r["err"] is not None]
        if len(errors) < min_samples:
            return None
        return sum(errors) / len(errors)

    async def best_model_by_brier(
        self, *, min_samples: int
    ) -> Optional[tuple[str, int, float]]:
        """Lowest-Brier forecast model with >= ``min_samples`` settled predictions.

        Returns ``(model, n, brier)`` or ``None`` if no model has enough data yet.
        """
        async with self._c.execute(
            """
            SELECT mp.model AS model,
                   COUNT(*) AS n,
                   AVG( (mp.prob - (CASE WHEN mr.result='yes' THEN 1.0 ELSE 0.0 END)) *
                        (mp.prob - (CASE WHEN mr.result='yes' THEN 1.0 ELSE 0.0 END)) ) AS brier
            FROM model_predictions mp
            JOIN market_results mr ON mr.ticker = mp.ticker
            GROUP BY mp.model
            HAVING n >= ?
            ORDER BY brier ASC
            LIMIT 1
            """,
            (min_samples,),
        ) as cur:
            row = await cur.fetchone()
        if row is None:
            return None
        return str(row["model"]), int(row["n"]), float(row["brier"])

    async def record_model_pnl(self, models: dict) -> None:
        """Cache each model's Strategy-2 backtest P&L (upsert per model). ``models``
        is the backtest summary's per-model dict (trades, net_usd, hit_rate)."""
        now = timeutils.isoformat_utc(timeutils.utc_now())
        async with self._write_lock:
            for model, m in models.items():
                await self._c.execute(
                    """INSERT INTO model_pnl (model, trades, net_usd, hit_rate, updated_at_utc)
                       VALUES (?, ?, ?, ?, ?)
                       ON CONFLICT(model) DO UPDATE SET
                         trades=excluded.trades, net_usd=excluded.net_usd,
                         hit_rate=excluded.hit_rate, updated_at_utc=excluded.updated_at_utc""",
                    (model, int(m.get("trades", 0)), float(m.get("net_usd", 0.0)),
                     m.get("hit_rate"), now),
                )
            await self._c.commit()

    async def best_model_by_pnl(self, *, min_trades: int) -> Optional[tuple[str, int, float]]:
        """Highest cached net-P&L model with >= ``min_trades`` backtested trades.

        Returns ``(model, trades, net_usd)`` or ``None`` (no model qualifies yet).
        """
        async with self._c.execute(
            "SELECT model, trades, net_usd FROM model_pnl WHERE trades >= ? "
            "ORDER BY net_usd DESC LIMIT 1",
            (min_trades,),
        ) as cur:
            row = await cur.fetchone()
        if row is None:
            return None
        return str(row["model"]), int(row["trades"]), float(row["net_usd"])

    async def record_model_calibration(self, calibration: dict[str, CalibrationMap]) -> None:
        """Cache each model's fitted isotonic recalibration curve (upsert per model)."""
        now = timeutils.isoformat_utc(timeutils.utc_now())
        async with self._write_lock:
            for model, cm in calibration.items():
                await self._c.execute(
                    """INSERT INTO model_calibration (model, breakpoints, n_samples, updated_at_utc)
                       VALUES (?, ?, ?, ?)
                       ON CONFLICT(model) DO UPDATE SET
                         breakpoints=excluded.breakpoints, n_samples=excluded.n_samples,
                         updated_at_utc=excluded.updated_at_utc""",
                    (model, serialize(cm), cm.n_samples, now),
                )
            await self._c.commit()

    async def load_model_calibration(self) -> dict[str, CalibrationMap]:
        """All cached calibration maps -> ``{model: CalibrationMap}``. Rows that
        fail to deserialize (corrupt/empty) are skipped rather than raised."""
        out: dict[str, CalibrationMap] = {}
        async with self._c.execute(
            "SELECT model, breakpoints, n_samples FROM model_calibration"
        ) as cur:
            rows = await cur.fetchall()
        for row in rows:
            cm = deserialize(row["breakpoints"], int(row["n_samples"]))
            if cm is not None:
                out[str(row["model"])] = cm
        return out

    async def get_market_result(self, ticker: str) -> Optional[str]:
        async with self._c.execute(
            "SELECT result FROM market_results WHERE ticker = ?", (ticker,)
        ) as cur:
            row = await cur.fetchone()
        return str(row["result"]) if row is not None else None

    async def traded_tickers_without_result(self) -> list[str]:
        """Distinct tickers we have (paper) traded that are not yet settled."""
        async with self._c.execute(
            """
            SELECT DISTINCT t.ticker
            FROM trades t
            LEFT JOIN market_results r ON r.ticker = t.ticker
            WHERE r.ticker IS NULL
            """
        ) as cur:
            rows = await cur.fetchall()
        return [str(row["ticker"]) for row in rows]

    async def predicted_tickers_without_result(self, limit: int = 500) -> list[str]:
        """Tickers we have SHADOW-PREDICTED but never recorded a settlement for.

        ``traded_tickers_without_result`` only covers markets we actually filled,
        which in dry-run is none -- so ``market_results`` stayed empty and every
        consumer that joins it (the Brier leaderboard, ``best_model_by_brier`` and
        therefore ``WA_TRADING_MODEL=auto``, and the report's calibration panels)
        silently produced nothing. Grading shadow predictions needs outcomes for
        every market we predicted, traded or not. Oldest first so a backlog drains
        deterministically; capped per pass to bound REST calls.
        """
        async with self._c.execute(
            """
            SELECT p.ticker, MIN(p.climate_date) AS d
            FROM model_predictions p
            LEFT JOIN market_results r ON r.ticker = p.ticker
            WHERE r.ticker IS NULL
            GROUP BY p.ticker
            ORDER BY d ASC
            LIMIT ?
            """,
            (limit,),
        ) as cur:
            rows = await cur.fetchall()
        return [str(row["ticker"]) for row in rows]

    async def has_trade(self, client_order_id: str) -> bool:
        """True if a fill has already been journaled for this client order id.

        Used during crash-recovery reconciliation to avoid double-counting a
        fill that was already recorded before the restart.
        """
        async with self._c.execute(
            "SELECT 1 FROM trades WHERE client_order_id = ? LIMIT 1",
            (client_order_id,),
        ) as cur:
            row = await cur.fetchone()
        return row is not None

    async def trades_for_ticker(self, ticker: str) -> list[Trade]:
        """All journaled fills for a ticker (for settlement P&L)."""
        async with self._c.execute(
            "SELECT * FROM trades WHERE ticker = ?", (ticker,)
        ) as cur:
            rows = await cur.fetchall()
        return [
            Trade(
                client_order_id=row["client_order_id"],
                exchange_order_id=row["exchange_order_id"],
                venue=Venue(row["venue"]),
                ticker=row["ticker"],
                side=Side(row["side"]),
                action=Action(row["action"]),
                price_cents=int(row["price_cents"]),
                count=int(row["count"]),
                strategy=Strategy(row["strategy"]),
                executed_at_utc=timeutils.parse_isoformat_utc(row["executed_at_utc"]),
                model_prob=_optional_float(row["model_prob"]),
            )
            for row in rows
        ]

    async def ticker_exposure_usd(self, ticker: str) -> float:
        """Best-effort current capital committed to a ticker, in USD.

        Sums two components:

        * Realized position cost from the ``trades`` ledger (buys add, sells
          reduce net long cost), and
        * The *remaining* worst-case cost of still-working (resting/partially
          filled) limit orders.

        This is the denominator the risk manager checks new orders against.
        """
        return await self._exposure_usd("ticker = ?", ticker)

    async def group_exposure_usd(self, prefix: str) -> float:
        """Committed capital across every ticker sharing ``prefix`` -- a correlated
        group (e.g. all buckets of one station/day, which settle on one underlying).
        Feeds the risk manager's correlation-aware cap."""
        return await self._exposure_usd("ticker LIKE ?", prefix + "%")

    async def _exposure_usd(self, match_sql: str, param: str) -> float:
        # ``match_sql`` is a fixed internal fragment ("ticker = ?" / "ticker LIKE ?");
        # the value is always parameterized, so there is no injection surface.
        # Realized notional from fills.
        async with self._c.execute(
            f"SELECT action, price_cents, count FROM trades WHERE {match_sql}",
            (param,),
        ) as cur:
            trade_rows = await cur.fetchall()
        realized = 0.0
        for row in trade_rows:
            per_unit = row["price_cents"] / 100.0
            if row["action"] == Action.BUY.value:
                realized += per_unit * row["count"]
            else:
                realized -= per_unit * row["count"]

        # Committed capital in still-working orders (unfilled remainder).
        async with self._c.execute(
            f"""SELECT action, price_cents, count, filled_count
               FROM open_limit_orders
               WHERE {match_sql} AND status IN (?, ?, ?)""",
            (
                param,
                OrderStatus.PENDING.value,
                OrderStatus.RESTING.value,
                OrderStatus.PARTIALLY_FILLED.value,
            ),
        ) as cur:
            order_rows = await cur.fetchall()
        working = 0.0
        for row in order_rows:
            remaining = max(0, row["count"] - row["filled_count"])
            if row["action"] == Action.BUY.value:
                per_unit = row["price_cents"] / 100.0
            else:
                per_unit = (100 - row["price_cents"]) / 100.0
            working += per_unit * remaining

        return max(0.0, realized) + working


    # ------------------------------------------------------------------ #
    # Strategy-1 discovery experiment (observation only; never gates a trade)
    # ------------------------------------------------------------------ #
    async def record_lockin_event(self, event: dict[str, object]) -> Optional[int]:
        """Insert one immutable detection record; return its id.

        Returns the EXISTING id when this (ticker, climate_date, source) has
        already been recorded, so callers can keep attaching snapshots to the
        same event across evaluation cycles without ever mutating the detection.
        A detection is recorded before any gate runs -- that is the whole point,
        since grading only the tradeable ones is what hid the safety defect.
        """
        cols = (
            "ticker", "series", "icao", "climate_date", "source", "side", "action",
            "strike_type", "floor_strike_f", "cap_strike_f", "boundary_f",
            "certainty_bound_f", "observed_tmax_f", "lockin_margin_f", "margin_f",
            "excess_f", "observation_at_utc", "ingest_at_utc", "detected_at_utc",
            "observation_age_s",
        )
        placeholders = ", ".join("?" for _ in cols)
        async with self._write_lock:
            await self._c.execute(
                f"INSERT OR IGNORE INTO lockin_events ({', '.join(cols)}) "
                f"VALUES ({placeholders})",
                tuple(event.get(c) for c in cols),
            )
            await self._c.commit()
        async with self._c.execute(
            "SELECT id FROM lockin_events "
            "WHERE ticker = ? AND climate_date = ? AND source = ?",
            (event["ticker"], event["climate_date"], event["source"]),
        ) as cur:
            row = await cur.fetchone()
        return int(row["id"]) if row else None

    async def record_book_snapshot(self, snap: dict[str, object]) -> Optional[int]:
        """Append one order-book sample; return its id."""
        cols = (
            "ticker", "event_id", "reason", "offset_seconds", "requested_at_utc",
            "received_at_utc", "round_trip_ms", "gate_outcome",
            "best_yes_bid_cents", "best_yes_bid_size", "best_yes_ask_cents",
            "best_yes_ask_size", "best_no_bid_cents", "best_no_bid_size",
            "best_no_ask_cents", "best_no_ask_size", "spread_cents",
            "yes_depth_total", "no_depth_total", "yes_depth_to_ceiling",
            "no_depth_to_ceiling", "ceiling_cents", "n_yes_levels", "n_no_levels",
            "market_status", "ladder_json",
        )
        placeholders = ", ".join("?" for _ in cols)
        async with self._write_lock:
            cur = await self._c.execute(
                f"INSERT INTO book_snapshots ({', '.join(cols)}) "
                f"VALUES ({placeholders})",
                tuple(snap.get(c) for c in cols),
            )
            await self._c.commit()
            return int(cur.lastrowid) if cur.lastrowid else None

    async def record_sweep_metrics(self, rows: list[dict[str, object]]) -> None:
        """Bulk-insert executable-economics rows for one snapshot."""
        if not rows:
            return
        cols = (
            "snapshot_id", "side", "target_size", "fillable_size", "book_cleared",
            "top_ask_cents", "vwap_cents", "slippage_cents", "fee_cents",
            "net_if_correct_usd", "net_if_wrong_usd",
        )
        placeholders = ", ".join("?" for _ in cols)
        async with self._write_lock:
            await self._c.executemany(
                f"INSERT OR REPLACE INTO sweep_metrics ({', '.join(cols)}) "
                f"VALUES ({placeholders})",
                [tuple(r.get(c) for c in cols) for r in rows],
            )
            await self._c.commit()

    async def record_source_observation(self, obs: dict[str, object]) -> None:
        """Journal one observation from any feed (idempotent per valid time)."""
        cols = (
            "icao", "source", "climate_date", "observed_at_utc", "received_at_utc",
            "publication_age_s", "temp_f", "raw_value", "precision_f",
            "running_max_f",
        )
        placeholders = ", ".join("?" for _ in cols)
        async with self._write_lock:
            await self._c.execute(
                f"INSERT OR IGNORE INTO source_observations ({', '.join(cols)}) "
                f"VALUES ({placeholders})",
                tuple(obs.get(c) for c in cols),
            )
            await self._c.commit()

    async def get_market_settled_at(self, ticker: str) -> Optional[str]:
        """The exchange's settlement time for a market, if it published one."""
        async with self._c.execute(
            "SELECT settled_at_utc FROM market_results WHERE ticker = ?", (ticker,)
        ) as cur:
            row = await cur.fetchone()
        return str(row["settled_at_utc"]) if row and row["settled_at_utc"] else None

    async def ungraded_lockin_events(self, limit: int = 500) -> list[dict[str, object]]:
        """Detections still awaiting an official settlement, oldest first."""
        async with self._c.execute(
            "SELECT id, ticker, side FROM lockin_events "
            "WHERE call_correct IS NULL ORDER BY id LIMIT ?",
            (limit,),
        ) as cur:
            rows = await cur.fetchall()
        return [{"id": int(r["id"]), "ticker": r["ticker"], "side": r["side"]}
                for r in rows]

    async def grade_lockin_event(
        self, event_id: int, *, result: str, settled_at_utc: Optional[str],
        correct: bool,
    ) -> None:
        """Attach the official settlement to one detection.

        The only post-insert write to ``lockin_events``. Guarded on
        ``call_correct IS NULL`` so a grade can never be silently rewritten.
        """
        async with self._write_lock:
            await self._c.execute(
                "UPDATE lockin_events SET settled_result = ?, settled_at_utc = ?, "
                "call_correct = ?, graded_at_utc = ? "
                "WHERE id = ? AND call_correct IS NULL",
                (result, settled_at_utc, 1 if correct else 0,
                 timeutils.utc_now().isoformat(), event_id),
            )
            await self._c.commit()

    async def get_tmax_observed_at(
        self, icao: str, climate_date: str
    ) -> Optional[datetime]:
        """VALID time of the observation behind the current running max.

        Needed to age a detection against the weather rather than against our own
        clock. ``daily_tmax`` is updated in place, so this is the only surviving
        link from a running max back to the observation that set it -- and it
        survives only for the CURRENT max, which is why the experiment records
        it into ``lockin_events`` at detection time rather than reconstructing it.
        """
        async with self._c.execute(
            "SELECT observed_at_utc FROM daily_tmax WHERE icao = ? AND climate_date = ?",
            (icao, climate_date),
        ) as cur:
            row = await cur.fetchone()
        if row is None or row["observed_at_utc"] is None:
            return None
        try:
            return timeutils.parse_isoformat_utc(str(row["observed_at_utc"]))
        except ValueError:
            return None

    async def latest_book_snapshot_at(self, ticker: str) -> Optional[str]:
        """When this ticker's book was last sampled (ISO string), if ever."""
        async with self._c.execute(
            "SELECT received_at_utc FROM book_snapshots WHERE ticker = ? "
            "ORDER BY id DESC LIMIT 1",
            (ticker,),
        ) as cur:
            row = await cur.fetchone()
        return str(row["received_at_utc"]) if row else None


def _row_to_order(row: aiosqlite.Row) -> LimitOrder:
    """Hydrate a :class:`LimitOrder` from a DB row."""
    return LimitOrder(
        client_order_id=row["client_order_id"],
        ticker=row["ticker"],
        venue=Venue(row["venue"]),
        side=Side(row["side"]),
        action=Action(row["action"]),
        price_cents=int(row["price_cents"]),
        count=int(row["count"]),
        strategy=Strategy(row["strategy"]),
        status=OrderStatus(row["status"]),
        created_at_utc=timeutils.parse_isoformat_utc(row["created_at_utc"]),
        updated_at_utc=timeutils.parse_isoformat_utc(row["updated_at_utc"]),
        exchange_order_id=row["exchange_order_id"],
        filled_count=int(row["filled_count"]),
        model_prob=_optional_float(row["model_prob"]),
    )


def _optional_float(value: object) -> Optional[float]:
    return float(value) if value is not None else None
