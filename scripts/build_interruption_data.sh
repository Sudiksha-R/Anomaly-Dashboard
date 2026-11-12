#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT"

if [[ "$#" -gt 0 ]]; then
  PYTHONPATH="anomaly-dashboard/.python_deps:.python_deps" \
    python anomaly-dashboard/pipeline/build_interruption_store.py "$@"
  exit 0
fi

PYTHONPATH="anomaly-dashboard/.python_deps:.python_deps" \
  python anomaly-dashboard/pipeline/build_interruption_store.py

PYTHONPATH="anomaly-dashboard/.python_deps:.python_deps" \
  python anomaly-dashboard/pipeline/build_5min_volume_store.py

PYTHONPATH="anomaly-dashboard/.python_deps:.python_deps" \
  python anomaly-dashboard/pipeline/build_interruption_store.py \
    --source anomaly-dashboard/data/moe_volume_5min.duckdb \
    --output anomaly-dashboard/data/interruption_events_5min.duckdb \
    --interval-minutes 5
