"""Per-strategy LIVE clearance: going live on Strategy 1 must not arm Strategy 2.

`dry_run` is global, so `WA_DRY_RUN=false` would otherwise put BOTH strategies on
real money -- and Strategy 2 measures -$3.26/trade. The gate lives in RiskManager
because that is the single approval authority every order path already passes
through, so no caller can route around it.

The truth table these lock down:

    dry_run   allow_live_model_edge | lock_in   model_edge
    true      (either)              | allowed   allowed     <- unchanged today
    false     false                 | allowed   BLOCKED     <- the safe cutover
    false     true                  | allowed   allowed     <- deliberate opt-in
"""
import asyncio, os, sys, tempfile
import os as _osp
sys.path.insert(0, _osp.path.dirname(_osp.path.dirname(_osp.path.abspath(__file__))))

from weather_arb.config import Settings
from weather_arb.db import Database
from weather_arb.models import Action, OrderType, Strategy
from weather_arb.risk_manager import RiskManager

failures = []
def check(n, c):
    print(("PASS" if c else "FAIL"), "-", n)
    if not c: failures.append(n)


async def _decide(db, *, dry_run, allow_live, strategy, price=50, count=100):
    settings = Settings(dry_run=dry_run, allow_live_model_edge=allow_live)
    rm = RiskManager(settings=settings, db=db)
    return await rm.size_order(
        ticker="KXHIGHNY-26AUG24-B84.5", action=Action.BUY, price_cents=price,
        strategy=strategy, desired_count=count, order_type=OrderType.LIMIT,
    )


async def test_truth_table():
    with tempfile.TemporaryDirectory() as d:
        async with Database(os.path.join(d, "r.sqlite3")) as db:
            # --- dry-run: nothing changes, both strategies still journal -------
            for allow in (False, True):
                for strat in (Strategy.LOCK_IN, Strategy.MODEL_EDGE):
                    dec = await _decide(db, dry_run=True, allow_live=allow,
                                        strategy=strat)
                    check(f"dry_run + allow={allow}: {strat.value} approved",
                          dec.approved and dec.count > 0)

            # --- live, gate closed: lock-in trades, model-edge does not --------
            lock = await _decide(db, dry_run=False, allow_live=False,
                                 strategy=Strategy.LOCK_IN)
            edge = await _decide(db, dry_run=False, allow_live=False,
                                 strategy=Strategy.MODEL_EDGE)
            check("LIVE + gate closed: lock_in still approved",
                  lock.approved and lock.count > 0)
            check("LIVE + gate closed: model_edge BLOCKED", not edge.approved)
            check("rejection names the flag to flip",
                  "WA_ALLOW_LIVE_MODEL_EDGE" in (edge.reason or ""))
            check("blocked order is sized zero", edge.count == 0)

            # --- live, gate open: deliberate opt-in ----------------------------
            edge_on = await _decide(db, dry_run=False, allow_live=True,
                                    strategy=Strategy.MODEL_EDGE)
            check("LIVE + gate open: model_edge approved",
                  edge_on.approved and edge_on.count > 0)


async def test_gate_precedes_sizing():
    """A blocked strategy must be refused outright, not merely downsized."""
    with tempfile.TemporaryDirectory() as d:
        async with Database(os.path.join(d, "s.sqlite3")) as db:
            # Price/size that would comfortably pass every other check.
            dec = await _decide(db, dry_run=False, allow_live=False,
                                strategy=Strategy.MODEL_EDGE, price=5, count=1)
            check("even a 1-contract 5c model-edge order is refused live",
                  not dec.approved)
            # And the same order is fine for the cleared strategy.
            ok = await _decide(db, dry_run=False, allow_live=False,
                               strategy=Strategy.LOCK_IN, price=5, count=1)
            check("the same order as lock_in is approved", ok.approved)


async def test_defaults_are_safe():
    check("allow_live_model_edge defaults to False",
          Settings().allow_live_model_edge is False)
    prev = os.environ.pop("WA_ALLOW_LIVE_MODEL_EDGE", None)
    try:
        check("absent env var -> False", Settings.from_env().allow_live_model_edge is False)
        os.environ["WA_ALLOW_LIVE_MODEL_EDGE"] = "true"
        check("env var honoured", Settings.from_env().allow_live_model_edge is True)
    finally:
        os.environ.pop("WA_ALLOW_LIVE_MODEL_EDGE", None)
        if prev is not None:
            os.environ["WA_ALLOW_LIVE_MODEL_EDGE"] = prev


async def main():
    for fn in (test_truth_table, test_gate_precedes_sizing, test_defaults_are_safe):
        print(f"\n-- {fn.__name__}")
        await fn()

asyncio.run(main())
print("\n" + ("ALL PASS" if not failures else f"FAILURES: {failures}"))
sys.exit(1 if failures else 0)
