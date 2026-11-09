"""Kalshi trading-fee model.

Kalshi charges a trading fee on execution (there is no fee at settlement).  The
general published formula, per fill, is::

    fee = roundup_to_cent( coefficient x contracts x P x (1 - P) )

where ``P`` is the fill price in dollars (0..1) and the default coefficient is
0.07.  Fees are highest near 50c and vanish near the 1c/99c extremes -- which is
exactly why the deterministic lock-in (which trades near 98c) is barely taxed
while mid-book model-edge trades are not.

We fold these fees into paper-trading P&L so a month of dry-run results reflects
what live trading would actually net, and we tighten the Strategy-2 edge
requirement so a nominal 15% edge that fees would eat is not taken.  Some series
use a different coefficient; it is configurable via ``settings.kalshi_fee_coefficient``.
"""

from __future__ import annotations

import math


def fee_cents(price_cents: int, count: int, coefficient: float = 0.07) -> int:
    """Total trading fee (whole cents, rounded up) for a fill.

    ``price_cents`` is the per-contract price (1..99); ``count`` the number of
    contracts.
    """
    if count <= 0:
        return 0
    p = price_cents / 100.0
    raw_cents = coefficient * count * p * (1.0 - p) * 100.0
    # Round to kill floating-point noise (e.g. 175.0000000003) before ceil,
    # so an exact 1.75 dollar fee rounds to 175c, not 176c.
    return int(math.ceil(round(raw_cents, 9)))


def fee_fraction_per_contract(price_cents: int, coefficient: float = 0.07) -> float:
    """Per-contract fee as a fraction of $1 (i.e. in probability units).

    Used to make the Strategy-2 edge test fee-aware: an edge must clear the
    threshold *after* the fee it would incur at the traded price.
    """
    p = price_cents / 100.0
    return coefficient * p * (1.0 - p)
