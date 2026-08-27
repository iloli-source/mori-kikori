#!/bin/bash
# 毎日0時5分(JST)にcronから実行され、Mori MCPから前日分の
# トランスクリプトを取得して data/ に保存する。

set -u

export TZ=Asia/Tokyo

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PYTHON_BIN="$SCRIPT_DIR/.venv/bin/python3"
PY_SCRIPT="$SCRIPT_DIR/mori_fetch.py"
LOG_DIR="$SCRIPT_DIR/logs"
LOG_FILE="$LOG_DIR/mori-cron.log"
LOCK_DIR="$SCRIPT_DIR/.run-lock"

mkdir -p "$LOG_DIR"

# 二重起動防止（mkdir はアトミック）。10分以上古いロックは残骸とみなして奪う。
if ! mkdir "$LOCK_DIR" 2>/dev/null; then
  if [ -n "$(find "$LOCK_DIR" -maxdepth 0 -mmin +10 2>/dev/null)" ]; then
    rmdir "$LOCK_DIR" 2>/dev/null || true
    mkdir "$LOCK_DIR" 2>/dev/null || { echo "$(date '+%F %T') lock busy, skip" >> "$LOG_FILE"; exit 0; }
  else
    echo "$(date '+%F %T') another run in progress, skip" >> "$LOG_FILE"
    exit 0
  fi
fi
trap 'rmdir "$LOCK_DIR" 2>/dev/null' EXIT

{
  echo "=== $(date '+%Y-%m-%d %H:%M:%S') START (--days-ago 1) ==="
  "$PYTHON_BIN" "$PY_SCRIPT" --days-ago 1
  EXIT_CODE=$?
  echo "=== $(date '+%Y-%m-%d %H:%M:%S') END (exit=$EXIT_CODE) ==="
  echo ""
} >> "$LOG_FILE" 2>&1

exit ${EXIT_CODE:-0}
