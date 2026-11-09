"""Settlement grading covers SHADOW-PREDICTED markets, not just filled ones --
the gap that left market_results empty and every Brier consumer silently dead."""
import asyncio, os, sys, tempfile
import os as _osp; sys.path.insert(0, _osp.path.dirname(_osp.path.dirname(_osp.path.abspath(__file__))))
from weather_arb.db import Database
from weather_arb.config import Settings

failures = []
def check(n, c):
    print(("PASS" if c else "FAIL"), "-", n)
    if not c: failures.append(n)


async def main():
    with tempfile.TemporaryDirectory() as d:
        async with Database(os.path.join(d, "s.sqlite3")) as db:
            for i in range(3):
                await db.record_model_prediction(
                    ticker=f"T{i}", model="current", climate_date=f"2026-08-1{i}",
                    prob=0.5, forecast_high_f=80, sigma_f=3)

            # The dry-run reality: predictions exist, zero fills.
            check("no trades booked (dry-run)", await db.traded_tickers_without_result() == [])
            pend = await db.predicted_tickers_without_result()
            check("predicted-but-ungraded finds all 3", sorted(pend) == ["T0", "T1", "T2"])
            check("oldest climate_date first (backlog drains deterministically)",
                  pend[0] == "T0")

            await db.record_market_result("T0", "yes")
            pend2 = await db.predicted_tickers_without_result()
            check("graded ticker drops out", sorted(pend2) == ["T1", "T2"])
            check("limit caps a backlog pass",
                  len(await db.predicted_tickers_without_result(limit=1)) == 1)

            # The payoff: Brier now has outcomes to join against.
            check("Brier leaderboard was empty before grading",
                  await db.best_model_by_brier(min_samples=2) is None)
            await db.record_market_result("T1", "no")
            best = await db.best_model_by_brier(min_samples=2)
            check("Brier leaderboard works once results exist",
                  best is not None and best[0] == "current" and best[1] == 2)

    check("grade batch knob exists with a bounded default",
          0 < Settings().settlement_grade_batch <= 1000)
    print("\n" + ("ALL PASS" if not failures else f"FAILURES: {failures}"))
    sys.exit(1 if failures else 0)

asyncio.run(main())
