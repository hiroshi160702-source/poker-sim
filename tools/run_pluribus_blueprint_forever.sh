#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="/Users/hiroshi/UEC/lab/poker-sim"
TABLE_DIR="$ROOT_DIR/app/sample_cpus/strategy_tables"
LOG_DIR="$ROOT_DIR/logs"

WAIT_PID="${1:-}"
CHUNK_ITERATIONS="${PLURIBUS_BLUEPRINT_CHUNK_ITERATIONS:-500000}"
PLAYERS="${PLURIBUS_BLUEPRINT_PLAYERS:-6}"
STACK="${PLURIBUS_BLUEPRINT_STACK:-5000}"
PRINT_EVERY="${PLURIBUS_BLUEPRINT_PRINT_EVERY:-10000}"
SEED="${PLURIBUS_BLUEPRINT_SEED:-20260504}"

BOOTSTRAP_STATE="$TABLE_DIR/pluribus_blueprint_6p_1000000_state.json"
BOOTSTRAP_TABLE="$TABLE_DIR/pluribus_blueprint_6p_1000000.json"
BOOTSTRAP_VISITS="$TABLE_DIR/pluribus_blueprint_6p_1000000_visits.json"

STATE="$TABLE_DIR/pluribus_blueprint_6p_continuous_state.json"
TABLE="$TABLE_DIR/pluribus_blueprint_6p_continuous.json"
VISITS="$TABLE_DIR/pluribus_blueprint_6p_continuous_visits.json"
RUN_LOG="$LOG_DIR/pluribus_blueprint_6p_continuous.log"

mkdir -p "$TABLE_DIR" "$LOG_DIR"
cd "$ROOT_DIR"

timestamp() {
  date '+%Y-%m-%d %H:%M:%S'
}

completed_iterations() {
  python3 - "$1" <<'PY'
import json
import pathlib
import sys

path = pathlib.Path(sys.argv[1])
if not path.exists():
    print(0)
    raise SystemExit
data = json.loads(path.read_text())
print(data.get("completed_iterations") or data.get("completed_iterations_total") or 0)
PY
}

if [[ -n "$WAIT_PID" ]]; then
  echo "[$(timestamp)] waiting for existing training pid=$WAIT_PID" | tee -a "$RUN_LOG"
  while kill -0 "$WAIT_PID" 2>/dev/null; do
    sleep 30
  done
  echo "[$(timestamp)] existing training pid=$WAIT_PID finished" | tee -a "$RUN_LOG"
fi

if [[ ! -f "$STATE" ]]; then
  if [[ ! -f "$BOOTSTRAP_STATE" ]]; then
    echo "[$(timestamp)] missing bootstrap state: $BOOTSTRAP_STATE" | tee -a "$RUN_LOG"
    exit 1
  fi
  cp "$BOOTSTRAP_STATE" "$STATE"
  [[ -f "$BOOTSTRAP_TABLE" ]] && cp "$BOOTSTRAP_TABLE" "$TABLE"
  [[ -f "$BOOTSTRAP_VISITS" ]] && cp "$BOOTSTRAP_VISITS" "$VISITS"
  echo "[$(timestamp)] bootstrapped continuous state from $BOOTSTRAP_STATE" | tee -a "$RUN_LOG"
fi

while true; do
  before="$(completed_iterations "$STATE")"
  target=$((before + CHUNK_ITERATIONS))
  echo "[$(timestamp)] starting chunk: before=$before target=$target iterations=$CHUNK_ITERATIONS" | tee -a "$RUN_LOG"

  python3 -u tools/strategy_preflop_multiway.py \
    --iterations "$CHUNK_ITERATIONS" \
    --players "$PLAYERS" \
    --stack "$STACK" \
    --resume-state "$STATE" \
    --out "$TABLE" \
    --checkpoint-out "$STATE" \
    --print-every "$PRINT_EVERY" \
    --min-visits 1 \
    --seed "$SEED" 2>&1 | tee -a "$RUN_LOG"

  after="$(completed_iterations "$STATE")"
  echo "[$(timestamp)] finished chunk: before=$before after=$after" | tee -a "$RUN_LOG"
  sleep 3
done
