"""Order-book sampler for the Strategy-1 discovery experiment.

THE CONTROL PROBLEM THIS SOLVES. Sampling the book only when a lock-in fires
cannot distinguish four very different worlds:

  A. a real event-driven discount,
  B. ordinary microstructure that looks like a discount because you only ever
     looked during events,
  C. a thin-book artifact (a 14c ask one contract deep),
  D. adverse selection -- the market is cheap because the outcome is genuinely
     uncertain and our rule is the thing that is wrong.

Telling them apart needs the book when nothing is happening (control), the book
while a market approaches a boundary (armed), and the book at fixed offsets after
a detection (the decay curve). This samples all three.

It reads and writes; it never gates a trade and never places an order.

Rate discipline: Kalshi's client-side bucket is 8 req/s and the evaluation loop
already spends it, so this is deliberately a junior partner -- work is capped per
tick (`max_captures_per_tick`) and prioritised event follow-ups > armed > control.
A late control sample costs a row; a late follow-up costs a point on the decay
curve that cannot be re-measured.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta
from typing import Optional

from . import timeutils
from .config import Settings
from .db import Database
from .market_discovery import MarketRegistry
from .models import WeatherMarket
from .risk_manager import RiskManager
from .telemetry import GATE_OBSERVE_ONLY, BookProbe

log = logging.getLogger(__name__)


class BookRecorder:
    """Samples order books on a control / armed / event-offset schedule."""

    def __init__(
        self, *, settings: Settings, db: Database, client: object,
        registry: MarketRegistry, risk: RiskManager,
    ) -> None:
        self._settings = settings
        self._cfg = settings.experiment
        self._db = db
        self._registry = registry
        self._risk = risk
        self._probe = BookProbe(client=client, db=db, settings=settings)
        # ticker -> when we last captured anything for it
        self._last_capture: dict[str, datetime] = {}
        # pending event follow-ups: (due_at, ticker, event_id, offset_seconds)
        self._followups: list[tuple[datetime, str, int, float]] = []
        self._followups_lock = asyncio.Lock()

    # ------------------------------------------------------------------ #
    # Scheduling
    # ------------------------------------------------------------------ #
    def schedule_event_followups(self, ticker: str, event_id: int) -> None:
        """Queue the post-detection offsets for one event.

        Called from the engine the moment a detection is persisted. Synchronous
        and non-blocking on purpose: it runs inside the per-ticker lock on the
        trade path, and a detection must never wait on telemetry.

        The PRE-detection offsets (-10, -5, -2, -1 min) are not scheduled here --
        they cannot be, time only runs one way. They come from the armed stream,
        and are selected by nearest-timestamp at analysis time.
        """
        now = timeutils.utc_now()
        pending = [
            (now + timedelta(seconds=off), ticker, event_id, off)
            for off in self._cfg.offsets_seconds
        ]
        self._followups.extend(pending)

    # ------------------------------------------------------------------ #
    # Loop
    # ------------------------------------------------------------------ #
    async def run(self, stop_event: asyncio.Event) -> None:
        if not self._cfg.enabled:
            log.info("book recorder disabled")
            await stop_event.wait()          # park; never return (the supervisor
            return                            # treats a returning task as a crash)
        log.info(
            "book recorder started",
            extra={"context": {
                "control_s": self._cfg.control_interval_seconds,
                "armed_s": self._cfg.armed_interval_seconds,
                "arm_threshold_f": self._cfg.arm_threshold_f,
                "offsets_s": list(self._cfg.offsets_seconds),
                "max_per_tick": self._cfg.max_captures_per_tick,
            }},
        )
        while not stop_event.is_set():
            try:
                await self._tick()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - the recorder must never die
                log.exception("book recorder tick failed")
            try:
                await asyncio.wait_for(
                    stop_event.wait(), timeout=self._cfg.tick_seconds)
            except asyncio.TimeoutError:
                pass

    async def _tick(self) -> None:
        now = timeutils.utc_now()
        budget = self._cfg.max_captures_per_tick

        # 1. Event follow-ups first: a missed offset is a hole in the decay curve
        #    and cannot be re-measured, whereas a control sample recurs forever.
        due = [f for f in self._followups if f[0] <= now]
        if due:
            self._followups = [f for f in self._followups if f[0] > now]
            for _due_at, ticker, event_id, offset in due[:budget]:
                await self._probe.capture(
                    ticker, reason="offset", event_id=event_id,
                    offset_seconds=offset, gate_outcome=GATE_OBSERVE_ONLY,
                )
                self._last_capture[ticker] = now
                budget -= 1
            if budget <= 0:
                return

        # 2/3. Armed then control, oldest sample first so nothing starves.
        armed, control = await self._classify(now)
        for ticker, reason in (
            [(t, "armed") for t in armed] + [(t, "control") for t in control]
        ):
            if budget <= 0:
                break
            await self._probe.capture(
                ticker, reason=reason, gate_outcome=GATE_OBSERVE_ONLY)
            self._last_capture[ticker] = now
            budget -= 1

    async def _classify(self, now: datetime) -> tuple[list[str], list[str]]:
        """Split tracked markets into (armed, control), each already due."""
        armed: list[tuple[datetime, str]] = []
        control: list[tuple[datetime, str]] = []
        # One DB read per (icao, climate_date), not per market -- 84 markets share
        # 7 stations, so this is 7 reads a tick instead of 84.
        tmax_cache: dict[tuple[str, str], Optional[float]] = {}
        for market in self._registry.all():
            if not self._is_open(market, now):
                continue
            station = self._settings.station_by_icao(market.icao)
            if station is None:
                continue
            climate_date = market.event_date or self._risk.climate_date_str(
                station, now)
            key = (market.icao, climate_date)
            if key not in tmax_cache:
                tmax_cache[key] = await self._db.get_tmax(*key)
            tmax = tmax_cache[key]

            last = self._last_capture.get(market.ticker)
            is_armed = self._is_armed(market, tmax)
            interval = (self._cfg.armed_interval_seconds if is_armed
                        else self._cfg.control_interval_seconds)
            if last is not None and (now - last).total_seconds() < interval:
                continue
            (armed if is_armed else control).append(
                (last or datetime.min.replace(tzinfo=now.tzinfo), market.ticker))

        armed.sort()
        control.sort()
        return [t for _, t in armed], [t for _, t in control]

    def _is_armed(self, market: WeatherMarket, tmax_f: Optional[float]) -> bool:
        """Is this market close enough to (or past) its certainty bound?

        Armed means "the minutes around a possible detection are worth 60-second
        resolution". It stays armed once the bound is passed, so the post-event
        window keeps its cadence even after the scheduled offsets run out.
        """
        if tmax_f is None:
            return False
        lo_c, hi_c = market.settlement_interval_f()
        bound = hi_c if hi_c != float("inf") else lo_c
        if bound in (float("inf"), float("-inf")):
            return False
        return tmax_f >= bound - self._cfg.arm_threshold_f

    def _is_open(self, market: WeatherMarket, now: datetime) -> bool:
        return market.close_time_utc is None or now < market.close_time_utc
