#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT"

PYTHONPATH="anomaly-dashboard/.python_deps:.python_deps" \
  python anomaly-dashboard/pipeline/build_moe_analytics_store.py "$@"
