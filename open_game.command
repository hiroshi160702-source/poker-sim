#!/bin/zsh
set -eu

SCRIPT_DIR=$(cd "$(dirname "$0")" && pwd)
cd "$SCRIPT_DIR"

./start_server.sh
sleep 1
open http://127.0.0.1:8000
