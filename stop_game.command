#!/bin/zsh
set -eu

# デタッチ起動したローカルサーバーを Finder から止めるための補助です。
SCRIPT_DIR=$(cd "$(dirname "$0")" && pwd)
cd "$SCRIPT_DIR"

./stop_server.sh
