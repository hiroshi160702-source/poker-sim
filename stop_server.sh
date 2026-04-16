#!/bin/zsh
set -eu

# start_server.sh で起動したデタッチ済みローカルサーバーを停止します。
SCRIPT_DIR=$(cd "$(dirname "$0")" && pwd)
PID_FILE="$SCRIPT_DIR/.poker-sim.pid"

if [[ ! -f "$PID_FILE" ]]; then
  echo "Server is not running."
  exit 0
fi

PID=$(cat "$PID_FILE")
if kill -0 "$PID" 2>/dev/null; then
  kill "$PID"
  echo "Server stopped (PID $PID)."
else
  echo "PID file existed, but process $PID was not running."
fi

rm -f "$PID_FILE"
