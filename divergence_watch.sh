#!/usr/bin/env bash
# Long-running live NWS-vs-WU divergence detector (for the launchd job or manually).
# Each cycle it recomputes today's running divergence per city and pushes an ntfy
# alert the moment a divergence / 2F bin-flip newly appears. Reuses the same .env
# (ntfy topic etc.) as the tracker. Read-only -- nothing is ever traded.
set -euo pipefail
cd "$(dirname "$0")"

if [ -f .env ]; then
    set -a
    # shellcheck disable=SC1091
    source .env
    set +a
fi

PY="${WA_PYTHON:-.venv/bin/python}"
if [ ! -x "$PY" ]; then
    echo "python not found at $PY -- create the venv: python3 -m venv .venv && .venv/bin/pip install -r requirements.txt" >&2
    exit 1
fi

# WA_DIVERGENCE_ARGS customizes without editing this file, e.g.
#   WA_DIVERGENCE_ARGS="--interval 300 --city DEN --city CHI --city MIA"
# shellcheck disable=SC2086
exec "$PY" -m weather_arb.divergence --watch ${WA_DIVERGENCE_ARGS:-}
