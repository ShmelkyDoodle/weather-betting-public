#!/usr/bin/env bash
# Run the settlement-mapping verification once (for the daily launchd job or
# manually). Exits non-zero if any mapping can't be confirmed.
set -euo pipefail
cd "$(dirname "$0")"

if [ -f .env ]; then
    set -a
    # shellcheck disable=SC1091
    source .env
    set +a
fi

PY="${WA_PYTHON:-.venv/bin/python}"
echo "===== verify_mappings $(date -u +%Y-%m-%dT%H:%M:%SZ) ====="
exec "$PY" -m weather_arb.verify_mappings
