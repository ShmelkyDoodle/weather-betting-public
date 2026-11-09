"""Passive model-edge placement: resting bid/ask pricing (offline, pure)."""
import asyncio, sys
import os as _osp; sys.path.insert(0, _osp.path.dirname(_osp.path.dirname(_osp.path.abspath(__file__))))
from weather_arb.arb_engine import passive_buy_price, passive_sell_price

failures = []
def check(n, c):
    print(("PASS" if c else "FAIL"), "-", n)
    if not c: failures.append(n)


def test_passive_buy():
    # Strong edge (limit 60, market bidding low): improve the 40 bid by 1 -> 41.
    check("strong edge improves the bid to 41", passive_buy_price(60, 40, 45, 1) == 41)
    # Never cross: capped strictly below the ask.
    check("capped below ask (no cross)", passive_buy_price(60, 44, 45, 1) == 44)
    # Thin edge: rest at the fee-adjusted edge limit and wait.
    check("thin edge rests at edge limit", passive_buy_price(33, 50, 51, 1) == 33)
    # No room to rest below a 1c ask.
    check("no passive price below a 1c ask", passive_buy_price(1, None, 1, 1) is None)
    # No book on either side -> rest at the edge limit.
    check("empty book rests at edge limit", passive_buy_price(40, None, None, 1) == 40)


def test_passive_sell():
    # Strong edge (limit 55): undercut the 65 ask by 1 -> 64, above the 60 bid.
    check("strong edge undercuts the ask to 64", passive_sell_price(55, 60, 65, 1) == 64)
    # Thin edge: rest at the edge limit (above market) and wait.
    check("thin edge rests at edge limit 67", passive_sell_price(67, 50, 51, 1) == 67)
    # Never cross: stay strictly above the bid.
    check("capped above bid (no cross)", passive_sell_price(55, 63, 64, 1) == 64)
    # No room above a 99c bid.
    check("no passive price above a 99c bid", passive_sell_price(99, 99, None, 1) is None)


async def main():
    test_passive_buy(); test_passive_sell()
    print("\n" + ("ALL PASS" if not failures else f"FAILURES: {failures}"))
    sys.exit(1 if failures else 0)

asyncio.run(main())
