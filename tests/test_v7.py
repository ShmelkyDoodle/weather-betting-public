"""Stage 3: best-model-by-Brier selection + auto trading-model pick."""
import asyncio, os, sys, tempfile
import os as _osp; sys.path.insert(0, _osp.path.dirname(_osp.path.dirname(_osp.path.abspath(__file__))))
from weather_arb.config import Settings, RiskConfig
from weather_arb.db import Database
from weather_arb.risk_manager import RiskManager
from weather_arb.market_discovery import MarketRegistry
from weather_arb.arb_engine import ArbitrageEngine

failures=[]
def check(n,c):
    print(("PASS" if c else "FAIL"),"-",n);
    if not c: failures.append(n)

async def seed(db, n=6):
    # 'good' predicts correctly (Brier ~0.01), 'bad' inverted (~0.81)
    for i in range(n):
        tk=f"T{i}"; res="yes" if i%2==0 else "no"
        pyes=0.9 if res=="yes" else 0.1
        await db.record_model_prediction(ticker=tk,model="good",climate_date="2026-07-10",prob=pyes,forecast_high_f=90,sigma_f=3)
        await db.record_model_prediction(ticker=tk,model="bad",climate_date="2026-07-10",prob=1-pyes,forecast_high_f=80,sigma_f=3)
        await db.record_market_result(tk,res)

def engine(settings, db):
    return ArbitrageEngine(settings=settings, db=db, client=None, risk=RiskManager(settings=settings,db=db), forecast=None, registry=MarketRegistry())

async def main():
    with tempfile.TemporaryDirectory() as d:
        async with Database(os.path.join(d,"s.sqlite3")) as db:
            # empty -> auto falls back to current
            e=engine(Settings(trading_model="auto"), db)
            check("auto -> current when no data", await e._selected_model_name()=="current")
            check("best_model None when empty", await db.best_model_by_brier(min_samples=5) is None)
            await seed(db,6)
            best=await db.best_model_by_brier(min_samples=5)
            check("best model = good", best is not None and best[0]=="good")
            check("best n=6", best[1]==6)
            check("min_samples gate", await db.best_model_by_brier(min_samples=100) is None)
            # auto now selects 'good' (min_samples=5 to match seeded data)
            e2=engine(Settings(trading_model="auto", model_select_min_samples=5), db)
            check("auto selects good", await e2._selected_model_name()=="good")
            # default min_samples (20) not yet met -> stays on current (safe)
            e2b=engine(Settings(trading_model="auto"), db)
            check("auto stays current below default min_samples", await e2b._selected_model_name()=="current")
            # pinned model overrides data
            e3=engine(Settings(trading_model="ensemble_bc"), db)
            check("pinned model honored", await e3._selected_model_name()=="ensemble_bc")
    print("\n"+("ALL PASS" if not failures else f"FAILURES: {failures}"))
    sys.exit(1 if failures else 0)
asyncio.run(main())
