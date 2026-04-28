#!/usr/bin/env bash
# Wait until the local Qwen3.6-27B vLLM server reports a model on /v1/models.
set -euo pipefail
cd "$(dirname "$0")/.."
HOST="${QWEN_SERVE_HOST:-127.0.0.1}"
PORT="${QWEN_SERVE_PORT:-8000}"
API_KEY="${QWEN_SERVE_API_KEY:-EMPTY}"
URL="http://$HOST:$PORT/v1/models"
echo "[wait] polling $URL ..."
for i in $(seq 1 600); do
  if curl -fsS -H "Authorization: Bearer $API_KEY" "$URL" >/dev/null 2>&1; then
    echo "[wait] ready after ${i}s"
    curl -fsS -H "Authorization: Bearer $API_KEY" "$URL"
    echo
    exit 0
  fi
  sleep 1
done
echo "[wait] timed out after 600s" >&2
exit 1
