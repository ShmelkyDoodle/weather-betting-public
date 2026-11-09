"""Immutable domain models and enumerations shared across the system.

Keeping these in one place gives the pollers, the market client, the risk
manager and the arbitrage engine a single, strictly-typed vocabulary.  All
prices are integer **cents** in ``[1, 99]`` (the Kalshi convention); all
temperatures used for strike comparison are **degrees Fahrenheit** (the unit US
NWS daily climate products and weather contracts settle in).
"""

from __future__ import annotations

import enum
import math
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional


class Venue(str, enum.Enum):
    """Prediction-market / betting venue.

    Only :attr:`KALSHI` is on the automated trading path today; the others are
    reference venues for the cross-venue basis monitor (:mod:`cross_venue`).
    ``FORECASTEX`` (Interactive Brokers, CFTC) and ``POLYMARKET`` (US access via
    QCEX) run comparable daily city-high markets; ``BETFAIR`` / ``SMARKETS`` are
    UK/EU exchanges surfaced only under the ``global`` venue scope.
    """

    KALSHI = "kalshi"
    POLYMARKET = "polymarket"
    FORECASTEX = "forecastex"
    BETFAIR = "betfair"
    SMARKETS = "smarkets"


class Side(str, enum.Enum):
    """Which side of a binary contract an order references."""

    YES = "yes"
    NO = "no"


class Action(str, enum.Enum):
    """Order direction relative to ``side``."""

    BUY = "buy"
    SELL = "sell"


class OrderType(str, enum.Enum):
    """Only limit orders are ever permitted (see :mod:`risk_manager`)."""

    LIMIT = "limit"


class OrderStatus(str, enum.Enum):
    """Lifecycle of a working order, mirrored between the exchange and the DB."""

    PENDING = "pending"          # created locally, not yet acknowledged
    RESTING = "resting"          # live on the book
    PARTIALLY_FILLED = "partially_filled"
    FILLED = "filled"
    CANCELED = "canceled"
    REJECTED = "rejected"

    @property
    def is_terminal(self) -> bool:
        return self in {OrderStatus.FILLED, OrderStatus.CANCELED, OrderStatus.REJECTED}

    @property
    def is_open(self) -> bool:
        return self in {OrderStatus.PENDING, OrderStatus.RESTING, OrderStatus.PARTIALLY_FILLED}


class Strategy(str, enum.Enum):
    """Origin strategy for an order (used for idempotency + attribution)."""

    LOCK_IN = "lock_in"          # Strategy 1: deterministic T_max lock-in
    MODEL_EDGE = "model_edge"    # Strategy 2: forecast vs. implied probability
    MANUAL = "manual"


@dataclass(frozen=True, slots=True)
class MetarObservation:
    """A single decoded surface observation for one station."""

    icao: str
    observed_at_utc: datetime
    temp_c: Optional[float]
    dewpoint_c: Optional[float]
    raw_metar: str

    @staticmethod
    def _c_to_f(celsius: Optional[float]) -> Optional[float]:
        if celsius is None:
            return None
        return celsius * 9.0 / 5.0 + 32.0

    @property
    def temp_f(self) -> Optional[float]:
        return self._c_to_f(self.temp_c)

    @property
    def dewpoint_f(self) -> Optional[float]:
        return self._c_to_f(self.dewpoint_c)


@dataclass(frozen=True, slots=True)
class PriceLevel:
    """One resting level in an order book: ``size`` contracts at ``price_cents``."""

    price_cents: int
    size: int


@dataclass(frozen=True, slots=True)
class OrderBook:
    """A normalized binary-market order book.

    Kalshi natively expresses the book as resting *bids* on the ``yes`` and
    ``no`` sides.  A resting NO bid at price ``p`` is economically a YES offer
    (ask) at ``100 - p``, so YES asks are derived from the NO side.  All levels
    are stored best-first.
    """

    ticker: str
    timestamp: datetime
    yes_bids: tuple[PriceLevel, ...] = ()
    no_bids: tuple[PriceLevel, ...] = ()

    @property
    def best_yes_bid(self) -> Optional[PriceLevel]:
        """Highest price someone will pay for YES (best price we can SELL YES)."""
        return max(self.yes_bids, key=lambda lvl: lvl.price_cents, default=None)

    @property
    def best_no_bid(self) -> Optional[PriceLevel]:
        return max(self.no_bids, key=lambda lvl: lvl.price_cents, default=None)

    @property
    def best_yes_ask(self) -> Optional[PriceLevel]:
        """Lowest price someone will sell YES at (best price we can BUY YES).

        Derived from the best NO bid: ``yes_ask = 100 - no_bid``.
        """
        no_bid = self.best_no_bid
        if no_bid is None:
            return None
        return PriceLevel(price_cents=100 - no_bid.price_cents, size=no_bid.size)

    @property
    def yes_asks(self) -> tuple[PriceLevel, ...]:
        """Full YES ask ladder derived from the NO bid ladder, best (lowest) first."""
        levels = [
            PriceLevel(price_cents=100 - lvl.price_cents, size=lvl.size)
            for lvl in self.no_bids
        ]
        return tuple(sorted(levels, key=lambda lvl: lvl.price_cents))

    @property
    def no_asks(self) -> tuple[PriceLevel, ...]:
        """Full NO ask ladder derived from the YES bid ladder, best (lowest) first.

        A resting YES bid at price ``p`` is a NO offer at ``100 - p``.
        """
        levels = [
            PriceLevel(price_cents=100 - lvl.price_cents, size=lvl.size)
            for lvl in self.yes_bids
        ]
        return tuple(sorted(levels, key=lambda lvl: lvl.price_cents))

    @property
    def best_no_ask(self) -> Optional[PriceLevel]:
        """Lowest price someone will sell NO at (best price we can BUY NO)."""
        yes_bid = self.best_yes_bid
        if yes_bid is None:
            return None
        return PriceLevel(price_cents=100 - yes_bid.price_cents, size=yes_bid.size)

    def get_l2_sweep_metrics(
        self, action_side: "Side", target_size: int = 100,
        max_price_cents: Optional[int] = None,
    ) -> dict[str, object]:
        """What it would really cost to sweep ``target_size`` contracts of one side.

        Exists to answer the "depth illusion": a headline ask of 14c is worthless
        if only 1 contract rests there and the next 99 sit at 99c. This walks the
        actual ask ladder and reports the volume-weighted price of the whole fill.

        Reuses :attr:`yes_asks` / :attr:`no_asks`, which already invert Kalshi's
        "both sides as bids" representation (``ask = 100 - opposite_bid``) and sort
        best-first -- deriving it again here would be a second place to drift.

        Returns
        -------
        dict with:
          ``top_levels``    -- top 3 ask levels, e.g. ``"14c x5, 18c x12, 98c x200"``
          ``vwap_cents``    -- volume-weighted average fill price (float, 2dp), or
                               ``None`` if the ladder is empty
          ``book_cleared``  -- True if the book could NOT supply ``target_size``
                               (``vwap_cents`` is then for the partial fill)
          ``fillable_size`` -- contracts actually obtainable, capped at target
          ``depth_size``    -- total size resting across the whole ladder
          ``top_ask_cents`` -- best ask, for comparison against ``vwap_cents``

        ``max_price_cents`` truncates the ladder at a price ceiling, matching what
        a marketable limit at that price would actually reach. Pass the lock-in
        ceiling (98c) to get the sweep the live engine would really execute --
        ``buyable_*_size`` already sums exactly this set of levels. Left at
        ``None`` the whole ladder is swept, which measures the book rather than
        the order.
        """
        ladder = self.yes_asks if action_side is Side.YES else self.no_asks
        if max_price_cents is not None:
            ladder = tuple(
                lvl for lvl in ladder if lvl.price_cents <= max_price_cents
            )
        top_levels = ", ".join(
            f"{lvl.price_cents}c x{lvl.size}" for lvl in ladder[:3]
        ) or "(empty)"
        depth = sum(lvl.size for lvl in ladder)

        target = max(0, int(target_size))
        filled = 0
        cost = 0
        for lvl in ladder:                      # best-first, so this is a real sweep
            if filled >= target:
                break
            take = min(lvl.size, target - filled)
            filled += take
            cost += take * lvl.price_cents

        return {
            "top_levels": top_levels,
            "vwap_cents": round(cost / filled, 2) if filled else None,
            "book_cleared": filled < target,
            "fillable_size": filled,
            "depth_size": depth,
            "top_ask_cents": ladder[0].price_cents if ladder else None,
        }

    def buyable_yes_size(self, max_price_cents: int) -> int:
        """Contracts takeable by a YES *buy* limit at ``max_price_cents``.

        Sums YES-ask size at prices ``<= max_price_cents`` (the offers such a
        marketable limit would sweep).
        """
        return sum(
            lvl.size for lvl in self.yes_asks if lvl.price_cents <= max_price_cents
        )

    def sellable_yes_size(self, min_price_cents: int) -> int:
        """Contracts takeable by a YES *sell* limit at ``min_price_cents``.

        Sums YES-bid size at prices ``>= min_price_cents``.
        """
        return sum(
            lvl.size for lvl in self.yes_bids if lvl.price_cents >= min_price_cents
        )

    def buyable_no_size(self, max_price_cents: int) -> int:
        """Contracts takeable by a NO *buy* limit at ``max_price_cents``.

        Sums NO-ask size at prices ``<= max_price_cents``.
        """
        return sum(
            lvl.size for lvl in self.no_asks if lvl.price_cents <= max_price_cents
        )


# Kalshi ``strike_type`` values for temperature markets.
STRIKE_GREATER = "greater"
STRIKE_GREATER_EQ = "greater_or_equal"
STRIKE_LESS = "less"
STRIKE_LESS_EQ = "less_or_equal"
STRIKE_BETWEEN = "between"

# Kalshi settles on a whole-degF value, and rounding is half-up, so integer k
# owns the real interval [k-0.5, k+0.5). See WeatherMarket.settlement_interval_f.
_ROUND_HALF_WIDTH = 0.5

# Safety margin (deg F) the observed running max must clear before a bucket is
# called deterministic. See WeatherMarket.deterministic_lock_in for the full
# derivation; in short, 0.5 is EXACT mathematical certainty (it is the rounding
# half-width above), not an arbitrary buffer, and anything larger is pure
# conservatism. Overridable via WA_LOCKIN_MARGIN_F (see config.RiskConfig).
#
# ⚠ Do NOT lower this below 0.5 without re-running scripts/replay_lockins.py.
# The pre-2026-08-26 rule was effectively margin 0.0 (a bare `observed > cap`)
# and it produced 18 wrong calls in 564 replayed detections -- every one of them
# in the sub-0.1 degF band this margin exists to exclude.
LOCKIN_MARGIN_F = 0.5


@dataclass(frozen=True, slots=True)
class WeatherMarket:
    """Static + live metadata for one tradeable weather contract.

    Kalshi daily high-temperature events are split into mutually-exclusive
    *buckets*.  A bucket is described by an optional lower bound (``floor_f``)
    and/or upper bound (``cap_f``):

    * tail-high (``≥ X``): ``floor`` set, ``cap`` = ``None``  -> YES iff Tmax ≥ floor
    * tail-low  (``≤ Y``): ``cap`` set, ``floor`` = ``None``  -> YES iff Tmax ≤ cap
    * interior (``X..Y``): both set                          -> YES iff floor ≤ Tmax ≤ cap

    For backward compatibility a simple ``strike_temp_f`` + ``comparison``
    (``>=``/``>``/``<=``/``<``) may be supplied instead; ``floor_f``/``cap_f``
    are derived from it.
    """

    ticker: str
    venue: Venue
    icao: str
    strike_temp_f: float = 0.0
    comparison: str = ">="
    floor_strike_f: Optional[float] = None
    cap_strike_f: Optional[float] = None
    strike_type: str = ""
    title: str = ""
    close_time_utc: Optional[datetime] = None
    status: str = "active"
    # Settlement climate day (LST) as ``YYYY-MM-DD``. When set, the engine
    # evaluates this market only against that day's running T_max / forecast --
    # never against an unrelated day's temperature.
    event_date: Optional[str] = None

    @property
    def kind(self) -> str:
        """Normalized ``strike_type``; inferred from ``comparison`` if unset."""
        if self.strike_type:
            return self.strike_type
        return {
            ">=": STRIKE_GREATER_EQ,
            ">": STRIKE_GREATER,
            "<=": STRIKE_LESS_EQ,
            "<": STRIKE_LESS,
        }.get(self.comparison, STRIKE_GREATER_EQ)

    @property
    def floor_f(self) -> Optional[float]:
        if self.floor_strike_f is not None:
            return self.floor_strike_f
        if self.kind in (STRIKE_GREATER, STRIKE_GREATER_EQ):
            return self.strike_temp_f
        return None

    @property
    def cap_f(self) -> Optional[float]:
        if self.cap_strike_f is not None:
            return self.cap_strike_f
        if self.kind in (STRIKE_LESS, STRIKE_LESS_EQ):
            return self.strike_temp_f
        return None

    def settles_yes(self, final_tmax_f: float) -> bool:
        """Whether the *final* daily max settles this bucket YES."""
        lo, hi = self.floor_f, self.cap_f
        if lo is not None and hi is not None:
            return lo <= final_tmax_f <= hi
        if lo is not None:
            return final_tmax_f > lo if self.kind == STRIKE_GREATER else final_tmax_f >= lo
        if hi is not None:
            return final_tmax_f < hi if self.kind == STRIKE_LESS else final_tmax_f <= hi
        return False

    def settlement_interval_f(self) -> tuple[float, float]:
        """Continuous ``(lo, hi)`` of *true* temperatures that settle this bucket YES.

        ``floor_f``/``cap_f`` are the **integer** strikes Kalshi lists, and
        settlement is a whole-degF value (the NWS CLI daily high), so a bucket
        covers a *set of integers*, not a continuous span.  Rounding is half-up,
        so integer ``k`` claims the real interval ``[k-0.5, k+0.5)``.  An interior
        bucket ``[F, F+1]`` therefore covers ``[F-0.5, F+1.5]`` -- **twice** the
        span of the raw strikes -- while a strict tail moves the *other* way by
        half a degree.

        Integrating a continuous forecast density over the raw strikes instead
        (what ``forecast_ingest`` did before 2026-08-23) loses ~30% of the
        probability mass: the six buckets of an event summed to 0.70, not 1.0.

        The branching mirrors :meth:`settles_yes` exactly so the pricing grid and
        the settlement rule cannot drift apart.  Returns an empty interval when
        the market has no usable strike, matching ``settles_yes`` returning False.
        """
        lo, hi = self.floor_f, self.cap_f
        if lo is not None and hi is not None:            # interior: lo <= S <= hi
            return lo - _ROUND_HALF_WIDTH, hi + _ROUND_HALF_WIDTH
        if lo is not None:                               # tail-high
            strict = self.kind == STRIKE_GREATER         # S > lo  <=>  S >= lo+1
            return (lo + _ROUND_HALF_WIDTH if strict else lo - _ROUND_HALF_WIDTH), math.inf
        if hi is not None:                               # tail-low
            strict = self.kind == STRIKE_LESS            # S < hi  <=>  S <= hi-1
            return -math.inf, (hi - _ROUND_HALF_WIDTH if strict else hi + _ROUND_HALF_WIDTH)
        return math.inf, -math.inf                       # no strike -> never YES

    def deterministic_lock_in(
        self, observed_tmax_f: float, *, margin_f: float = LOCKIN_MARGIN_F
    ) -> Optional[tuple[Side, Action]]:
        """Return the certain (side, action) given the observed running max, else None.

        The daily max is monotone non-decreasing, so an observed running max
        ``m`` is a **lower bound** on the final max. Therefore:

        * if every true max ``>= m`` settles the bucket NO, it is deterministic
          **NO** (buy NO);
        * if every true max ``>= m`` settles it YES -- only possible when the YES
          region is unbounded above, i.e. a tail-high bucket -- it is
          deterministic **YES** (buy YES).

        Interior and tail-low buckets are bounded above, so they can never be
        deterministically YES intraday (the max may still rise out of the range).

        ⚠ THE BUG THIS FIXES (2026-08-26). The rule used to be a bare
        ``observed_tmax_f > cap_f`` against the **integer strike**. But Kalshi
        settles on a whole-degF value -- the NWS CLI daily high, which is the
        continuous record ROUNDED -- while ``observed_tmax_f`` is a float derived
        from a tenths-of-degC METAR (e.g. ``84.02``). ``84.02 > 84`` is true, so
        the old rule declared the ``{83, 84}`` bucket a certain NO; but a true max
        of 84.02-84.49 rounds to a CLI of **84**, and that bucket settles **YES**.
        Replaying the old rule over 564 real detections found **18 wrong calls**,
        all but two of them in the sub-0.1 degF band, and 11 reproduced on the
        exact ``daily_tmax`` value the live engine consumes.

        The correct comparison is against the **continuous settlement interval**,
        not the raw strike. :meth:`settlement_interval_f` already returns the
        region of true temperatures that round into a YES settlement, and its
        branching mirrors :meth:`settles_yes`, so deriving the certainty bound
        from it means the lock-in rule, the pricing grid and the settlement rule
        can never drift apart. For the shapes Kalshi actually lists that bound is:

        =========================  ====================  =========================
        bucket                     YES region (true F)   certain-NO threshold
        =========================  ====================  =========================
        ``between`` [F, F+1]       [F-0.5, F+1.5]        m >= cap_f + 0.5
        ``greater`` floor F        [F+0.5, inf)          (never certain NO)
        ``less`` cap C             (-inf, C-0.5]         m >= cap_f - 0.5
        =========================  ====================  =========================

        and the certain-YES threshold for ``greater`` floor F is ``m >= F + 0.5``.

        ``margin_f`` is expressed so that the default **0.5 reproduces exactly
        that mathematical bound** (0.5 is the rounding half-width, so the shift
        below is zero); a larger value adds a pure safety buffer on top, and a
        smaller one erodes the guarantee. It is monotone: bigger margin, stricter
        rule, fewer detections, never more.

        Note the deliberate asymmetry on a strict ``less`` tail: the literal
        ``cap_f + margin_f`` would demand ``C + 0.5`` there, a full degree beyond
        certainty, and would silently discard real detections. Deriving from the
        interval gives ``C - 0.5``, which is both correct and the same rule the
        other shapes get.
        """
        lo_c, hi_c = self.settlement_interval_f()
        if lo_c > hi_c:                      # no usable strike -> never YES
            return None
        # margin_f == _ROUND_HALF_WIDTH is exact certainty, so shift by the excess.
        buffer_f = margin_f - _ROUND_HALF_WIDTH
        if math.isfinite(hi_c):              # bounded above: only NO can be certain
            if observed_tmax_f >= hi_c + buffer_f:
                return (Side.NO, Action.BUY)
            return None
        if math.isfinite(lo_c) and observed_tmax_f >= lo_c + buffer_f:
            return (Side.YES, Action.BUY)
        return None

    def resolves_yes_given_tmax(
        self, tmax_f: float, *, margin_f: float = LOCKIN_MARGIN_F
    ) -> bool:
        """Back-compat helper: is settlement already a certain YES?"""
        return self.deterministic_lock_in(
            tmax_f, margin_f=margin_f) == (Side.YES, Action.BUY)


@dataclass(frozen=True, slots=True)
class LimitOrderRequest:
    """An intent to place a single limit order; validated by the risk manager."""

    client_order_id: str
    ticker: str
    venue: Venue
    side: Side
    action: Action
    price_cents: int
    count: int
    strategy: Strategy
    order_type: OrderType = OrderType.LIMIT
    # For model-edge orders: the modeled probability that *the side we took*
    # wins (P(YES) for a YES buy, P(NO) for a YES sell). Persisted so the report
    # can measure forecast calibration. ``None`` for deterministic/manual orders.
    model_prob: Optional[float] = None

    def notional_usd(self) -> float:
        """Worst-case cash outlay/exposure for this order, in USD.

        A binary contract's per-unit cost is ``price/100`` dollars for a buy;
        for a sell the capital at risk is ``(100 - price)/100`` dollars per
        contract (the amount you can lose if it settles against you).
        """
        if self.action is Action.BUY:
            per_contract = self.price_cents / 100.0
        else:
            per_contract = (100 - self.price_cents) / 100.0
        return per_contract * self.count


@dataclass(slots=True)
class LimitOrder:
    """A working/finalized order as tracked in the ledger."""

    client_order_id: str
    ticker: str
    venue: Venue
    side: Side
    action: Action
    price_cents: int
    count: int
    strategy: Strategy
    status: OrderStatus
    created_at_utc: datetime
    updated_at_utc: datetime
    exchange_order_id: Optional[str] = None
    filled_count: int = 0
    model_prob: Optional[float] = None


@dataclass(frozen=True, slots=True)
class Trade:
    """A realized fill written to the immutable trades ledger."""

    client_order_id: Optional[str]
    exchange_order_id: Optional[str]
    venue: Venue
    ticker: str
    side: Side
    action: Action
    price_cents: int
    count: int
    strategy: Strategy
    executed_at_utc: datetime
    model_prob: Optional[float] = None

    @property
    def notional_usd(self) -> float:
        return (self.price_cents / 100.0) * self.count


@dataclass(frozen=True, slots=True)
class ProbabilityEstimate:
    """Output of the forecast model for a single strike."""

    icao: str
    strike_temp_f: float
    probability: float          # P(daily T_max >= strike), in [0, 1]
    forecast_high_f: float
    sigma_f: float
    lower_bound_f: float        # already-observed T_max used to truncate

    def implied_fair_cents(self) -> int:
        """Fair YES price in whole cents, clamped to the tradeable ``[1, 99]``."""
        return max(1, min(99, round(self.probability * 100)))


def clamp_price_cents(price_cents: int) -> int:
    """Clamp any price into the exchange-tradeable ``[1, 99]`` cent range."""
    return max(1, min(99, int(price_cents)))


def is_finite_number(value: object) -> bool:
    """True iff ``value`` is a real, finite ``int``/``float`` (not NaN/inf)."""
    return isinstance(value, (int, float)) and math.isfinite(float(value))
