#!/usr/bin/env bash
# Regenerate the visual dashboard once (for the weekly launchd job or manually).
# Writes report.html and keeps a dated copy under reports/ for a week-by-week
# history.
set -euo pipefail
cd "$(dirname "$0")"

if [ -f .env ]; then
    set -a
    # shellcheck disable=SC1091
    source .env
    set +a
fi

PY="${WA_PYTHON:-.venv/bin/python}"
mkdir -p reports
STAMP="$(date -u +%Y-%m-%d)"

# --- keep prob_grid_fixed current BEFORE anything reads it -------------------
# Rows written since the last run have prob_grid_fixed = NULL, and
# `backtest --grid-fixed` filters those out -- so without this, every backtest
# silently ignores the most recent week. The script is idempotent and only ever
# writes the prob_grid_fixed column; `prob` is never touched.
#
# ⚠ GRID_FIX_CUTOVER IS A FIXED HISTORICAL CONSTANT -- DO NOT change it to
# `date -u` or "now". It is the instant the tracker restarted onto the corrected
# bucket grid. Rows BEFORE it were priced on the old grid and must be recomputed
# by inversion; rows AFTER it were born correct and are copied straight across.
# Moving the cutover forward would send correctly-priced rows through the
# inversion path, where they would at best fail validation and at worst fit noise.
GRID_FIX_CUTOVER="2026-08-24T00:05:22+00:00"

# --no-backup: the weekly cadence would otherwise leave a ~200 MB VACUUM INTO
# snapshot behind every week, and this run is non-destructive anyway.
# Non-fatal: the dashboard is the point of this job, so a backfill hiccup must
# not abort it (weekly.sh runs under `set -e`).
echo "$(date -u +%Y-%m-%dT%H:%M:%SZ)  backfilling prob_grid_fixed since ${GRID_FIX_CUTOVER}"
if ! "$PY" scripts/backfill_grid_probs.py \
        --corrected-since "$GRID_FIX_CUTOVER" --no-backup; then
    echo "$(date -u +%Y-%m-%dT%H:%M:%SZ)  WARNING: backfill failed -- " \
         "--grid-fixed backtests will be missing the latest rows" >&2
fi

"$PY" -m weather_arb.report --html report.html
cp report.html "reports/report-${STAMP}.html"
echo "$(date -u +%Y-%m-%dT%H:%M:%SZ)  wrote report.html + reports/report-${STAMP}.html"
