#!/bin/zsh
set -eu

# ローカル用 FastAPI サーバーをデタッチ起動し、起動したターミナルを
# 閉じてもすぐ止まらないようにします。
SCRIPT_DIR=$(cd "$(dirname "$0")" && pwd)
PID_FILE="$SCRIPT_DIR/.poker-sim.pid"
LOG_FILE="$SCRIPT_DIR/.poker-sim.log"

if [[ -f "$PID_FILE" ]]; then
  # PID が生きていれば既存サーバーを再利用し、死んでいれば古い PID
  # ファイルを消してから新しく起動します。
  PID=$(cat "$PID_FILE")
  if kill -0 "$PID" 2>/dev/null; then
    echo "Server is already running: http://127.0.0.1:8000 (PID $PID)"
    exit 0
  fi
  rm -f "$PID_FILE"
fi

cd "$SCRIPT_DIR"
PID=$(python3 - <<'PY'
import subprocess
from pathlib import Path

script_dir = Path.cwd()
log_file = script_dir / ".poker-sim.log"
# uvicorn は新しいセッションで起動し、このスクリプト終了後も動き続けます。
with log_file.open("ab") as stream:
    process = subprocess.Popen(
        ["python3", "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", "8000"],
        cwd=str(script_dir),
        stdout=stream,
        stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL,
        start_new_session=True,
    )
print(process.pid)
PY
)
echo "$PID" > "$PID_FILE"
echo "Server started: http://127.0.0.1:8000 (PID $PID)"
