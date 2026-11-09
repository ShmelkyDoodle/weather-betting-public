"""Settlement P&L math for binary contracts.

Shared by the live settlement reconciler and the offline report so the payoff
convention is defined in exactly one place.  A binary contract settles at 100¢
(true) or 0¢ (false).  All inputs are plain strings so this can be called from
both typed models and raw SQL rows.
"""

from __future__ import annotations


def trade_pnl_cents(
    *, side: str, action: str, price_cents: int, count: int, result: str
) -> float:
    """Realized P&L (in cents) for one fill given the market's settled result.

    Parameters
    ----------
    side:    ``"yes"`` or ``"no"`` -- which contract was traded.
    action:  ``"buy"`` or ``"sell"``.
    price_cents: the fill price (1..99).
    count:   number of contracts filled.
    result:  ``"yes"`` or ``"no"`` -- the market's settlement outcome.

    A buy pays ``settle - price`` per contract; a sell pays ``price - settle``,
    where ``settle`` is 100 if the traded side won and 0 otherwise.
    """
    yes_won = result.lower() == "yes"
    if side.lower() == "yes":
        settle = 100 if yes_won else 0
    else:
        settle = 100 if not yes_won else 0

    per_contract = (settle - price_cents) if action.lower() == "buy" else (price_cents - settle)
    return float(per_contract * count)


def trade_won(*, side: str, action: str, result: str) -> bool:
    """Whether a fill was profitable at settlement (ignoring the price paid).

    Used for hit-rate stats: a *buy* of the winning side, or a *sell* of the
    losing side, counts as a directional "win".
    """
    yes_won = result.lower() == "yes"
    side_won = yes_won if side.lower() == "yes" else not yes_won
    if action.lower() == "buy":
        return side_won
    return not side_won
