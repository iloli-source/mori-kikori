#!/bin/bash
# 毎日0時5分にcronから実行され、Mori MCPから前日分の
# トランスクリプトを取得して data/ に保存する。

set -u

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PYTHON_BIN="$SCRIPT_DIR/.venv/bin/python3"
PY_SCRIPT="$SCRIPT_DIR/mori_fetch.py"
LOG_DIR="$SCRIPT_DIR/logs"
LOG_FILE="$LOG_DIR/mori-cron.log"

mkdir -p "$LOG_DIR"

{
  echo "=== $(date '+%Y-%m-%d %H:%M:%S') START (--days-ago 1) ==="
  "$PYTHON_BIN" "$PY_SCRIPT" --days-ago 1
  EXIT_CODE=$?
  echo "=== $(date '+%Y-%m-%d %H:%M:%S') END (exit=$EXIT_CODE) ==="
  echo ""
} >> "$LOG_FILE" 2>&1

exit ${EXIT_CODE:-0}
