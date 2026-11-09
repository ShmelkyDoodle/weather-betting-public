"""Arbitrage and pricing engine: the two trading strategies.

Strategy 1 -- Intraday T_max Lock-In (deterministic)
    Once the *validated* running daily maximum meets or exceeds a contract's
    strike, that contract will settle YES (the daily max is monotonic and can
    only rise).  If YES can still be bought below the ceiling (default 98c), we
    sweep the cheap offers with an aggressive marketable **limit** buy capped at
    the ceiling -- never crossing above it, never a market order.

Strategy 2 -- Model Convergence / Implied-Probability Discrepancy
    Using the NBM/HRRR gridpoint forecast we estimate ``P(T_max >= strike)`` with
    a lower-truncated normal (conditioned on temperature already observed).  When
    the model probability diverges from the market's implied probability by more
    than the configured EV edge (default 15 points), we post a bounded limit
    order on the favorable side at a price that preserves the edge.

Both strategies route every prospective order through the :class:`RiskManager`
for sizing and the limit-only guarantee, journal a PENDING record *before*
transmission (for crash-safe reconciliation), then finalize state from the
exchange response.  A per-ticker lock serializes the event-driven and periodic
evaluation paths so an order is never double-submitted.
"""

from __future__ import annotations

import asyncio
import logging
import math
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Optional

from . import timeutils
from .alerts import AlertManager
from .calibration import CalibrationMap
from .config import Settings, StationConfig
from .db import Database
from .fees import fee_fraction_per_contract
from .forecast_ingest import INTRADAY_SUFFIX, ForecastClient, base_model_name
from .market_client import MarketClient, MarketClientError
from .market_discovery import MarketRegistry
from .models import (
    Action,
    LimitOrder,
    LimitOrderRequest,
    OrderBook,
    OrderStatus,
    OrderType,
    PriceLevel,
    Side,
    Strategy,
    Trade,
    WeatherMarket,
    clamp_price_cents,
)
from .risk_manager import RiskManager
from .telemetry import (
    GATE_ACCEPTED,
    GATE_ALREADY_ORDERED,
    GATE_NO_BOOK,
    GATE_NO_DEPTH,
    GATE_NO_QUOTE,
    GATE_NOT_TRADEABLE,
    GATE_OBSERVE_ONLY,
    GATE_PRICED_OUT,
    BookProbe,
)

# Observation feeds a detection can come from. Only SOURCE_METAR can trade;
# SOURCE_NWS5MIN is shadow-only (see ArbitrageEngine.observe_shadow_lockin).
SOURCE_METAR = "metar"
SOURCE_NWS5MIN = "nws5min"

log = logging.getLogger(__name__)

# Market statuses in which we must not place new orders.
_UNTRADEABLE_STATUS = frozenset(
    {"closed", "settled", "finalized", "determined", "inactive", "expired"}
)


def passive_buy_price(
    edge_limit: int, best_bid: Optional[int], best_ask: Optional[int], improve: int
) -> Optional[int]:
    """Resting BUY price for a passive model-edge bid, or ``None`` if impossible.

    Improve the current best bid by ``improve`` ticks for queue priority, but never
    pay above the fee-adjusted ``edge_limit`` and never cross (stay strictly below
    the ask). Returns a price in [1, 99] or ``None`` when no passive price both
    keeps the edge and rests below the offer."""
    price = edge_limit if best_bid is None else min(edge_limit, best_bid + improve)
    if best_ask is not None:
        price = min(price, best_ask - 1)   # strictly inside the spread (no cross)
    return price if 1 <= price <= 99 else None


def passive_sell_price(
    edge_limit: int, best_bid: Optional[int], best_ask: Optional[int], improve: int
) -> Optional[int]:
    """Resting SELL price for a passive model-edge ask (sell YES / take NO), or
    ``None``. Undercut the best ask by ``improve`` ticks but never accept below the
    fee-adjusted ``edge_limit`` and never cross (stay strictly above the bid)."""
    price = edge_limit if best_ask is None else max(edge_limit, best_ask - improve)
    if best_bid is not None:
        price = max(price, best_bid + 1)   # strictly inside the spread (no cross)
    return price if 1 <= price <= 99 else None


def edge_limit_prices(
    model_prob: float, threshold: float, coeff: float,
    calibration: Optional["CalibrationMap"] = None,
) -> tuple[int, int, float, float]:
    """``(buy_limit, sell_limit, yes_prob, no_prob)`` for one model-edge decision.

    The fee-adjusted prices at which a ``model_prob`` edge still clears
    ``threshold`` *after* the trading fee it would incur: the most we may pay to
    buy YES, and the least we may accept to sell it. Pure and module-level so the
    backtest can price a simulated resting order with the **live** rule instead of
    a re-derivation that would silently drift from it.
    """
    yes_prob = calibration.apply(model_prob) if calibration else model_prob
    no_prob = calibration.apply(1.0 - model_prob) if calibration else (1.0 - model_prob)
    gross = (yes_prob - threshold) * 100.0
    buy_fee = fee_fraction_per_contract(clamp_price_cents(int(gross)), coeff) * 100.0
    buy_limit = clamp_price_cents(math.floor(gross - buy_fee))
    gross = ((1.0 - no_prob) + threshold) * 100.0
    sell_fee = fee_fraction_per_contract(clamp_price_cents(int(gross)), coeff) * 100.0
    sell_limit = clamp_price_cents(math.ceil(gross + sell_fee))
    return buy_limit, sell_limit, yes_prob, no_prob


def get_dynamic_edge_threshold(
    hour_lst: float, *, max_threshold: float, min_threshold: float,
    start_hour: float, end_hour: float,
) -> float:
    """Required model edge at ``hour_lst`` (fractional hour, Local Standard Time).

    ``max_threshold`` at/before ``start_hour``, decaying linearly to
    ``min_threshold`` at/after ``end_hour``. The shape encodes that an early-morning
    "edge" is mostly forecast noise, while late in the climate day the running max
    has largely decided the outcome so a smaller edge is more trustworthy.

    Pure and module-level for the same reason :func:`edge_limit_prices` is: the
    backtest sweep prices against the **live** rule rather than a re-derivation
    that would silently drift from it.

    A degenerate or inverted window (``end_hour <= start_hour``) collapses to
    ``max_threshold`` -- the conservative end -- rather than dividing by zero.
    """
    if end_hour <= start_hour:
        return max_threshold
    if hour_lst <= start_hour:
        return max_threshold
    if hour_lst >= end_hour:
        return min_threshold
    frac = (hour_lst - start_hour) / (end_hour - start_hour)
    return max_threshold + frac * (min_threshold - max_threshold)


def gross_edges(
    yes_prob: float, no_prob: float,
    yes_ask_cents: Optional[int], no_ask_cents: Optional[int],
) -> tuple[Optional[float], Optional[float]]:
    """``(buy_yes_edge, buy_no_edge)`` in probability points, BEFORE fees.

    The threshold rule itself lives in :func:`edge_limit_prices`, which folds the
    threshold into a fee-adjusted limit price rather than comparing an edge; these
    are the explicit edges that rule is implicitly testing, surfaced for logging
    and for the persistence gate. ``None`` where the book has no such side.

    * buy YES  -> ``yes_prob - yes_ask/100``
    * buy NO   -> ``no_prob  - no_ask/100``  (``no_ask`` == ``100 - yes_bid``)
    """
    return (
        None if yes_ask_cents is None else yes_prob - yes_ask_cents / 100.0,
        None if no_ask_cents is None else no_prob - no_ask_cents / 100.0,
    )


# Standard yardstick for the L2 depth probe. Fixed (not the order's own size) so
# every lock-in event is directly comparable when the depth study is analysed.
L2_PROBE_SIZE = 100


@dataclass(frozen=True, slots=True)
class _EdgeSighting:
    """When a qualifying edge for one ticker was first seen, and on which side."""

    first_seen_utc: datetime
    action: Action
    climate_date: str


@dataclass(frozen=True, slots=True)
class Signal:
    """A human-readable preview of the bet the engine would place on a market."""

    ticker: str
    title: str
    icao: str
    strategy: Strategy
    side: Side
    action: Action
    limit_price_cents: int      # the most you should pay / least you should accept
    available_size: int         # contracts takeable at that price right now
    reason: str                 # plain-English why
    timing: str                 # plain-English when
    urgent: bool                # True for deterministic lock-ins (act now)
    model_prob: Optional[float] = None
    market_prob: Optional[float] = None

    @property
    def action_text(self) -> str:
        return f"{self.action.value.upper()} {self.side.value.upper()}"


class ArbitrageEngine:
    """Evaluates markets and submits risk-checked limit orders."""

    def __init__(
        self,
        *,
        settings: Settings,
        db: Database,
        client: MarketClient,
        risk: RiskManager,
        forecast: ForecastClient,
        registry: MarketRegistry,
        alerts: Optional["AlertManager"] = None,
        recorder: Optional[Any] = None,
    ) -> None:
        self._settings = settings
        # Observation substrate for the discovery experiment. The probe is always
        # available (it only reads and writes); the recorder is optional so every
        # offline test that builds an engine keeps working unchanged.
        self._probe = BookProbe(client=client, db=db, settings=settings)
        self._recorder = recorder
        # Optional so every offline test that builds an engine keeps working; when
        # absent, lock-ins are logged exactly as before and nothing is pushed.
        self._alerts = alerts
        self._db = db
        self._client = client
        self._risk = risk
        self._forecast = forecast
        self._registry = registry
        self._locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
        # ticker -> (status, fetched_at)
        self._market_status: dict[str, tuple[str, datetime]] = {}
        # ticker -> (climate_date, last-logged time) for shadow predictions; re-logged
        # on the prediction-history cadence so the lead-time series builds up.
        self._pred_logged_on: dict[str, tuple[str, datetime]] = {}
        # (icao, climate_date) -> per-model bias °F, computed once/day/station
        self._bias_cache: dict[tuple[str, str], dict[str, float]] = {}
        # Auto-selected trading model (refreshed hourly).
        self._selected_model: Optional[str] = None
        self._selected_at: Optional[datetime] = None
        # Signal persistence (backlog 0b): when a qualifying edge for each ticker
        # was first seen. Only populated while enable_edge_persistence is on. Bounded:
        # a ticker belongs to exactly one climate day, so entries are evicted when
        # that day rolls over (see _edge_persisted).
        self._edge_seen: dict[str, _EdgeSighting] = {}
        self._edge_seen_day: Optional[str] = None
        # Lock-in telemetry latches: (channel, ticker) -> the side found certain,
        # so each channel is logged ONCE per market per climate day.
        # _evaluate_lock_in runs every eval cycle (~120s) and a market stays
        # certain for the rest of the day, so logging unconditionally would emit
        # ~30 lines/hour/market and bury the depth study in its own noise.
        # The two channels latch SEPARATELY on purpose: "detect" fires before any
        # gating, but "depth" needs an order book, and a market can be certain
        # for several cycles before one arrives. Sharing one latch would burn the
        # depth sample on a cycle that never actually probed.
        # Bounded the same way as _edge_seen: evicted on climate-date rollover.
        self._lockin_seen: dict[tuple[str, str], str] = {}
        self._lockin_seen_day: Optional[str] = None
        self._calibration_cache: dict[str, CalibrationMap] = {}
        self._calibration_loaded_at: Optional[datetime] = None

    # ------------------------------------------------------------------ #
    # Periodic + event-driven entry points
    # ------------------------------------------------------------------ #
    async def run(self, stop_event: asyncio.Event) -> None:
        """Periodic evaluation loop (both strategies for every market)."""
        log.info(
            "arb engine started",
            extra={"context": {"markets": self._registry.tickers(),
                               "dry_run": self._settings.dry_run}},
        )
        while not stop_event.is_set():
            try:
                await self.evaluate_all()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - evaluator must never die
                log.exception("arb evaluation cycle failed")
            try:
                await asyncio.wait_for(
                    stop_event.wait(), timeout=self._settings.arb_eval_seconds
                )
            except asyncio.TimeoutError:
                pass

    async def evaluate_all(self) -> None:
        """Evaluate every currently-discovered market concurrently."""
        tasks = [self.evaluate_market(m) for m in self._registry.all()]
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def on_new_high(self, icao: str, climate_date: str, tmax_f: float) -> None:
        """Low-latency callback fired by the METAR poller on a validated new max.

        Only the deterministic lock-in strategy is run here, for the markets on
        the station that just printed a new high.
        """
        for market in self._registry.for_station(icao):
            # Only act on markets that settle on the day this max belongs to.
            if market.event_date is not None and market.event_date != climate_date:
                continue
            await self._evaluate_lock_in(market, climate_date=climate_date, tmax_f=tmax_f)

    async def evaluate_market(self, market: WeatherMarket) -> None:
        """Run both strategies for one market for the current climate day."""
        station = self._settings.station_by_icao(market.icao)
        if station is None:
            return
        # Cheap local guard: never work a market whose trading window has closed
        # (also avoids a wasted metadata REST call per expired ticker).
        now = timeutils.utc_now()
        if market.close_time_utc is not None and now >= market.close_time_utc:
            return
        # Evaluate on the market's own settlement day (falls back to today when
        # the ticker carried no parseable date).
        today = self._risk.climate_date_str(station, now)
        climate_date = market.event_date or today
        tmax = await self._db.get_tmax(market.icao, climate_date)

        await self._log_model_predictions(market, station, climate_date, tmax)
        if tmax is not None:
            await self._evaluate_lock_in(market, climate_date=climate_date, tmax_f=tmax)
        await self._evaluate_model_edge(
            market, station=station, climate_date=climate_date, observed_tmax_f=tmax
        )

    async def _log_model_predictions(
        self,
        market: WeatherMarket,
        station: StationConfig,
        climate_date: str,
        observed_tmax_f: Optional[float],
    ) -> None:
        """Journal every forecast model's P(YES) for a market, refreshed on the
        prediction-history cadence so ``model_prediction_history`` accumulates a
        lead-time series (the Strategy-2 backtest enters at the first snapshot an
        edge appears).

        This is shadow logging only — it never affects trading, and a failure
        here must never disrupt the engine.
        """
        cadence = self._settings.prediction_history_min_interval_seconds
        now = timeutils.utc_now()
        last = self._pred_logged_on.get(market.ticker)
        if last is not None and last[0] == climate_date \
                and (now - last[1]).total_seconds() < cadence:
            return
        try:
            bias = await self._daily_bias(station, climate_date)
            estimates = await self._forecast.model_probabilities(
                station=station, market=market,
                climate_date=climate_date, observed_tmax_f=observed_tmax_f, bias=bias,
            )
        except Exception:  # noqa: BLE001 - shadow logging is best-effort
            return
        if not estimates:
            return
        for name, est in estimates.items():
            await self._db.record_model_prediction(
                ticker=market.ticker, model=name, climate_date=climate_date,
                prob=est.probability, forecast_high_f=est.forecast_high_f, sigma_f=est.sigma_f,
                history_min_interval_s=self._settings.prediction_history_min_interval_seconds,
            )
        self._pred_logged_on[market.ticker] = (climate_date, now)

    async def _daily_bias(
        self, station: StationConfig, climate_date: str
    ) -> dict[str, float]:
        """Record raw model highs and return each model's rolling bias (once/day/station)."""
        key = (station.icao, climate_date)
        cached = self._bias_cache.get(key)
        if cached is not None:
            return cached
        bias: dict[str, float] = {}
        try:
            highs = await self._forecast.raw_model_highs(station, climate_date)
            for model, high in highs.items():
                await self._db.record_daily_forecast(
                    icao=station.icao, model=model,
                    climate_date=climate_date, forecast_high_f=high,
                )
                correction = await self._db.recent_bias(station.icao, model, climate_date)
                if correction is not None:
                    bias[model] = correction
        except Exception:  # noqa: BLE001 - bias is an optimization, never fatal
            pass
        self._bias_cache[key] = bias
        return bias

    async def evaluate_signal(self, market: WeatherMarket) -> Optional[Signal]:
        """Read-only preview: what would we bet on this market right now?

        Runs the same decision logic as live trading (deterministic lock-in
        first, then forecast edge) but places nothing and writes nothing -- it
        returns a plain-English :class:`Signal`, or ``None`` if no bet is
        warranted. Powers the ``signals`` command so a person can see/place the
        bets by hand.
        """
        station = self._settings.station_by_icao(market.icao)
        if station is None:
            return None
        now = timeutils.utc_now()
        if market.close_time_utc is not None and now >= market.close_time_utc:
            return None
        climate_date = market.event_date or self._risk.climate_date_str(station, now)
        tmax = await self._db.get_tmax(market.icao, climate_date)
        book = await self._get_book(market.ticker)
        if book is None:
            return None
        ceiling = self._settings.risk.lock_in_price_ceiling_cents

        # Strategy 1 -- deterministic lock-in.
        if tmax is not None:
            det = market.deterministic_lock_in(
                tmax, margin_f=self._settings.risk.lockin_margin_f)
            if det is not None:
                side, action = det
                best = book.best_yes_ask if side is Side.YES else book.best_no_ask
                if best is not None and best.price_cents < ceiling:
                    size = (book.buyable_yes_size(ceiling) if side is Side.YES
                            else book.buyable_no_size(ceiling))
                    settles = "YES" if side is Side.YES else "NO"
                    where = "at or above" if side is Side.YES else "past"
                    reason = (f"{station.name} already reached {tmax:.0f}°F — {where} this "
                              f"contract's line, so it settles {settles} for certain.")
                    return Signal(
                        ticker=market.ticker, title=market.title or market.ticker,
                        icao=market.icao, strategy=Strategy.LOCK_IN, side=side, action=action,
                        limit_price_cents=ceiling, available_size=max(0, size),
                        reason=reason, timing="Place now — the outcome is already decided.",
                        urgent=True, market_prob=best.price_cents / 100.0,
                    )

        # Strategy 2 -- forecast vs. implied-probability edge (selected model).
        estimate = await self._trading_estimate(station, market, climate_date, tmax)
        if estimate is not None:
            req = self._model_edge_order(
                market, book, estimate.probability,
                self._settings.risk.model_edge_threshold, climate_date,
            )
            if req is not None:
                if req.action is Action.BUY and book.best_yes_ask is not None:
                    market_prob = book.best_yes_ask.price_cents / 100.0
                elif req.action is Action.SELL and book.best_yes_bid is not None:
                    market_prob = book.best_yes_bid.price_cents / 100.0
                else:
                    market_prob = None
                reason = (f"Forecast puts this around {estimate.probability * 100:.0f}%, but the "
                          f"market is pricing roughly {int(round((market_prob or 0) * 100))}% — "
                          f"a gap worth trading.")
                return Signal(
                    ticker=market.ticker, title=market.title or market.ticker,
                    icao=market.icao, strategy=Strategy.MODEL_EDGE, side=req.side, action=req.action,
                    limit_price_cents=req.price_cents, available_size=req.count,
                    reason=reason,
                    timing="No rush — leave a resting limit; the edge is the forecast, not speed.",
                    urgent=False, model_prob=estimate.probability, market_prob=market_prob,
                )
        return None

    # ------------------------------------------------------------------ #
    # Strategy 1: deterministic T_max lock-in
    # ------------------------------------------------------------------ #
    def _lockin_first_sighting(
        self, ticker: str, *, side: Side, climate_date: str, channel: str
    ) -> bool:
        """First time ``channel`` has seen ``ticker`` certain today?

        ``_evaluate_lock_in`` runs on every eval cycle (~120 s) and a market that
        has locked in stays certain for the rest of the climate day, so telemetry
        fired unconditionally would emit roughly 30 lines/hour/market -- tens of
        thousands a day across 84 tracked markets, enough to trip logrotate and
        discard the very depth history it is meant to accumulate. One line per
        market per day is also the right UNIT: the backtest counts 421
        detections as 421 events, not as 421 x every-cycle re-observations.

        ``channel`` separates the two telemetry streams ("detect" and "depth").
        They must latch independently: "detect" fires before any gating, while
        "depth" needs an order book that may not arrive for several cycles, so a
        shared latch would consume the depth sample on a cycle that never probed.

        Bounded exactly like :attr:`_edge_seen`: a ticker belongs to one climate
        day, so entries are dropped when the day rolls over.

        Re-arms if the certain side ever changes. That should be impossible (the
        observed max only rises, so certainty is monotone), but a silent
        re-detection is more useful than a silently swallowed one if it happens.
        """
        if climate_date != self._lockin_seen_day:
            self._lockin_seen = {}
            self._lockin_seen_day = climate_date
        key = (channel, ticker)
        if self._lockin_seen.get(key) == side.value:
            return False
        self._lockin_seen[key] = side.value
        return True

    async def _lock_in_decision(
        self, market: WeatherMarket, *, side: Side, ceiling: int,
        client_order_id: str,
    ) -> tuple[str, Optional[OrderBook], Optional[PriceLevel], int]:
        """Run every gate and name the one that stopped us.

        Split out of :meth:`_evaluate_lock_in` so the reason recorded in
        telemetry and the reason we actually returned are the SAME value --
        re-deriving the rejection for the log would be a second place to drift,
        which is the failure mode that produced the "0 wrong calls" claim.

        Behaviour is unchanged: the old code's single
        ``best is None or best.price_cents >= ceiling or desired <= 0`` test is
        preserved exactly, just resolved into three distinguishable outcomes.
        Reads only; nothing is placed here.
        """
        if not await self._is_tradeable(market.ticker):
            return GATE_NOT_TRADEABLE, None, None, 0
        # Idempotency: one lock-in order per market per climate day.
        existing = await self._db.get_order(client_order_id)
        if existing is not None and (
            existing.status.is_open or existing.status is OrderStatus.FILLED
        ):
            return GATE_ALREADY_ORDERED, None, None, 0

        book = await self._get_book(market.ticker)
        if book is None:
            return GATE_NO_BOOK, None, None, 0

        # Buy the certain side; trigger only while it is still buyable below the
        # ceiling (i.e. the market has not fully priced the certainty).
        if side is Side.YES:
            best = book.best_yes_ask
            desired = book.buyable_yes_size(ceiling)
        else:
            best = book.best_no_ask
            desired = book.buyable_no_size(ceiling)

        if best is None:
            return GATE_NO_QUOTE, book, None, desired
        if best.price_cents >= ceiling:
            return GATE_PRICED_OUT, book, best, desired
        if desired <= 0:
            return GATE_NO_DEPTH, book, best, desired
        return GATE_ACCEPTED, book, best, desired

    async def _persist_lockin_event(
        self, market: WeatherMarket, *, climate_date: str, side: Side,
        action: Action, tmax_f: float, source: str, outcome: str,
        book: Optional[OrderBook],
    ) -> Optional[int]:
        """Write the immutable detection row plus the book it was decided on.

        When the decision exited before fetching a book (market not tradeable,
        already ordered), one is fetched HERE purely for the record. That costs
        an extra REST call on the first sighting of a detection and nothing
        after, and it is the difference between "we have no idea what the market
        looked like" and a measurement.
        """
        try:
            now = timeutils.utc_now()
            obs = await self._db.get_tmax_observed_at(market.icao, climate_date)
            lo_c, hi_c = market.settlement_interval_f()
            bound = hi_c if hi_c != float("inf") else lo_c
            boundary = market.cap_f if market.cap_f is not None else market.floor_f
            event_id = await self._db.record_lockin_event({
                "ticker": market.ticker,
                "series": market.ticker.split("-")[0],
                "icao": market.icao,
                "climate_date": climate_date,
                "source": source,
                "side": side.value,
                "action": action.value,
                "strike_type": market.kind,
                "floor_strike_f": market.floor_f,
                "cap_strike_f": market.cap_f,
                "boundary_f": boundary,
                "certainty_bound_f": bound,
                "observed_tmax_f": tmax_f,
                "lockin_margin_f": self._settings.risk.lockin_margin_f,
                "margin_f": (tmax_f - boundary) if boundary is not None else None,
                "excess_f": tmax_f - bound,
                "observation_at_utc": obs.isoformat() if obs else None,
                "ingest_at_utc": None,
                "detected_at_utc": now.isoformat(),
                "observation_age_s": (now - obs).total_seconds() if obs else None,
            })
            if event_id is None:
                return None
            if book is not None:
                await self._probe.capture(
                    market.ticker, reason="signal", book=book,
                    requested_at=book.timestamp, received_at=book.timestamp,
                    event_id=event_id, offset_seconds=0.0, gate_outcome=outcome,
                    sweep_sides=(side,),
                )
            else:
                await self._probe.capture(
                    market.ticker, reason="signal", event_id=event_id,
                    offset_seconds=0.0, gate_outcome=outcome, sweep_sides=(side,),
                )
            if self._recorder is not None:
                self._recorder.schedule_event_followups(market.ticker, event_id)
            return event_id
        except Exception:  # noqa: BLE001 - telemetry must never break trading
            log.exception(
                "failed to persist lock-in event",
                extra={"context": {"ticker": market.ticker, "source": source}},
            )
            return None

    async def observe_shadow_lockin(
        self, *, icao: str, climate_date: str, tmax_f: float, source: str,
        observed_at_utc: Optional[datetime] = None,
    ) -> int:
        """Record detections from a SECONDARY observation feed. Never trades.

        The 5-minute NWS product is quantized to whole degC (1.8 degF) -- coarser
        than the +-0.08 degF METAR error that caused the lock-in defect -- so it
        is structurally incapable of proving certainty at 1 degF bucket
        granularity. It is wired to this method and to nothing else: it produces
        `lockin_events` rows for measurement and arms the book recorder, and it
        cannot reach :meth:`_submit` even in principle.

        Returns the number of detections recorded.
        """
        recorded = 0
        for market in self._registry.for_station(icao):
            if market.event_date is not None and market.event_date != climate_date:
                continue
            det = market.deterministic_lock_in(
                tmax_f, margin_f=self._settings.risk.lockin_margin_f)
            if det is None:
                continue
            side, action = det
            if not self._lockin_first_sighting(
                market.ticker, side=side, climate_date=climate_date,
                channel=f"shadow:{source}",
            ):
                continue
            book, requested_at, received_at = await self._probe.fetch(market.ticker)
            event_id = await self._persist_lockin_event(
                market, climate_date=climate_date, side=side, action=action,
                tmax_f=tmax_f, source=source, outcome=GATE_OBSERVE_ONLY,
                book=book,
            )
            if event_id is not None:
                recorded += 1
        return recorded

    async def _evaluate_lock_in(
        self, market: WeatherMarket, *, climate_date: str, tmax_f: float
    ) -> None:
        det = market.deterministic_lock_in(
            tmax_f, margin_f=self._settings.risk.lockin_margin_f)
        if det is None:
            return  # settlement not yet certain given the observed max
        side, action = det

        ceiling = self._settings.risk.lock_in_price_ceiling_cents
        client_order_id = f"s1-{market.ticker}-{climate_date}"

        async with self._locks[market.ticker]:
            # Live corroboration of the backtest's detection rate. Fires on the
            # TRANSITION into certainty and BEFORE any tradeability/risk gating,
            # so the live count is comparable to `backtest --strategy 1`'s
            # detections (421 in 30 days) rather than to the ~1-in-421 that
            # clears the price ceiling. Inside the lock because the eval loop and
            # on_new_high can both reach one market concurrently.
            first_sighting = self._lockin_first_sighting(
                market.ticker, side=side, climate_date=climate_date,
                channel="detect",
            )
            if first_sighting:
                log.info(
                    "lock-in detected",
                    extra={"context": {
                        "ticker": market.ticker,
                        "icao": market.icao,
                        "climate_date": climate_date,
                        "side": side.value,
                        "action": action.value,
                        "tmax_f": tmax_f,
                        "floor_f": market.floor_f,
                        "cap_f": market.cap_f,
                        "strike_type": market.kind,
                    }},
                )

            outcome, book, best, desired = await self._lock_in_decision(
                market, side=side, ceiling=ceiling,
                client_order_id=client_order_id,
            )
            priced_out = outcome != GATE_ACCEPTED

            # Persist the detection and the market state it was decided on --
            # ONCE per market per climate day, and regardless of which gate it
            # died at. This is the experiment's whole denominator: grading only
            # detections that clear the price ceiling (1 in 421) is exactly how
            # "421 detections, 0 wrong calls" went unchallenged for weeks.
            if first_sighting:
                await self._persist_lockin_event(
                    market, climate_date=climate_date, side=side, action=action,
                    tmax_f=tmax_f, source=SOURCE_METAR, outcome=outcome,
                    book=book,
                )

            # L2 depth probe, answering "was that headline ask 1 contract deep?".
            # Deliberately ABOVE the ceiling gate: 420 of 421 backtested lock-ins
            # sit at exactly 100c against a 98c ceiling, so probing only the
            # tradeable ones would accrue ~1 sample/month and could never resolve
            # depth. Computed from the SAME `book` snapshot the entry is priced
            # off, not a re-fetch: OrderBook is a frozen dataclass that the WS
            # feed REPLACES rather than mutates, so this cannot tear against an
            # in-flight delta, and we stay inside the per-ticker lock either way.
            #
            # SCOPE, and this is the limitation to keep in view: this samples at
            # t2 -- METAR publication, when the live engine first learns of
            # certainty. The ~17c discount in CLAUDE.md lives at t1 (1-minute
            # proof, a median 48 min earlier) and is structurally NOT observable
            # here without a real-time sub-hourly feed. See HANDOFF.md s3.
            # Always computed (a cheap walk over a few levels) so the ntfy push
            # is populated even on a later cycle; logged only on first sight.
            l2 = (book.get_l2_sweep_metrics(side, target_size=L2_PROBE_SIZE)
                  if book is not None else None)
            if l2 is not None and self._lockin_first_sighting(
                market.ticker, side=side, climate_date=climate_date,
                channel="depth",
            ):
                # Permanent historical record outside SQLite -- the depth study
                # reads this back from logs/tracker.out.log.
                log.info(
                    "lock-in L2 depth",
                    extra={"context": {
                        "ticker": market.ticker, "side": side.value,
                        "top_levels": l2["top_levels"],
                        "vwap_cents": l2["vwap_cents"],
                        "probe_size": L2_PROBE_SIZE,
                        "book_cleared": l2["book_cleared"],
                        "fillable_size": l2["fillable_size"],
                        "depth_size": l2["depth_size"],
                        "top_ask_cents": l2["top_ask_cents"],
                        "sweep_size": desired,
                        "ceiling_cents": ceiling,
                        "priced_out": priced_out,
                        "gate_outcome": outcome,
                    }},
                )

            if priced_out:
                return

            request = LimitOrderRequest(
                client_order_id=client_order_id,
                ticker=market.ticker,
                venue=self._client.venue,
                side=side,
                action=action,
                price_cents=ceiling,          # aggressive marketable limit, capped
                count=desired,
                strategy=Strategy.LOCK_IN,
            )
            log.info(
                "lock-in signal",
                extra={"context": {"ticker": market.ticker, "tmax_f": tmax_f,
                                   "side": side.value,
                                   "floor_f": market.floor_f, "cap_f": market.cap_f,
                                   "best_ask": best.price_cents,
                                   "sweep_size": desired}},
            )
            placed = await self._submit(request)
            # Push only once the order has actually cleared the risk gate --
            # _submit returns None when risk rejects it, and an alert for an order
            # that was never journaled would be a lie.
            if placed is not None:
                await self._push_lock_in(
                    market, tmax_f=tmax_f, side=side,
                    ask_cents=best.price_cents, order=placed, l2=l2)

    async def _selected_model_name(self) -> str:
        """The forecast model Strategy 2 currently trades on.

        ``settings.trading_model`` pins a name, or ``"auto"`` picks the lowest-Brier
        model with enough settled samples, or ``"auto_pnl"`` picks the highest
        backtested net-P&L model (from the ``model_pnl`` cache written by
        ``backtest --strategy 2 --persist``) and falls back to Brier then
        ``current``. Refreshed hourly; ``current`` until a leaderboard is meaningful.

        Intraday-derived models (``<model>_id`` and standalone ``intraday``) are
        logged and ranked like any other model, but an auto-pick only *trades* one
        when ``WA_ENABLE_INTRADAY_FORECAST`` is on -- otherwise the flag would not
        actually control whether intraday reaches the order path. With it off, a
        winning intraday pick falls back to its unsharpened base model.
        """
        configured = self._settings.trading_model
        if configured not in ("auto", "auto_pnl"):
            return configured
        now = timeutils.utc_now()
        if (self._selected_model is not None and self._selected_at is not None
                and (now - self._selected_at).total_seconds() < 3600):
            return self._selected_model

        min_samples = self._settings.model_select_min_samples
        metric: str = "brier"
        best: Optional[tuple[str, int, float]] = None
        if configured == "auto_pnl":
            best = await self._db.best_model_by_pnl(min_trades=min_samples)
            if best is not None:
                metric = "net_usd"
        if best is None:                                   # fall back to Brier
            best = await self._db.best_model_by_brier(min_samples=min_samples)
        chosen = best[0] if best is not None else "current"
        if not self._settings.enable_intraday_forecast:
            chosen = base_model_name(chosen)
        if chosen != self._selected_model:
            log.info(
                "trading model selected",
                extra={"context": {"model": chosen,
                                   "by": metric if best is not None else "default",
                                   "value": best[2] if best else None,
                                   "samples": best[1] if best else None}},
            )
        self._selected_model, self._selected_at = chosen, now
        return chosen

    async def _calibration_for(self, model: str) -> Optional[CalibrationMap]:
        """The cached isotonic recalibration curve for ``model``, or ``None`` when
        disabled or no fit exists yet. Refreshed hourly (same cadence as model
        selection); a load failure degrades to uncalibrated rather than raising."""
        if not self._settings.enable_probability_calibration:
            return None
        now = timeutils.utc_now()
        if (self._calibration_loaded_at is None
                or (now - self._calibration_loaded_at).total_seconds() >= 3600):
            try:
                self._calibration_cache = await self._db.load_model_calibration()
            except Exception:  # noqa: BLE001 - calibration must never break trading
                log.exception("calibration load failed; trading uncalibrated")
                self._calibration_cache = {}
            self._calibration_loaded_at = now
        return self._calibration_cache.get(model)

    async def _trading_estimate(
        self, station: StationConfig, market: WeatherMarket,
        climate_date: str, observed_tmax_f: Optional[float],
    ) -> Optional[ProbabilityEstimate]:
        """P(YES) from the currently-selected trading model (fast path: current).

        With ``WA_ENABLE_INTRADAY_FORECAST`` on, the selected model's
        intraday-sharpened ``_id`` variant is traded instead; if no intraday curve
        is available yet (still building, no observation, not today) the variant is
        simply absent and we fall back to the unsharpened model.
        """
        model = await self._selected_model_name()
        sharpen = self._settings.enable_intraday_forecast
        if model == "current":
            return await self._forecast.market_probability(
                station=station, market=market,
                climate_date=climate_date, observed_tmax_f=observed_tmax_f,
                apply_intraday=sharpen,
            )
        wanted = model
        if sharpen and base_model_name(model) == model:
            wanted = f"{model}{INTRADAY_SUFFIX}"
        bias = await self._daily_bias(station, climate_date)
        estimates = await self._forecast.model_probabilities(
            station=station, market=market, climate_date=climate_date,
            observed_tmax_f=observed_tmax_f, bias=bias,
        )
        return (estimates.get(wanted) or estimates.get(model)
                or estimates.get(base_model_name(model)) or estimates.get("current"))

    # ------------------------------------------------------------------ #
    # Strategy 2: model vs. implied-probability edge
    # ------------------------------------------------------------------ #
    def _edge_threshold_for(self, station: StationConfig) -> float:
        """The model-edge threshold to require right now for ``station``.

        Flat ``risk.model_edge_threshold`` unless ``enable_dynamic_edge`` is on, in
        which case it decays with the station's own LOCAL STANDARD TIME hour --
        the seven cities span UTC-5..-8, so a single global hour would apply the
        morning threshold to LA while NY is already mid-afternoon.
        """
        risk = self._settings.risk
        if not self._settings.enable_dynamic_edge:
            return risk.model_edge_threshold
        return get_dynamic_edge_threshold(
            timeutils.local_hour_lst(
                timeutils.utc_now(), station.standard_utc_offset_hours),
            max_threshold=risk.dynamic_edge_max_threshold,
            min_threshold=risk.dynamic_edge_min_threshold,
            start_hour=risk.dynamic_edge_start_hour,
            end_hour=risk.dynamic_edge_end_hour,
        )

    def _edge_persisted(
        self, ticker: str, action: Optional[Action], climate_date: str
    ) -> bool:
        """Has a qualifying edge on ``action`` survived ``edge_persist_seconds``?

        ``action=None`` means the edge has gone; the ticker's timer is cleared so a
        later re-appearance starts over. A side flip also restarts it -- half a
        window of "buy YES" followed by half of "sell YES" is not a persistent
        signal, it is a model that cannot make up its mind.

        Gates BOTH entry actions, not just ``Action.BUY``: in this strategy a SELL
        is buying NO, i.e. equally a new position. Gating only one side would let
        NO entries in instantly while YES waited 15 minutes.

        Always ``True`` when the feature is off, so the default path is unchanged.
        Gated by its OWN flag (``enable_edge_persistence``), independent of the
        time-decay threshold: measured 2026-08-23, the decay is harmful while this
        is mildly useful, so they must be switchable separately.
        """
        if not self._settings.enable_edge_persistence:
            return True
        # A ticker lives on exactly one climate day, so anything from an earlier
        # day is dead weight -- drop it when the day rolls over.
        if climate_date != self._edge_seen_day:
            self._edge_seen = {
                t: e for t, e in self._edge_seen.items() if e.climate_date >= climate_date
            }
            self._edge_seen_day = climate_date
        if action is None:
            self._edge_seen.pop(ticker, None)
            return False
        now = timeutils.utc_now()
        seen = self._edge_seen.get(ticker)
        if seen is None or seen.action is not action or seen.climate_date != climate_date:
            self._edge_seen[ticker] = _EdgeSighting(now, action, climate_date)
            return False
        held = (now - seen.first_seen_utc).total_seconds()
        return held >= self._settings.risk.edge_persist_seconds

    async def _evaluate_model_edge(
        self,
        market: WeatherMarket,
        *,
        station: StationConfig,
        climate_date: str,
        observed_tmax_f: Optional[float],
    ) -> None:
        # If settlement is already certain, Strategy 1 owns this market.
        if observed_tmax_f is not None and market.deterministic_lock_in(
                observed_tmax_f,
                margin_f=self._settings.risk.lockin_margin_f) is not None:
            return

        estimate = await self._trading_estimate(
            station, market, climate_date, observed_tmax_f
        )
        if estimate is None:
            return
        model_prob = estimate.probability
        threshold = self._edge_threshold_for(station)
        calibration = await self._calibration_for(await self._selected_model_name())

        async with self._locks[market.ticker]:
            if not await self._is_tradeable(market.ticker):
                return
            book = await self._get_book(market.ticker)
            if book is None:
                return

            request = self._model_edge_order(
                market, book, model_prob, threshold, climate_date,
                calibration=calibration,
                min_price_cents=self._settings.model_edge_min_price_cents,
            )
            # The explicit edge behind the decision. `edge_limit_prices` folds the
            # threshold into a fee-adjusted limit price rather than comparing an
            # edge, so `ask <= buy_limit` IS `gross_edge >= threshold + fee` (up to
            # integer-cent rounding). Naming it makes the persistence gate and the
            # log speak in the same units as the threshold.
            yes_ask_lvl, no_ask_lvl = book.best_yes_ask, book.best_no_ask
            _bl, _sl, yes_p, no_p = edge_limit_prices(
                model_prob, threshold, self._settings.kalshi_fee_coefficient, calibration)
            buy_edge, sell_edge = gross_edges(
                yes_p, no_p,
                yes_ask_lvl.price_cents if yes_ask_lvl is not None else None,
                no_ask_lvl.price_cents if no_ask_lvl is not None else None,
            )
            gross_edge = (
                None if request is None
                else (buy_edge if request.action is Action.BUY else sell_edge)
            )

            # Signal persistence: an edge that has only just appeared is not yet
            # evidence. Clearing on `None` restarts the timer, so a flickering
            # edge never accumulates. No-op unless enable_edge_persistence is on.
            if not self._edge_persisted(
                market.ticker, request.action if request else None, climate_date
            ):
                if request is not None:
                    seen = self._edge_seen.get(market.ticker)
                    log.debug(
                        "model-edge held pending persistence",
                        extra={"context": {
                            "ticker": market.ticker, "action": request.action.value,
                            "threshold": round(threshold, 4),
                            "gross_edge": (
                                None if gross_edge is None else round(gross_edge, 4)),
                            "held_s": round(
                                (timeutils.utc_now() - seen.first_seen_utc).total_seconds()
                                if seen else 0.0),
                            "need_s": self._settings.risk.edge_persist_seconds}},
                    )
                return
            if request is None:
                return

            # Replace any stale model-edge order at a different price.
            await self._reconcile_model_edge_orders(market.ticker, keep=request.client_order_id)

            existing = await self._db.get_order(request.client_order_id)
            if existing is not None and existing.status.is_open:
                return  # identical order already working -> idempotent

            log.info(
                "model-edge signal",
                extra={"context": {"ticker": market.ticker,
                                   "model_prob": round(model_prob, 4),
                                   "forecast_high_f": round(estimate.forecast_high_f, 2),
                                   "sigma_f": round(estimate.sigma_f, 2),
                                   "action": request.action.value,
                                   "threshold": round(threshold, 4),
                                   "gross_edge": (
                                       None if gross_edge is None else round(gross_edge, 4)),
                                   "price": request.price_cents,
                                   "count": request.count}},
            )
            await self._submit(request)

    def _model_edge_order(
        self,
        market: WeatherMarket,
        book: OrderBook,
        model_prob: float,
        threshold: float,
        climate_date: str,
        calibration: Optional[CalibrationMap] = None,
        min_price_cents: int = 0,
    ) -> Optional[LimitOrderRequest]:
        """Build a limit order capturing a *fee-aware* model edge, or ``None``.

        The price we are willing to trade at is tightened by the trading fee it
        would incur, so a nominal edge that fees would erase is not taken. Note the
        test below is stated as a price comparison but is exactly an edge test:
        ``ask <= buy_limit`` iff ``gross_edge >= threshold + fee`` (see
        :func:`gross_edges`), up to integer-cent rounding. The
        order records the (possibly calibrated) probability that the side we take
        wins -- used for later calibration analysis and, via ``win_prob`` in
        :meth:`_submit`, for fractional-Kelly sizing.

        With ``calibration`` set, each side's raw probability is recalibrated
        *independently* before the edge check (YES on ``model_prob``, NO on
        ``1 - model_prob``) -- see :mod:`calibration` for why: the raw models are
        well-calibrated in aggregate but overconfident specifically on the cheap
        longshot buckets this threshold rule is drawn to. ``min_price_cents`` is
        cheap defense-in-depth against the same failure mode, applied to the
        actual execution price on either leg (cost of NO is ``100 - price`` when
        selling YES).
        """
        best_ask = book.best_yes_ask
        best_bid = book.best_yes_bid
        ask_c = best_ask.price_cents if best_ask is not None else None
        bid_c = best_bid.price_cents if best_bid is not None else None
        coeff = self._settings.kalshi_fee_coefficient
        passive = self._settings.enable_passive_model_edge
        improve = self._settings.passive_improve_ticks
        cap = self._settings.risk.max_contracts_per_order

        # Fee-adjusted prices that still clear the threshold after fees, from the
        # shared rule the passive backtest also prices against.
        buy_limit, sell_limit, yes_prob, no_prob = edge_limit_prices(
            model_prob, threshold, coeff, calibration)

        # BUY YES edge (our side wins iff YES).
        if buy_limit >= 1:
            if ask_c is not None and ask_c <= buy_limit and ask_c >= min_price_cents:  # takeable now
                size = book.buyable_yes_size(buy_limit)
                if size > 0:
                    return self._edge_request(market, climate_date, Action.BUY,
                                              buy_limit, size, yes_prob)
            elif passive:                                            # rest a bid
                price = passive_buy_price(buy_limit, bid_c, ask_c, improve)
                if price is not None and price >= min_price_cents:
                    return self._edge_request(market, climate_date, Action.BUY,
                                              price, cap, yes_prob)

        # SELL YES edge (our side wins iff NO; economically buying NO at 100-price).
        if sell_limit <= 99:
            if bid_c is not None and bid_c >= sell_limit and (100 - bid_c) >= min_price_cents:
                size = book.sellable_yes_size(sell_limit)          # takeable now
                if size > 0:
                    return self._edge_request(market, climate_date, Action.SELL,
                                              sell_limit, size, no_prob)
            elif passive:                                            # rest an ask
                price = passive_sell_price(sell_limit, bid_c, ask_c, improve)
                if price is not None and (100 - price) >= min_price_cents:
                    return self._edge_request(market, climate_date, Action.SELL,
                                              price, cap, no_prob)
        return None

    def _edge_request(self, market: WeatherMarket, climate_date: str, action: Action,
                      price: int, count: int, model_prob: float) -> LimitOrderRequest:
        verb = "buy" if action is Action.BUY else "sell"
        return LimitOrderRequest(
            client_order_id=f"s2-{market.ticker}-{climate_date}-{verb}-{price}",
            ticker=market.ticker,
            venue=self._client.venue,
            side=Side.YES,
            action=action,
            price_cents=price,
            count=count,
            strategy=Strategy.MODEL_EDGE,
            model_prob=model_prob,
        )

    async def _reconcile_model_edge_orders(self, ticker: str, *, keep: str) -> None:
        """Cancel working model-edge orders on ``ticker`` except ``keep``."""
        for order in await self._db.get_open_orders(ticker=ticker):
            if order.strategy is not Strategy.MODEL_EDGE:
                continue
            if order.client_order_id == keep:
                continue
            if order.exchange_order_id:
                await self._client.cancel_order(order.exchange_order_id)
            await self._db.set_order_status(order.client_order_id, OrderStatus.CANCELED)

    # ------------------------------------------------------------------ #
    # Shared submission pipeline
    # ------------------------------------------------------------------ #
    async def _submit(self, request: LimitOrderRequest) -> Optional[LimitOrder]:
        """Risk-check, journal, transmit and finalize a single limit order."""
        decision = await self._risk.size_order(
            ticker=request.ticker,
            action=request.action,
            price_cents=request.price_cents,
            strategy=request.strategy,
            desired_count=request.count,
            order_type=OrderType.LIMIT,
            win_prob=request.model_prob,
            # Correlated group = all buckets of one station/day (ticker minus its
            # bucket suffix, e.g. KXHIGHNY-26JUL21-B90.5 -> KXHIGHNY-26JUL21).
            group_key=request.ticker.rsplit("-", 1)[0],
        )
        if not decision.approved:
            log.info(
                "order rejected by risk manager",
                extra={"context": {"ticker": request.ticker, "reason": decision.reason,
                                   "cap_usd": round(decision.cap_usd, 2),
                                   "exposure_usd": round(decision.exposure_usd, 2)}},
            )
            return None

        sized = LimitOrderRequest(
            client_order_id=request.client_order_id,
            ticker=request.ticker,
            venue=request.venue,
            side=request.side,
            action=request.action,
            price_cents=request.price_cents,
            count=decision.count,
            strategy=request.strategy,
            model_prob=request.model_prob,
        )

        now = timeutils.utc_now()
        pending = LimitOrder(
            client_order_id=sized.client_order_id,
            ticker=sized.ticker,
            venue=sized.venue,
            side=sized.side,
            action=sized.action,
            price_cents=sized.price_cents,
            count=sized.count,
            strategy=sized.strategy,
            status=OrderStatus.PENDING,
            created_at_utc=now,
            updated_at_utc=now,
            model_prob=sized.model_prob,
        )
        # Journal intent BEFORE transmitting so a crash mid-flight is recoverable.
        await self._db.upsert_open_order(pending)

        try:
            placed = await self._client.place_limit_order(sized)
        except MarketClientError as exc:
            await self._db.set_order_status(sized.client_order_id, OrderStatus.REJECTED)
            log.warning(
                "order placement failed",
                extra={"context": {"ticker": sized.ticker, "error": str(exc)}},
            )
            return None

        # The client-built order doesn't carry our model probability; restore it
        # so the fill record captures it for calibration.
        placed.model_prob = sized.model_prob
        await self._db.set_order_status(
            placed.client_order_id,
            placed.status,
            filled_count=placed.filled_count,
            exchange_order_id=placed.exchange_order_id,
        )
        if placed.filled_count > 0:
            await self._record_fill(placed)
        return placed

    async def _push_lock_in(
        self, market: WeatherMarket, *, tmax_f: float, side: Side,
        ask_cents: int, order: LimitOrder,
        l2: Optional[dict[str, object]] = None,
    ) -> None:
        """High-priority push that Strategy 1 just fired (best-effort, never raises).

        ``AlertManager.send`` is a *blocking* 10 s-timeout HTTP POST, so it runs in
        a worker thread: a lock-in is a race against the market repricing the
        certainty away, and stalling the event loop here would cost precisely the
        edge the strategy exists to capture.
        """
        if self._alerts is None:
            return
        # Which boundary the observed max actually breached. Certain-NO fires when
        # T_max climbs past the bucket's cap; certain-YES when it reaches the floor
        # of a tail-high bucket. Reporting the other one would be meaningless.
        if side is Side.NO:
            strike = market.cap_f
            breach = f"max {tmax_f:.1f}F > cap {strike:.0f}F" if strike is not None else "cap"
        else:
            strike = market.floor_f
            breach = f"max {tmax_f:.1f}F >= floor {strike:.0f}F" if strike is not None else "floor"
        mode = "DRY-RUN" if self._settings.dry_run else "LIVE"
        title = f"[{mode}] Lock-in {side.value.upper()} {market.ticker}"
        message = "\n".join([
            f"{breach}  ->  settlement certain",
            f"buy {side.value.upper()} x{order.count} crossing ask {ask_cents}c "
            f"(limit {order.price_cents}c)",
            f"observed_tmax_f {tmax_f:.2f}   bucket "
            f"[{market.floor_f}, {market.cap_f}]",
            f"order {order.client_order_id}  status {order.status.value}",
        ])
        if l2 is not None:
            vwap = l2.get("vwap_cents")
            # The whole point: compare the headline ask against what 100 contracts
            # would actually average. A big gap is the depth illusion, visible on
            # the phone at the moment it matters.
            message += (
                f"\nL2 Depth: {l2['top_levels']} | "
                f"VWAP({L2_PROBE_SIZE}): {'n/a' if vwap is None else f'{vwap}'}\u00a2"
            )
            if l2.get("book_cleared"):
                message += (f"  [BOOK CLEARED - only {l2['fillable_size']} "
                            f"of {L2_PROBE_SIZE} available]")
        try:
            await asyncio.to_thread(
                self._alerts.send, title, message, priority="high", tags="moneybag")
        except Exception:  # noqa: BLE001 - alerting must never break the trade path
            log.exception(
                "lock-in alert failed",
                extra={"context": {"ticker": market.ticker}},
            )

    async def _record_fill(self, order: LimitOrder) -> None:
        trade = Trade(
            client_order_id=order.client_order_id,
            exchange_order_id=order.exchange_order_id,
            venue=order.venue,
            ticker=order.ticker,
            side=order.side,
            action=order.action,
            price_cents=order.price_cents,
            count=order.filled_count,
            strategy=order.strategy,
            executed_at_utc=timeutils.utc_now(),
            model_prob=order.model_prob,
        )
        await self._db.record_trade(trade)
        log.info(
            "fill recorded",
            extra={"context": {"ticker": order.ticker, "count": order.filled_count,
                               "price": order.price_cents, "action": order.action.value}},
        )

    # ------------------------------------------------------------------ #
    # Market data helpers
    # ------------------------------------------------------------------ #
    async def _get_book(self, ticker: str) -> Optional[OrderBook]:
        """Prefer the fresh WebSocket book; fall back to a REST snapshot."""
        cached = self._client.get_cached_orderbook(ticker)
        if cached is not None:
            age = (timeutils.utc_now() - cached.timestamp).total_seconds()
            if age <= self._settings.orderbook_refresh_seconds * 2:
                return cached
        try:
            return await self._client.get_orderbook(ticker)
        except MarketClientError:
            log.warning("orderbook fetch failed", extra={"context": {"ticker": ticker}})
            return cached  # possibly stale, possibly None

    async def _is_tradeable(self, ticker: str) -> bool:
        cached = self._market_status.get(ticker)
        ttl = 60.0
        if cached is not None:
            status, fetched = cached
            if (timeutils.utc_now() - fetched).total_seconds() < ttl:
                return status not in _UNTRADEABLE_STATUS
        try:
            info = await self._client.get_market(ticker)
        except MarketClientError:
            return True  # fail-open on metadata; exchange still rejects bad orders
        self._market_status[ticker] = (info.status.lower(), timeutils.utc_now())
        return info.status.lower() not in _UNTRADEABLE_STATUS
