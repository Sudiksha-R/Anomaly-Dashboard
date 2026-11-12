#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
APP_ROOT="$ROOT/anomaly-dashboard"

export PYTHONPATH="$APP_ROOT/.python_deps:$ROOT/.python_deps"
python "$APP_ROOT/pipeline/build_anomaly_store.py" "$@"
