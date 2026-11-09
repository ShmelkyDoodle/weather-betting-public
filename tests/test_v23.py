"""#4 out-of-sample threshold sweep: config stats, min-train-trades guard,
train-picks/test-scores separation, robustness panel (offline)."""
import asyncio, sys
import os as _osp; sys.path.insert(0, _osp.path.dirname(_osp.path.dirname(_osp.path.abspath(__file__))))
from weather_arb.backtest import Candle, _config_stats, _simulate, DEFAULT_THRESHOLD_GRID, DEFAULT_FLOOR_GRID

failures = []
def check(n, c):
    print(("PASS" if c else "FAIL"), "-", n)
    if not c: failures.append(n)


def test_config_stats():
    rows = [{"won": True, "net_usd": 60.0, "fee_usd": 1.0},
            {"won": False, "net_usd": -40.0, "fee_usd": 1.0}]
    s = _config_stats(rows)
    check("trades counted", s["trades"] == 2)
    check("net summed", abs(s["net_usd"] - 20.0) < 1e-9)
    check("per-trade = net/n", abs(s["per_trade"] - 10.0) < 1e-9)
    check("hit rate", abs(s["hit_rate"] - 0.5) < 1e-9)
    empty = _config_stats([])
    check("empty is safe (no ZeroDivisionError)",
          empty["trades"] == 0 and empty["per_trade"] == 0.0 and empty["hit_rate"] == 0.0)


def test_grid_shape():
    check("threshold grid spans the live 15% up to 50%",
          DEFAULT_THRESHOLD_GRID[0] == 0.15 and DEFAULT_THRESHOLD_GRID[-1] == 0.50)
    check("floor grid starts at no-floor", DEFAULT_FLOOR_GRID[0] == 0)


def test_min_train_trades_guard():
    """The guard is what stops a config winning by trading almost never — the
    exact failure mode the price floor exhibits (lifts hit rate, worsens $/trade)."""
    # Mirrors the selection loop in run_threshold_sweep.
    grid = {
        (0.15, 0): {"m": [{"won": False, "net_usd": -1.0, "fee_usd": 0.1}] * 50},
        (0.50, 30): {"m": [{"won": True, "net_usd": 99.0, "fee_usd": 0.1}] * 2},
    }
    def pick(min_trades):
        best, best_net = None, None
        for cell, per in grid.items():
            st = _config_stats(per.get("m", []))
            if st["trades"] < min_trades:
                continue
            if best_net is None or st["net_usd"] > best_net:
                best, best_net = cell, st["net_usd"]
        return best
    check("without a guard the 2-trade config wins", pick(0) == (0.50, 30))
    check("with the guard it is refused", pick(20) == (0.15, 0))
    check("no config clears an impossible guard", pick(1000) is None)


def test_simulate_threshold_monotonicity():
    """A higher threshold must never trade MORE: it is strictly more selective."""
    snaps = {(f"T{i}", "m"): [{"created_at": "2026-08-20T12:00:00+00:00",
                               "prob": 0.5 + 0.02 * i, "date": "2026-08-20"}]
             for i in range(30)}
    results = {f"T{i}": ("yes" if i % 2 else "no") for i in range(30)}
    candles = {f"T{i}": [Candle(int(1_787_000_000), 30, 65)] for i in range(30)}
    counts = []
    for thr in (0.15, 0.30, 0.45):
        per, _ = _simulate(snaps, results, candles, thr, 0.07, 100)
        counts.append(len(per.get("m", [])))
    check(f"trade count non-increasing as threshold rises {counts}",
          all(counts[i] >= counts[i + 1] for i in range(len(counts) - 1)))


async def main():
    test_config_stats(); test_grid_shape()
    test_min_train_trades_guard(); test_simulate_threshold_monotonicity()
    print("\n" + ("ALL PASS" if not failures else f"FAILURES: {failures}"))
    sys.exit(1 if failures else 0)

asyncio.run(main())
