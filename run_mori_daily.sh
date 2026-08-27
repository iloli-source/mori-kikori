#!/bin/bash
# 毎日0時5分(JST)にcronから実行する。
# バックフィルモードで動くため、過去に失敗した日も自動で再試行され、
# 直近8日は文字起こし遅延(最大7日+境界余裕1日)の取り込みのため取得済みでも再取得する。

set -u

export TZ=Asia/Tokyo
export PYTHONUNBUFFERED=1

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PYTHON_BIN="$SCRIPT_DIR/.venv/bin/python3"
PY_SCRIPT="$SCRIPT_DIR/mori_fetch.py"
LOG_DIR="$SCRIPT_DIR/logs"
LOG_FILE="$LOG_DIR/mori-cron.log"
LOCK_DIR="$SCRIPT_DIR/.run-lock"
PID_FILE="$LOCK_DIR/pid"

mkdir -p "$LOG_DIR"

# 二重起動防止（mkdir はアトミック）。先行プロセスが生きている場合のみスキップする。
# PID の生存だけでなくコマンド名も確認し、PID 再利用で永久スキップに陥らないようにする。
if ! mkdir "$LOCK_DIR" 2>/dev/null; then
  OTHER_PID="$(cat "$PID_FILE" 2>/dev/null || true)"
  if [ -n "$OTHER_PID" ] && ps -o command= -p "$OTHER_PID" 2>/dev/null | grep -q 'run_mori_daily\.sh'; then
    echo "$(date '+%F %T') another run in progress (pid=$OTHER_PID), skip" >> "$LOG_FILE"
    exit 0
  fi
  # 残骸ロック（プロセス消滅 or 無関係プロセスの PID）を回収
  rm -rf "$LOCK_DIR"
  if ! mkdir "$LOCK_DIR" 2>/dev/null; then
    echo "$(date '+%F %T') lock busy, skip" >> "$LOG_FILE"
    exit 0
  fi
fi
echo $$ > "$PID_FILE"
# 残骸回収の同時競合(TOCTOU)対策: 書いた直後に自分の PID が残っているか再確認
if [ "$(cat "$PID_FILE" 2>/dev/null)" != "$$" ]; then
  echo "$(date '+%F %T') lock lost to concurrent reclaim, skip" >> "$LOG_FILE"
  exit 0
fi
# 所有者(自分のPIDが記録されている)場合のみロックを片付ける — 競合側のロックを消さない
trap '[ "$(cat "$PID_FILE" 2>/dev/null)" = "$$" ] && rm -rf "$LOCK_DIR"' EXIT

{
  echo "=== $(date '+%Y-%m-%d %H:%M:%S') START (backfill --refetch-recent 8) ==="
  "$PYTHON_BIN" "$PY_SCRIPT" --refetch-recent 8
  EXIT_CODE=$?
  echo "=== $(date '+%Y-%m-%d %H:%M:%S') END (exit=$EXIT_CODE) ==="
  echo ""
} >> "$LOG_FILE" 2>&1

exit ${EXIT_CODE:-0}
