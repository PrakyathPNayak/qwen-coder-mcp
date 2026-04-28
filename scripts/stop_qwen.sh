#!/usr/bin/env bash
# Stop the locally-served Qwen3.6-27B vLLM process started by serve_qwen.sh.
set -euo pipefail
cd "$(dirname "$0")/.."
PIDFILE=".loop/serve.pid"
if [ ! -f "$PIDFILE" ]; then
  echo "no pid file ($PIDFILE) — is the server running?" >&2
  exit 1
fi
PID="$(cat "$PIDFILE")"
if kill -0 "$PID" 2>/dev/null; then
  kill "$PID"
  for _ in $(seq 1 30); do
    kill -0 "$PID" 2>/dev/null || break
    sleep 1
  done
  if kill -0 "$PID" 2>/dev/null; then
    kill -9 "$PID" 2>/dev/null || true
  fi
  echo "[serve_qwen] stopped pid $PID"
else
  echo "[serve_qwen] pid $PID not running"
fi
rm -f "$PIDFILE"
