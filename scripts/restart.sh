#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
APP_ROOT="$ROOT/anomaly-dashboard"
BACKEND_PORT="${ANOMALY_BACKEND_PORT:-8010}"
FRONTEND_PORT="${ANOMALY_FRONTEND_PORT:-4180}"
BACKEND_LOG="$APP_ROOT/backend-$BACKEND_PORT.log"
FRONTEND_LOG="$APP_ROOT/frontend-$FRONTEND_PORT.log"

export PATH="/apps/jupyter/6.5.4/bin:$PATH"
PYTHON_BIN="${ANOMALY_PYTHON_BIN:-$(command -v python || true)}"
NPM_BIN="${ANOMALY_NPM_BIN:-$(command -v npm || true)}"

if [[ -z "$PYTHON_BIN" || -z "$NPM_BIN" ]]; then
  echo "Could not find Python or npm."
  echo "Python: ${PYTHON_BIN:-missing}"
  echo "npm:    ${NPM_BIN:-missing}"
  exit 1
fi

pkill -u "$USER" -f "$APP_ROOT/backend/main.py" 2>/dev/null || true
pkill -u "$USER" -f "uvicorn main:app.*--port $BACKEND_PORT" 2>/dev/null || true
pkill -u "$USER" -f "vite.*--port $FRONTEND_PORT" 2>/dev/null || true

export PYTHONPATH="$APP_ROOT/.python_deps:$ROOT/.python_deps:$APP_ROOT/backend"
cd "$APP_ROOT/backend"
nohup "$PYTHON_BIN" -m uvicorn main:app --host 127.0.0.1 --port "$BACKEND_PORT" > "$BACKEND_LOG" 2>&1 < /dev/null &
BACKEND_PID=$!

cd "$APP_ROOT/frontend"
nohup "$NPM_BIN" run dev -- --host 127.0.0.1 --port "$FRONTEND_PORT" --strictPort > "$FRONTEND_LOG" 2>&1 < /dev/null &
FRONTEND_PID=$!

BACKEND_OK=0
for _ in $(seq 1 30); do
  if curl --max-time 2 -fsS "http://127.0.0.1:$BACKEND_PORT/api/health" >/dev/null 2>&1; then
    BACKEND_OK=1
    break
  fi
  if ! kill -0 "$BACKEND_PID" 2>/dev/null; then
    break
  fi
  sleep 1
done

if [[ "$BACKEND_OK" != "1" ]]; then
  echo "The anomaly API failed to start."
  tail -40 "$BACKEND_LOG" || true
  exit 1
fi

FRONTEND_OK=0
for _ in $(seq 1 30); do
  if curl --max-time 2 -fsS "http://127.0.0.1:$FRONTEND_PORT/" >/dev/null 2>&1; then
    FRONTEND_OK=1
    break
  fi
  if ! kill -0 "$FRONTEND_PID" 2>/dev/null; then
    break
  fi
  sleep 1
done

if [[ "$FRONTEND_OK" != "1" ]]; then
  echo "The anomaly frontend failed to start."
  tail -40 "$FRONTEND_LOG" || true
  exit 1
fi

echo "Anomaly dashboard is running."
echo "Frontend: http://127.0.0.1:$FRONTEND_PORT/"
echo "API:      http://127.0.0.1:$BACKEND_PORT/api/health"
echo "PIDs:     backend=$BACKEND_PID frontend=$FRONTEND_PID"
echo "Logs:     $BACKEND_LOG"
echo "          $FRONTEND_LOG"
