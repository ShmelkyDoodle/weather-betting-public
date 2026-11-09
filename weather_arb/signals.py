"""``python -m weather_arb.signals`` -- what to bet right now, in plain English.

This is the human-facing companion to the automated engine. It runs the exact
same decision logic the live system uses (deterministic lock-in first, then
forecast edge), but instead of placing orders it prints a readable list of the
bets and how to place them by hand on Kalshi:

    - which market (ticker + title),
    - which side and the limit price (the most to pay / least to accept),
    - why, and
    - when (now for locked-in bets; no rush for forecast-edge bets).

It reads the live ledger the tracker maintains, so the temperatures (and thus
the lock-in signals) are current. Nothing is placed and nothing is written.
"""

from __future__ import annotations

import argparse
import asyncio
import sys

import aiohttp

from .arb_engine import ArbitrageEngine, Signal
from .config import Settings
from .db import Database
from .forecast_ingest import ForecastClient
from .intraday import IntradayCurves
from .market_client import KalshiClient
from .market_discovery import MarketDiscovery, MarketRegistry
from .models import Action, Side, Strategy
from .risk_manager import RiskManager
from . import timeutils


async def gather_signals(settings: Settings) -> tuple[list[Signal], int]:
    """Evaluate every currently-open market; return (signals, markets_scanned)."""
    db = Database(settings.db_path)
    await db.connect()
    client = KalshiClient(settings)
    session = aiohttp.ClientSession(headers={"User-Agent": settings.http_user_agent})
    try:
        registry = MarketRegistry(settings.markets)
        if settings.enable_discovery:
            discovery = MarketDiscovery(client=client, settings=settings, registry=registry)
            await discovery.refresh()
        # One-shot CLI: a background curve build would never land before we print,
        # so this preview waits for it -- but only when sharpening is actually on,
        # so the default path stays as fast as it is today.
        intraday = (
            IntradayCurves(session=session, settings=settings, wait_for_build=True)
            if settings.enable_intraday_forecast else None
        )
        engine = ArbitrageEngine(
            settings=settings, db=db, client=client,
            risk=RiskManager(settings=settings, db=db),
            forecast=ForecastClient(session=session, settings=settings, intraday=intraday),
            registry=registry,
        )
        markets = registry.all()
        results = await asyncio.gather(
            *(engine.evaluate_signal(m) for m in markets), return_exceptions=True
        )
        signals = [s for s in results if isinstance(s, Signal)]
        return signals, len(markets)
    finally:
        await client.close()
        await session.close()
        await db.close()


def _cents(c: int) -> str:
    return f"{c}¢"


def _render(signals: list[Signal], scanned: int) -> str:
    lock = sorted((s for s in signals if s.strategy is Strategy.LOCK_IN),
                  key=lambda s: -s.available_size)
    edge = sorted((s for s in signals if s.strategy is Strategy.MODEL_EDGE),
                  key=lambda s: -(s.model_prob or 0))
    now = timeutils.utc_now().strftime("%Y-%m-%d %H:%M UTC")

    out: list[str] = []
    out.append("=" * 68)
    out.append(f"  WHAT TO BET RIGHT NOW        {now}")
    out.append(f"  (scanned {scanned} open markets — this mirrors what the bot would do)")
    out.append("=" * 68)

    out.append("")
    out.append(f"LOCKED IN — place now ({len(lock)}):  the temperature already decided these")
    if lock:
        for s in lock:
            out.append(f"  * [{s.icao}] {s.title}")
            out.append(f"      -> {s.action_text}  at limit {_cents(s.limit_price_cents)} or better"
                       f"   (up to {s.available_size:,} contracts on offer)")
            out.append(f"      why: {s.reason}")
    else:
        out.append("  (none right now — no market's temperature has crossed its line yet today)")

    out.append("")
    out.append(f"FORECAST EDGE — no rush ({len(edge)}):  model disagrees with the market price")
    if edge:
        for s in edge:
            mp = f"{s.market_prob*100:.0f}%" if s.market_prob is not None else "?"
            fp = f"{s.model_prob*100:.0f}%" if s.model_prob is not None else "?"
            out.append(f"  * [{s.icao}] {s.title}")
            out.append(f"      -> {s.action_text}  at limit {_cents(s.limit_price_cents)}"
                       f"   (forecast {fp} vs market ~{mp})")
            out.append(f"      why: {s.reason}")
    else:
        out.append("  (none right now — market prices are within the fee-adjusted edge threshold)")

    out.append("")
    out.append("-" * 68)
    out.append(f"  No actionable bet on the other {scanned - len(signals)} markets"
               " (already fairly priced).")
    out.append("  Limit orders only. 'or better' = a lower price for a buy is fine; never pay above the limit.")
    out.append("=" * 68)
    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Show the bets the engine would place right now (nothing is placed)."
    )
    parser.add_argument("--json", action="store_true", help="emit JSON")
    args = parser.parse_args(argv)

    settings = Settings.from_env()
    signals, scanned = asyncio.run(gather_signals(settings))

    if args.json:
        import json
        print(json.dumps(
            {"scanned": scanned,
             "signals": [
                 {"ticker": s.ticker, "icao": s.icao, "strategy": s.strategy.value,
                  "action": s.action_text, "limit_price_cents": s.limit_price_cents,
                  "available_size": s.available_size, "model_prob": s.model_prob,
                  "market_prob": s.market_prob, "urgent": s.urgent,
                  "reason": s.reason, "timing": s.timing}
                 for s in signals]},
            indent=2, default=str))
    else:
        print(_render(signals, scanned))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
