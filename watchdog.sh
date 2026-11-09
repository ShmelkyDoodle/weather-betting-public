#!/usr/bin/env bash
# Run the watchdog once (for the launchd job or manually). Alerts if the tracker
# is silent, a settlement mapping needs review, the engine can no longer READ the
# order book, or it is alive but producing nothing. The last two exist because a
# fresh heartbeat is not a healthy system -- see weather_arb/watchdog.py.
set -euo pipefail
cd "$(dirname "$0")"

if [ -f .env ]; then
    set -a
    # shellcheck disable=SC1091
    source .env
    set +a
fi

PY="${WA_PYTHON:-.venv/bin/python}"
exec "$PY" -m weather_arb.watchdog
