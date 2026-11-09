#!/usr/bin/env bash
# Rotate oversized launchd logs (for the daily launchd job or manually). Uses
# copytruncate so it works while launchd holds the log files open.
set -euo pipefail
cd "$(dirname "$0")"

if [ -f .env ]; then
    set -a
    # shellcheck disable=SC1091
    source .env
    set +a
fi

PY="${WA_PYTHON:-.venv/bin/python}"
# WA_LOGROTATE_ARGS overrides defaults, e.g. "--max-mb 100 --keep 7".
# shellcheck disable=SC2086
exec "$PY" -m weather_arb.logrotate ${WA_LOGROTATE_ARGS:---max-mb 50 --keep 5}
