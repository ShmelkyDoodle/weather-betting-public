#!/usr/bin/env bash
# Launch the weather-arb tracker. Sources .env (if present) and runs the
# package from the project's virtualenv. Used both interactively and by launchd.
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

# Keep the machine awake while running so a multi-week paper run doesn't lose
# data to idle/system/disk sleep. NOTE: caffeinate cannot stop a laptop from
# sleeping when the LID is CLOSED on battery -- keep it plugged in with the lid
# open (or use clamshell with an external display) for a gap-free run.
if command -v caffeinate >/dev/null 2>&1; then
    exec caffeinate -ims "$PY" -m weather_arb.main
else
    exec "$PY" -m weather_arb.main
fi
