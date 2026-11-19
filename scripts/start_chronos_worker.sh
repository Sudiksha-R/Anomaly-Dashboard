#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
CHRONOS_JOB_ID="${1:-${SLURM_JOB_ID:-}}"
if [[ -z "$CHRONOS_JOB_ID" || ! "$CHRONOS_JOB_ID" =~ ^[0-9]+$ ]]; then
  echo "Usage: bash anomaly-dashboard/scripts/start_chronos_worker.sh YOUR_GPU_JOB_ID" >&2
  exit 2
fi
exec srun --jobid="$CHRONOS_JOB_ID" --overlap --exact --nodes=1 --ntasks=1 \
  --cpus-per-task=4 --mem=12G --gres=gpu:1 /usr/bin/env PATH=/usr/bin:/bin \
  "$ROOT/experiments/traffic_forecast/.venv/bin/python" \
  "$ROOT/anomaly-dashboard/scripts/serve_chronos_requests.py"
