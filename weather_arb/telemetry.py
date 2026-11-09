"""Observation substrate for the Strategy-1 discovery experiment.

WHY THIS MODULE EXISTS. The system ran five weeks in dry-run and accumulated
**zero** observations of the quantity the entire thesis depends on: the price and
depth actually available at the instant a lock-in becomes provable. There is no
historical L2 endpoint at Kalshi, so that data is not merely missing, it is
unrecoverable -- the only way to get it is to start writing it down.

Nothing here influences a trading decision. It records, it never gates.

Two design rules, both learned the hard way in this project:

* **Store the raw ladder, not just derived metrics.** Every derived quantity
  stored without its inputs (``daily_tmax`` updated in place; ``prob`` without
  its truncation bound) later had to be reconstructed by inversion.
* **Record the detection before the gates, not after.** ``backtest --strategy 1``
  graded only detections that cleared the 98c ceiling -- 1 of 421 -- which is
  precisely how "421 detections, 0 wrong calls" survived while the real error
  rate was ~3%.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Any, Optional

from . import timeutils
from .fees import fee_cents
from .models import OrderBook, Side

log = logging.getLogger(__name__)

# Sizes at which executable economics are computed. -1 means "the whole
# available ladder", recorded so "100 contracts were not available" is a
# measurement rather than an inference.
SWEEP_SIZES: tuple[int, ...] = (1, 10, 25, 50, 100, 250, 500, -1)

# Why a detection did not produce an order. Recorded on every snapshot, so the
# gate history is reconstructable at every instant -- not just the first.
GATE_ACCEPTED = "accepted"
GATE_NOT_TRADEABLE = "market_not_tradeable"
GATE_ALREADY_ORDERED = "already_ordered_today"
GATE_NO_BOOK = "no_book"
GATE_NO_QUOTE = "no_quote"
GATE_PRICED_OUT = "priced_out_at_ceiling"
GATE_NO_DEPTH = "no_depth_under_ceiling"
GATE_RISK_REJECTED = "risk_rejected"
GATE_OBSERVE_ONLY = "observe_only"          # control / armed sampling, no signal


def ladder_json(book: OrderBook) -> str:
    """The raw Kalshi representation, both sides, exactly as received.

    Kalshi expresses both sides as resting BIDS; asks are derived
    (``ask = 100 - opposite_bid``). Persisting the bids means any future
    derivation stays possible and no inversion convention is baked into history.
    """
    return json.dumps({
        "yes_bids": [[lvl.price_cents, lvl.size] for lvl in book.yes_bids],
        "no_bids": [[lvl.price_cents, lvl.size] for lvl in book.no_bids],
    }, separators=(",", ":"))


def snapshot_row(
    book: OrderBook, *, ticker: str, reason: str, ceiling_cents: int,
    requested_at: datetime, received_at: datetime,
    event_id: Optional[int] = None, offset_seconds: Optional[float] = None,
    gate_outcome: Optional[str] = None, market_status: Optional[str] = None,
) -> dict[str, Any]:
    """Flatten one order book into a ``book_snapshots`` row."""
    yb, ya = book.best_yes_bid, book.best_yes_ask
    nb, na = book.best_no_bid, book.best_no_ask
    spread = (ya.price_cents - yb.price_cents) if (ya and yb) else None
    return {
        "ticker": ticker,
        "event_id": event_id,
        "reason": reason,
        "offset_seconds": offset_seconds,
        "requested_at_utc": requested_at.isoformat(),
        "received_at_utc": received_at.isoformat(),
        "round_trip_ms": (received_at - requested_at).total_seconds() * 1000.0,
        "gate_outcome": gate_outcome,
        "best_yes_bid_cents": yb.price_cents if yb else None,
        "best_yes_bid_size": yb.size if yb else None,
        "best_yes_ask_cents": ya.price_cents if ya else None,
        "best_yes_ask_size": ya.size if ya else None,
        "best_no_bid_cents": nb.price_cents if nb else None,
        "best_no_bid_size": nb.size if nb else None,
        "best_no_ask_cents": na.price_cents if na else None,
        "best_no_ask_size": na.size if na else None,
        "spread_cents": spread,
        "yes_depth_total": sum(lvl.size for lvl in book.yes_asks),
        "no_depth_total": sum(lvl.size for lvl in book.no_asks),
        "yes_depth_to_ceiling": book.buyable_yes_size(ceiling_cents),
        "no_depth_to_ceiling": book.buyable_no_size(ceiling_cents),
        "ceiling_cents": ceiling_cents,
        "n_yes_levels": len(book.yes_asks),
        "n_no_levels": len(book.no_asks),
        "market_status": market_status,
        "ladder_json": ladder_json(book),
    }


def sweep_rows(
    book: OrderBook, *, snapshot_id: int, ceiling_cents: int,
    sides: tuple[Side, ...] = (Side.YES, Side.NO),
    fee_coefficient: float = 0.07,
    sizes: tuple[int, ...] = SWEEP_SIZES,
) -> list[dict[str, Any]]:
    """Executable economics at each size, for the sides requested.

    The sweep is **ceiling-truncated**, because that is the order the engine
    would actually send: ``buyable_*_size(ceiling)`` sums every level at or below
    the ceiling and pays a marketable limit there. Reading top-of-book instead is
    exactly how the (unreproducible) 17.4c estimate was produced.

    A "correct" lock-in settles at 100c, so ``net_if_correct_usd`` is
    ``(100 - vwap) * N / 100 - fee``; a wrong one settles at 0 and loses the
    whole premium. Both are recorded because the second is the number that makes
    a 98c "riskless" trade dangerous when the rule is wrong.
    """
    rows: list[dict[str, Any]] = []
    for side in sides:
        available = (book.buyable_yes_size(ceiling_cents) if side is Side.YES
                     else book.buyable_no_size(ceiling_cents))
        for size in sizes:
            target = available if size == -1 else size
            if target <= 0:
                rows.append({
                    "snapshot_id": snapshot_id, "side": side.value,
                    "target_size": size, "fillable_size": 0, "book_cleared": 1,
                    "top_ask_cents": None, "vwap_cents": None,
                    "slippage_cents": None, "fee_cents": 0.0,
                    "net_if_correct_usd": 0.0, "net_if_wrong_usd": 0.0,
                })
                continue
            m = book.get_l2_sweep_metrics(
                side, target_size=target, max_price_cents=ceiling_cents)
            vwap = m["vwap_cents"]
            top = m["top_ask_cents"]
            filled = int(m["fillable_size"])
            if vwap is None or filled <= 0:
                fee = 0.0
                net_ok = net_bad = 0.0
                slip = None
            else:
                # fee_cents takes an integer price; the VWAP is a float, so round
                # it. The fee curve is flat enough near the extremes that this is
                # sub-cent, and it is recorded alongside the VWAP either way.
                fee = float(fee_cents(int(round(float(vwap))), filled,
                                      fee_coefficient))
                net_ok = ((100.0 - float(vwap)) * filled - fee) / 100.0
                net_bad = (-float(vwap) * filled - fee) / 100.0
                slip = float(vwap) - float(top) if top is not None else None
            rows.append({
                "snapshot_id": snapshot_id, "side": side.value,
                "target_size": size, "fillable_size": filled,
                "book_cleared": 1 if m["book_cleared"] else 0,
                "top_ask_cents": top, "vwap_cents": vwap,
                "slippage_cents": slip, "fee_cents": fee,
                "net_if_correct_usd": net_ok, "net_if_wrong_usd": net_bad,
            })
    return rows


class BookProbe:
    """Fetches (or accepts) an order book and persists it with its timing.

    ``requested_at``/``received_at`` bracket the HTTP call, giving a real
    round-trip measurement. ``OrderBook.timestamp`` is our own receipt clock and
    Kalshi's REST payload carries no server timestamp, so this is the only
    latency signal available -- it is recorded rather than assumed.
    """

    def __init__(self, *, client: Any, db: Any, settings: Any) -> None:
        self._client = client
        self._db = db
        self._settings = settings

    @property
    def _ceiling(self) -> int:
        return self._settings.risk.lock_in_price_ceiling_cents

    async def fetch(self, ticker: str) -> tuple[Optional[OrderBook], datetime, datetime]:
        """A freshly fetched book plus the instants that bracket the request.

        Deliberately bypasses the engine's cached-book path: a cached book would
        record a round-trip time that never happened and a staleness the analysis
        could not see.
        """
        requested_at = timeutils.utc_now()
        try:
            book = await self._client.get_orderbook(ticker)
        except Exception:  # noqa: BLE001 - telemetry must never break the caller
            return None, requested_at, timeutils.utc_now()
        return book, requested_at, timeutils.utc_now()

    async def capture(
        self, ticker: str, *, reason: str, book: Optional[OrderBook] = None,
        requested_at: Optional[datetime] = None,
        received_at: Optional[datetime] = None,
        event_id: Optional[int] = None, offset_seconds: Optional[float] = None,
        gate_outcome: Optional[str] = None, market_status: Optional[str] = None,
        sweep_sides: Optional[tuple[Side, ...]] = None,
    ) -> Optional[int]:
        """Persist one snapshot (+ sweeps) and return its id.

        Pass ``book`` with its own timing to record the EXACT book a decision was
        made on; omit it to fetch one. Never raises: a telemetry failure must not
        disturb the caller, so it is logged and swallowed.

        A reused book has equal requested/received stamps and therefore
        ``round_trip_ms == 0`` -- it was not a fetch we timed. The report
        excludes ``reason='signal'`` from its round-trip statistics for exactly
        that reason. Re-fetching to get a real timing would record a DIFFERENT
        book than the one the decision was priced off, which is a worse trade.
        """
        try:
            if book is None:
                book, requested_at, received_at = await self.fetch(ticker)
                if book is None:
                    return None
            if requested_at is None or received_at is None:
                requested_at = received_at = book.timestamp
            row = snapshot_row(
                book, ticker=ticker, reason=reason, ceiling_cents=self._ceiling,
                requested_at=requested_at, received_at=received_at,
                event_id=event_id, offset_seconds=offset_seconds,
                gate_outcome=gate_outcome, market_status=market_status,
            )
            snapshot_id = await self._db.record_book_snapshot(row)
            if snapshot_id is None:
                return None
            # Sweeps are stored for signal/armed samples. Control samples keep
            # the raw ladder and are swept on demand by the analysis module --
            # same numbers, ~16x fewer rows.
            if reason != "control":
                await self._db.record_sweep_metrics(sweep_rows(
                    book, snapshot_id=snapshot_id, ceiling_cents=self._ceiling,
                    sides=sweep_sides or (Side.YES, Side.NO),
                    fee_coefficient=self._settings.kalshi_fee_coefficient,
                ))
            return snapshot_id
        except Exception:  # noqa: BLE001 - see above
            log.exception(
                "book snapshot capture failed",
                extra={"context": {"ticker": ticker, "reason": reason}},
            )
            return None
