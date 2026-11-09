"""Fractional-Kelly + correlated-group sizing in the risk manager (offline)."""
import asyncio, sys
import os as _osp; sys.path.insert(0, _osp.path.dirname(_osp.path.dirname(_osp.path.abspath(__file__))))
from weather_arb.config import Settings, RiskConfig
from weather_arb.models import Action, Strategy
from weather_arb.risk_manager import RiskManager

failures = []
def check(n, c):
    print(("PASS" if c else "FAIL"), "-", n)
    if not c: failures.append(n)

TICKER = "KXHIGHNY-26JUL21-B90.5"
GROUP = "KXHIGHNY-26JUL21"


class StubDB:
    def __init__(self, ticker_exp=0.0, group_exp=0.0):
        self._t, self._g = ticker_exp, group_exp
    async def ticker_exposure_usd(self, ticker):
        return self._t
    async def group_exposure_usd(self, prefix):
        return self._g


def _rm(risk, db=None):
    return RiskManager(settings=Settings(risk=risk), db=db or StubDB())


def test_kelly_count():
    # p=0.7, cost 0.5, quarter-Kelly on $10k: f*=(0.7-0.5)/0.5=0.4; stake=1000; 2000 ct.
    check("quarter-Kelly count = 2000", RiskManager.kelly_count(0.7, 0.5, 10_000, 0.25) == 2000)
    check("no edge -> 0", RiskManager.kelly_count(0.4, 0.5, 10_000, 0.25) == 0)
    check("kelly_fraction 0 -> 0", RiskManager.kelly_count(0.7, 0.5, 10_000, 0.0) == 0)
    check("degenerate cost 1.0 -> 0", RiskManager.kelly_count(0.9, 1.0, 10_000, 0.25) == 0)


async def test_kelly_downsizes_model_edge():
    base = dict(account_equity_usd=10_000, max_bankroll_fraction_model_edge=0.02)
    args = dict(ticker=TICKER, action=Action.BUY, price_cents=50,
                strategy=Strategy.MODEL_EDGE, desired_count=5000, win_prob=0.55)
    # Kelly off: only the per-market cap binds -> $200 / $0.50 = 400 contracts.
    off = await _rm(RiskConfig(kelly_fraction=0.0, **base)).size_order(**args)
    check("kelly off -> per-market cap 400", off.approved and off.count == 400)
    # Kelly on (1%): f*=0.1, stake=$10, 20 contracts -> Kelly binds below 400.
    on = await _rm(RiskConfig(kelly_fraction=0.01, **base)).size_order(**args)
    check("kelly on -> 20 contracts", on.approved and on.count == 20)
    # No win_prob -> Kelly is skipped even when enabled.
    now = await _rm(RiskConfig(kelly_fraction=0.01, **base)).size_order(
        **{**args, "win_prob": None})
    check("no win_prob -> kelly skipped (400)", now.count == 400)


async def test_group_cap():
    risk = RiskConfig(account_equity_usd=10_000, max_bankroll_fraction_model_edge=0.02,
                      max_bankroll_fraction_per_group=0.10)  # group cap = $1000
    args = dict(ticker=TICKER, action=Action.BUY, price_cents=50,
                strategy=Strategy.MODEL_EDGE, desired_count=5000, group_key=GROUP)
    # $980 already in the group -> only $20 left -> 40 contracts (below per-market 400).
    near = await _rm(risk, StubDB(group_exp=980.0)).size_order(**args)
    check("group cap binds -> 40 contracts", near.approved and near.count == 40)
    # Group fully used -> rejected.
    full = await _rm(risk, StubDB(group_exp=1000.0)).size_order(**args)
    check("group exhausted -> rejected", not full.approved and "group" in full.reason)
    # Group cap off (0) -> per-market 400 regardless of group exposure.
    offr = RiskConfig(account_equity_usd=10_000, max_bankroll_fraction_model_edge=0.02)
    off = await _rm(offr, StubDB(group_exp=980.0)).size_order(**args)
    check("group cap off -> 400", off.count == 400)


async def main():
    test_kelly_count()
    await test_kelly_downsizes_model_edge()
    await test_group_cap()
    print("\n" + ("ALL PASS" if not failures else f"FAILURES: {failures}"))
    sys.exit(1 if failures else 0)

asyncio.run(main())
