"""Isotonic probability recalibration: PAVA, binning, CalibrationMap, (de)serialize."""
import asyncio, sys
from datetime import datetime, timezone
import os as _osp; sys.path.insert(0, _osp.path.dirname(_osp.path.dirname(_osp.path.abspath(__file__))))
from weather_arb.calibration import (
    _pava, bin_stats, fit_calibration, CalibrationMap, serialize, deserialize,
)
from weather_arb.backtest import Candle, _edge_trade, first_qualifying_edge

failures = []
def check(n, c):
    print(("PASS" if c else "FAIL"), "-", n)
    if not c: failures.append(n)


def test_pava_pools_violations():
    # Strictly decreasing -> everything pools into one block at the weighted mean.
    out = _pava([0.8, 0.5, 0.2], [1.0, 1.0, 1.0])
    check("decreasing input pooled to constant mean 0.5", out == [0.5, 0.5, 0.5])
    # Already non-decreasing -> passed through unchanged.
    out2 = _pava([0.1, 0.3, 0.6], [1.0, 1.0, 1.0])
    check("non-decreasing input unchanged", out2 == [0.1, 0.3, 0.6])
    # A single local violation gets pooled with its neighbor, rest untouched.
    out3 = _pava([0.1, 0.5, 0.3, 0.9], [1.0, 1.0, 1.0, 1.0])
    check("local violation pooled, output non-decreasing", all(out3[i] <= out3[i+1] for i in range(3)))
    check("local violation pooled correctly (0.5,0.3 -> 0.4)", out3 == [0.1, 0.4, 0.4, 0.9])


def test_bin_stats():
    pairs = [(0.05, 1.0)] * 20 + [(0.85, 1.0)] * 20 + [(0.5, 0.0)] * 5  # last bin too sparse
    stats = bin_stats(pairs, n_bins=20, min_bin_samples=15)
    check("two qualifying bins (sparse bin dropped)", len(stats) == 2)
    xs = [s[0] for s in stats]
    check("bins sorted by probability", xs == sorted(xs))


def test_fit_calibration_corrects_overconfidence():
    # Model claims ~25-35% for a bucket that in reality wins only ~2% of the time
    # (the diagnosed failure mode) -- calibration should pull it far down.
    pairs = [(0.30, 0.0)] * 100 + [(0.30, 1.0)] * 2
    # ...and near-certain claims that are genuinely reliable stay high.
    pairs += [(0.90, 1.0)] * 90 + [(0.90, 0.0)] * 10
    cm = fit_calibration(pairs, n_bins=20, min_bin_samples=15)
    check("calibration fit produced", cm is not None)
    calibrated_low = cm.apply(0.30)
    calibrated_high = cm.apply(0.90)
    check("overconfident 30% claim corrected far below raw", calibrated_low < 0.10)
    check("genuinely reliable 90% claim stays high", calibrated_high > 0.80)
    check("calibration is monotonic", calibrated_low <= calibrated_high)


def test_fit_calibration_insufficient_data():
    check("too few samples -> None", fit_calibration([(0.5, 1.0)] * 5, min_bin_samples=15) is None)


def test_calibration_map_apply():
    cm = CalibrationMap(breakpoints=((0.2, 0.05), (0.5, 0.30), (0.8, 0.75)), n_samples=100)
    check("exact breakpoint", cm.apply(0.5) == 0.30)
    check("interpolates between breakpoints", abs(cm.apply(0.35) - 0.175) < 1e-9)
    check("clamps below range", cm.apply(0.0) == 0.05)
    check("clamps above range", cm.apply(1.0) == 0.75)
    empty = CalibrationMap(breakpoints=(), n_samples=0)
    check("no breakpoints -> identity pass-through", empty.apply(0.42) == 0.42)


def test_serialize_roundtrip():
    cm = CalibrationMap(breakpoints=((0.1, 0.02), (0.5, 0.31), (0.9, 0.88)), n_samples=250)
    text = serialize(cm)
    restored = deserialize(text, n_samples=cm.n_samples)
    check("round-trip preserves breakpoints", restored is not None and
          all(abs(a[0]-b[0]) < 1e-6 and abs(a[1]-b[1]) < 1e-6
              for a, b in zip(cm.breakpoints, restored.breakpoints)))
    check("empty string -> None", deserialize("", 0) is None)
    check("garbage -> None", deserialize("not,valid;data", 0) is None)


def test_edge_trade_applies_calibration():
    # Raw prob 0.30 looks like a qualifying YES edge at 10c (0.30-0.10=0.20 >= 0.15+fee).
    check("raw 0.30 qualifies without calibration",
          _edge_trade(0.30, 10, None, 0.15, 0.07) is not None)
    # A calibration map that has learned "raw 0.30 -> really ~0.05" suppresses it.
    cm = CalibrationMap(breakpoints=((0.0, 0.0), (0.30, 0.05), (1.0, 1.0)), n_samples=100)
    result = _edge_trade(0.30, 10, None, 0.15, 0.07, calibration=cm)
    check("calibrated-down prob no longer qualifies", result is None)
    # The NO side is calibrated on (1-prob) independently.
    cm_no = CalibrationMap(breakpoints=((0.0, 0.0), (0.70, 0.95), (1.0, 1.0)), n_samples=100)
    result_no = _edge_trade(0.30, None, 10, 0.15, 0.07, calibration=cm_no)
    check("NO side calibrated on 1-prob and still qualifies (0.95 vs 10c)",
          result_no is not None and result_no[0] == "no" and abs(result_no[2] - 0.95) < 1e-9)


def _iso(h):
    return datetime(2026, 7, 20, h, tzinfo=timezone.utc).isoformat()


def test_first_qualifying_edge_calibration_changes_outcome():
    # Both snapshots look tradeable at raw prob, but calibration kills the earlier
    # one and lets the later one through -- calibration changes WHICH snapshot wins.
    candles = [Candle(int(datetime(2026, 7, 20, 10, tzinfo=timezone.utc).timestamp()), 10, 92),
               Candle(int(datetime(2026, 7, 20, 14, tzinfo=timezone.utc).timestamp()), 10, 92)]
    snaps = [{"created_at": _iso(10), "prob": 0.30}, {"created_at": _iso(14), "prob": 0.60}]
    uncalibrated = first_qualifying_edge(snaps, candles, threshold=0.15, coeff=0.07)
    check("uncalibrated enters at the first (overconfident) snapshot",
          uncalibrated is not None and uncalibrated[3] == 0.30)
    cm = CalibrationMap(breakpoints=((0.0, 0.0), (0.30, 0.05), (0.60, 0.55), (1.0, 1.0)), n_samples=100)
    calibrated = first_qualifying_edge(snaps, candles, threshold=0.15, coeff=0.07, calibration=cm)
    check("calibrated skips the overconfident snapshot, enters at the reliable one",
          calibrated is not None and calibrated[3] == 0.55)


async def main():
    test_pava_pools_violations(); test_bin_stats()
    test_fit_calibration_corrects_overconfidence(); test_fit_calibration_insufficient_data()
    test_calibration_map_apply(); test_serialize_roundtrip()
    test_edge_trade_applies_calibration(); test_first_qualifying_edge_calibration_changes_outcome()
    print("\n" + ("ALL PASS" if not failures else f"FAILURES: {failures}"))
    sys.exit(1 if failures else 0)

asyncio.run(main())
