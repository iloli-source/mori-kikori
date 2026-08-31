#!/bin/bash
# launchd (RunAtLoad + StartCalendarInterval 00:15 + StartInterval 3600) から呼ばれる
# キャッチアップラッパー。
#
# 背景:
# - cron は Mac スリープ/電源断中に発火せず、起床後も取りこぼし分を実行しない。
# - launchd + 本スクリプトで「毎日 0:15」「PC を開いた時」「毎時リトライ」の
#   3経路から起動し、その日まだ成功していない場合のみ run_mori_daily.sh を実行する。
#
# 設計:
# - スタンプ logs/.last-success-date に最終成功日(JST)を記録。今日と一致なら即スキップ。
# - 実行が exit 0 のときだけスタンプを書く。失敗日はスタンプが残らず次の発火で再試行。
# - 取りこぼし日の回収は run_mori_daily.sh のバックフィルモード(--refetch-recent 8)が担う。
# - 多重起動防止は run_mori_daily.sh 内の mkdir ロックに委譲する。

set -u
export TZ=Asia/Tokyo

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
LOG_DIR="$SCRIPT_DIR/logs"
LOG_FILE="$LOG_DIR/mori-catchup.log"
STAMP_FILE="$LOG_DIR/.last-success-date"

mkdir -p "$LOG_DIR"

log() {
  echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*" >> "$LOG_FILE"
}

TODAY="$(date '+%F')"
LAST_SUCCESS="$(cat "$STAMP_FILE" 2>/dev/null || true)"

if [ "$LAST_SUCCESS" = "$TODAY" ]; then
  # 成功済みの日は毎時発火してもコストゼロで抜ける
  exit 0
fi

# run_mori_daily.sh は「別プロセス実行中でスキップ」時も exit 0 を返すため、
# その場合に成功スタンプを書いてしまわないよう、先行実行中なら何もせず抜ける。
if [ -d "$SCRIPT_DIR/.run-lock" ]; then
  OTHER_PID="$(cat "$SCRIPT_DIR/.run-lock/pid" 2>/dev/null || true)"
  if [ -n "$OTHER_PID" ] && ps -o command= -p "$OTHER_PID" 2>/dev/null | grep -q 'run_mori_daily\.sh'; then
    log "another run in progress (pid=$OTHER_PID) — skip without stamping"
    exit 0
  fi
fi

log "=== catchup start (last_success=${LAST_SUCCESS:-none}) ==="
/bin/bash "$SCRIPT_DIR/run_mori_daily.sh"
EXIT_CODE=$?

if [ "$EXIT_CODE" -eq 0 ]; then
  echo "$TODAY" > "$STAMP_FILE"
  log "=== catchup success — stamped $TODAY ==="
else
  log "=== catchup failed (exit=$EXIT_CODE) — will retry on next launchd fire ==="
fi

exit "$EXIT_CODE"
