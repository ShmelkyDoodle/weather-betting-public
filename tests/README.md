# Tests

Offline unit/integration tests. They stub all network I/O, so they run fast, need
no internet, and never touch Kalshi/NOAA.

## Run

From the repo root, using the venv's Python (no activation needed):

```bash
for f in tests/*.py; do echo "== $f =="; .venv/bin/python "$f"; done
```

Or one at a time:

```bash
.venv/bin/python tests/smoke_test.py
```

Each script prints `PASS`/`FAIL` per check and ends with `ALL PASS` (exit 0) or
`FAILURES: [...]` (exit 1).

## Coverage

| File | Covers |
|---|---|
| `smoke_test.py` | order-book math, Kalshi parsing, truncated-normal, LST climate day, DB atomic Tmax + exposure, risk sizing, Strategy-1 dry-run lock-in |
| `test_glitch.py` | METAR sensor-glitch protection (pending / 5-min confirm / neighbor corroboration / dewpoint reject) |
| `test_v2.py` | Kalshi range buckets, discovery parsing, settlement P&L, certain-NO lock-in |
| `test_v3.py` | fees, `model_prob` persistence + DB migration, calibration/Brier, fee-aware edge |
| `test_v4.py` | mapping-verification classifier + discovery self-guard |
| `test_v5.py` | multi-model forecasting + per-model Brier comparison |
| `test_v6.py` | forecast bias tracking + bias-corrected variants |
| `test_v7.py` | best-model-by-Brier auto-selection |
| `test_v8.py` | climatology base-rate model + forecast/climatology blend |
| `test_v9.py` | cross-venue basis monitor: scope toggle, pair classification, lock math |
| `test_v10.py` | cross-venue live quotes: bucket parsing, Polymarket Gamma parse, bucket-aligned lock scan |
| `test_v11.py` | ForecastEx via IBKR: snapshot parse, threshold→2°F-bin synthesis, synthesized cross-book lock |
| `test_v12.py` | NWS-vs-WU divergence: rounding/2°F bins, LST day-max, classify (+ gappy-1min floor), summarize |
| `test_v13.py` | 1-minute lock-in augmenter: corroboration (lone-glitch skip), `_apply` safety gates (sanity cap, no-op) |
| `test_v14.py` | WU recalibration: forecast error stats (bias/MAE/RMSE), Kalshi→Polymarket WU-station resolution |
| `test_v15.py` | Strategy-2 backtest: fee-adjusted edge-trade selection, per-model P&L summary + ranking |
| `test_v16.py` | intraday daily-max model: remaining-rise samples, per-hour curve, prediction + post-peak collapse |
| `test_v17.py` | passive model-edge placement: resting bid/ask pricing (improve, no-cross cap, edge-limit floor) |
| `test_v18.py` | fractional-Kelly + correlated-group sizing: kelly_count, model-edge downsizing, group cap/rejection |
| `test_v19.py` | log rotation: copytruncate + gzip generations, threshold skip, oldest-drop, selective dir rotate |
| `test_v20.py` | prediction history + P&L selection: `_price_at`, first-qualifying-edge, gated snapshotting, `model_pnl` |
| `test_v21.py` | probability recalibration: PAVA isotonic fit, binning, (de)serialize, edge-trade + first-qualifying-edge calibration wiring |
| `test_v22.py` | intraday → live forecast: `blend_normal` precision weighting + no-unearned-narrowing, non-blocking curve cache (build-once, failure backoff, wait mode), `_id` shadow variants, opt-in trading gate |
| `test_v23.py` | out-of-sample threshold sweep: config stats, min-train-trades guard (no winning by trading rarely), train-picks/test-scores split, threshold selectivity monotonicity |

Prereq: `.venv` with `requirements.txt` installed (see [../README.md](../README.md)).
Compile-check everything: `python3 -m py_compile weather_arb/*.py`.
