"""Execution and risk control.

The risk manager is the single gate every order passes through before it can be
constructed and sent.  It enforces four invariants:

1. **Limit orders only.**  Market orders are structurally impossible (the model
   forbids them) and additionally rejected here as defense-in-depth, so the
   system can never cross a wide weather-market spread by accident.
2. **Per-market bankroll cap.**  No single ticker may accumulate more than a
   configured fraction of account equity (default 5%; a tighter 2% for the
   probabilistic model-edge strategy).  Sizing accounts for capital already
   committed to the ticker via resting orders and realized fills.
3. **Price sanity.**  Prices must be tradeable whole cents in ``[1, 99]``.
4. **Per-order clamp.**  A hard ceiling on contracts per order.

It also owns the timezone discipline for the system: all sizing/logging uses UTC
internally, and NWS climate-day resolution is delegated to :mod:`timeutils`,
which uses *Local Standard Time* (never DST) so orders align with the settlement
window the exchange uses.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from datetime import date, datetime
from typing import Optional

from . import timeutils
from .config import Settings, StationConfig
from .db import Database
from .models import Action, OrderType, Side, Strategy

log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class RiskDecision:
    """Result of a sizing/approval check."""

    approved: bool
    count: int
    reason: str
    cap_usd: float
    exposure_usd: float

    @classmethod
    def rejected(cls, reason: str, *, cap_usd: float = 0.0, exposure_usd: float = 0.0) -> "RiskDecision":
        return cls(False, 0, reason, cap_usd, exposure_usd)


class RiskManager:
    """Sizes and vets every prospective order."""

    def __init__(self, *, settings: Settings, db: Database) -> None:
        self._settings = settings
        self._risk = settings.risk
        self._db = db

    # ------------------------------------------------------------------ #
    # Hard guards
    # ------------------------------------------------------------------ #
    def assert_limit_only(self, order_type: OrderType) -> None:
        """Raise unless the order is a limit order."""
        if order_type is not OrderType.LIMIT:
            raise ValueError(
                f"risk policy violation: {order_type} order rejected (limit-only)"
            )

    @staticmethod
    def _valid_price(price_cents: int) -> bool:
        return 1 <= price_cents <= 99

    # ------------------------------------------------------------------ #
    # Sizing
    # ------------------------------------------------------------------ #
    def cap_usd_for(self, strategy: Strategy) -> float:
        """Per-market capital cap in USD for a given strategy."""
        equity = self._risk.account_equity_usd
        if strategy is Strategy.MODEL_EDGE:
            fraction = self._risk.max_bankroll_fraction_model_edge
        else:
            fraction = self._risk.max_bankroll_fraction_per_market
        return equity * fraction

    @staticmethod
    def _per_contract_cost_usd(action: Action, price_cents: int) -> float:
        """Capital committed per contract (USD).

        Buying costs ``price/100``; selling a binary risks ``(100-price)/100``
        (the amount lost if it settles against you).
        """
        if action is Action.BUY:
            return price_cents / 100.0
        return (100 - price_cents) / 100.0

    @staticmethod
    def kelly_count(win_prob: float, per_contract_usd: float,
                    bankroll_usd: float, kelly_fraction: float) -> int:
        """Fractional-Kelly contract count for a binary position.

        The position costs (and risks) ``per_contract_usd`` in [0,1] and pays $1 on
        a win with probability ``win_prob``, so the Kelly wager fraction is
        ``f* = (p - c) / (1 - c)``. We stake ``kelly_fraction * f*`` of the bankroll
        and buy ``stake / c`` contracts. Returns 0 when there is no edge (``f* <= 0``)
        or inputs are degenerate."""
        c = per_contract_usd
        if not (0.0 < c < 1.0) or bankroll_usd <= 0 or kelly_fraction <= 0:
            return 0
        f_star = (win_prob - c) / (1.0 - c)
        if f_star <= 0:
            return 0
        # Round before floor to kill float noise (e.g. 1999.9999996 -> 2000), the
        # same guard fees.py uses.
        return max(0, math.floor(round(kelly_fraction * f_star * bankroll_usd / c, 9)))

    async def size_order(
        self,
        *,
        ticker: str,
        action: Action,
        price_cents: int,
        strategy: Strategy,
        desired_count: int,
        order_type: OrderType = OrderType.LIMIT,
        win_prob: Optional[float] = None,
        group_key: Optional[str] = None,
    ) -> RiskDecision:
        """Return an approved (possibly reduced) contract count for an order.

        The count keeps the ticker's committed capital within its per-market cap,
        honoring the desired size and per-order ceiling. Two opt-in refinements
        (off by default, so the default behavior is unchanged):

        * **fractional Kelly** (``kelly_fraction`` > 0, model-edge only): downsizes
          the desired count to the Kelly wager implied by ``win_prob`` and price;
        * **correlated-group cap** (``max_bankroll_fraction_per_group`` > 0): also
          bounds total exposure across ``group_key`` (all buckets of one
          station/day settle on one underlying).

        Returns a rejection on invalid price, exhausted caps, a non-limit order, or
        a strategy that is not cleared to trade live (see below).

        **Per-strategy live clearance.** ``dry_run`` is global, so going live for
        Strategy 1 would otherwise put Strategy 2 live too. Model-edge orders are
        refused once ``dry_run`` is off unless ``allow_live_model_edge`` is
        explicitly set. Enforced here rather than in the engine because this is the
        single approval authority every order path already passes through, so no
        future caller can route around it. Dry-run behaviour is unchanged, which
        keeps paper measurement of Strategy 2 running.
        """
        try:
            self.assert_limit_only(order_type)
        except ValueError as exc:
            return RiskDecision.rejected(str(exc))

        if (strategy is Strategy.MODEL_EDGE
                and not self._settings.dry_run
                and not self._settings.allow_live_model_edge):
            return RiskDecision.rejected(
                "model-edge not cleared for live trading "
                "(set WA_ALLOW_LIVE_MODEL_EDGE=true to permit)"
            )

        if not self._valid_price(price_cents):
            return RiskDecision.rejected(f"price {price_cents} outside [1, 99]")
        if desired_count <= 0:
            return RiskDecision.rejected("non-positive desired count")

        cap = self.cap_usd_for(strategy)
        exposure = await self._db.ticker_exposure_usd(ticker)
        remaining_budget = cap - exposure
        if remaining_budget <= 0:
            return RiskDecision.rejected(
                "per-market cap exhausted", cap_usd=cap, exposure_usd=exposure
            )

        per_contract = self._per_contract_cost_usd(action, price_cents)
        if per_contract <= 0:
            return RiskDecision.rejected(
                "degenerate per-contract cost", cap_usd=cap, exposure_usd=exposure
            )

        # Fractional-Kelly downsizing (model-edge only, opt-in).
        if (self._risk.kelly_fraction > 0 and win_prob is not None
                and strategy is Strategy.MODEL_EDGE):
            kc = self.kelly_count(win_prob, per_contract,
                                  self._risk.account_equity_usd, self._risk.kelly_fraction)
            desired_count = min(desired_count, kc)
            if desired_count <= 0:
                return RiskDecision.rejected(
                    "kelly sizes zero contracts", cap_usd=cap, exposure_usd=exposure
                )

        max_affordable = math.floor(remaining_budget / per_contract)
        count = min(desired_count, max_affordable, self._risk.max_contracts_per_order)

        # Correlated-group exposure cap (opt-in).
        group_fraction = self._risk.max_bankroll_fraction_per_group
        if group_key and group_fraction > 0:
            group_cap = self._risk.account_equity_usd * group_fraction
            group_exposure = await self._db.group_exposure_usd(group_key)
            group_affordable = math.floor(max(0.0, group_cap - group_exposure) / per_contract)
            count = min(count, group_affordable)
            if count <= 0:
                return RiskDecision.rejected(
                    "correlated-group cap exhausted", cap_usd=group_cap,
                    exposure_usd=group_exposure,
                )

        if count <= 0:
            return RiskDecision.rejected(
                "budget affords zero contracts", cap_usd=cap, exposure_usd=exposure
            )

        return RiskDecision(
            approved=True,
            count=count,
            reason="approved",
            cap_usd=cap,
            exposure_usd=exposure,
        )

    # ------------------------------------------------------------------ #
    # Timezone / climate-day discipline (all internal time is UTC)
    # ------------------------------------------------------------------ #
    @staticmethod
    def climate_date_for(station: StationConfig, dt_utc: datetime) -> date:
        """NWS climate day (Local Standard Time) for an instant at a station."""
        return timeutils.climate_date_for(dt_utc, station.standard_utc_offset_hours)

    @staticmethod
    def climate_date_str(station: StationConfig, dt_utc: datetime) -> str:
        return timeutils.climate_date_str(dt_utc, station.standard_utc_offset_hours)
