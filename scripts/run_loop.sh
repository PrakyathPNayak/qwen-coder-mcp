#!/usr/bin/env bash
# Launch the agentic loop as a detached background process.
# Usage:
#   ./scripts/run_loop.sh          # runs forever, logs to .loop/runtime.log
#   tail -f .loop/runtime.log      # watch progress
#   kill "$(cat .loop/loop.pid)"   # stop it
set -euo pipefail
cd "$(dirname "$0")/.."
mkdir -p .loop
if [ -f .loop/loop.pid ] && kill -0 "$(cat .loop/loop.pid)" 2>/dev/null; then
  echo "loop already running (pid $(cat .loop/loop.pid))" >&2
  exit 1
fi
nohup python -m agent.loop >> .loop/runtime.log 2>&1 &
echo $! > .loop/loop.pid
echo "loop started (pid $(cat .loop/loop.pid)); tail .loop/runtime.log"
