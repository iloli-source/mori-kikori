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
FAIL_COUNT_FILE="$LOG_DIR/.consecutive-failures"
NOTIFY_AFTER_FAILURES=3

mkdir -p "$LOG_DIR"

notify() {
  # macOS のみデスクトップ通知（osascript が無い環境では黙ってスキップ）
  command -v osascript >/dev/null 2>&1 || return 0
  osascript -e "display notification \"$1\" with title \"mori-kikori\"" 2>/dev/null || true
}

log() {
  echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*" >> "$LOG_FILE"
}

TODAY="$(date '+%F')"
LAST_SUCCESS="$(cat "$STAMP_FILE" 2>/dev/null || true)"

if [ "$LAST_SUCCESS" = "$TODAY" ]; then
  # 成功済みの日は即スキップ。1行だけログを残し、毎時発火が生きていること
  # （launchd が動いている証拠）を後から確認できるようにする。
  log "already succeeded today ($TODAY) — skip"
  exit 0
fi

# catchup 同士の多重起動を防止（launchd の RunAtLoad と StartInterval が
# ほぼ同時に発火し得る）。これがないと「片方が daily.sh のロックに弾かれて
# exit 0 → 実処理していないのに成功スタンプを書く」レースの入口になる。
# daily.sh と同じく PID 生存確認付き: kill -9 や再起動でロックが残っても
# 永久スキップに陥らず自動回収する。
CATCHUP_LOCK="$SCRIPT_DIR/.catchup-lock"
CATCHUP_PID_FILE="$CATCHUP_LOCK/pid"
if ! mkdir "$CATCHUP_LOCK" 2>/dev/null; then
  OTHER_PID="$(cat "$CATCHUP_PID_FILE" 2>/dev/null || true)"
  if [ -n "$OTHER_PID" ] && ps -o command= -p "$OTHER_PID" 2>/dev/null | grep -q 'run_mori_catchup\.sh'; then
    log "another catchup in progress (pid=$OTHER_PID) — skip"
    exit 0
  fi
  # 残骸ロック（プロセス消滅 or 無関係プロセスの PID）を回収
  rm -rf "$CATCHUP_LOCK"
  if ! mkdir "$CATCHUP_LOCK" 2>/dev/null; then
    log "catchup lock busy — skip"
    exit 0
  fi
fi
echo $$ > "$CATCHUP_PID_FILE"
# 残骸回収の同時競合(TOCTOU)対策: 書いた直後に自分の PID が残っているか再確認
if [ "$(cat "$CATCHUP_PID_FILE" 2>/dev/null)" != "$$" ]; then
  log "catchup lock lost to concurrent reclaim — skip"
  exit 0
fi
# 所有者の場合のみロックを片付ける — 競合側のロックを消さない
trap '[ "$(cat "$CATCHUP_PID_FILE" 2>/dev/null)" = "$$" ] && rm -rf "$CATCHUP_LOCK"' EXIT

# 簡易ローテーション: 毎時のスキップ行で肥大しないよう、512KB を超えたら直近500行だけ残す。
# ロック取得後に行うことで、同時起動とのローテーション競合によるログ喪失を防ぐ。
if [ -f "$LOG_FILE" ] && [ "$(wc -c < "$LOG_FILE")" -gt 524288 ]; then
  tail -500 "$LOG_FILE" > "$LOG_FILE.tmp" && mv "$LOG_FILE.tmp" "$LOG_FILE"
fi

# run_mori_daily.sh は「別プロセス実行中でスキップ」時も exit 0 を返すため、
# その場合に成功スタンプを書いてしまわないよう、先行実行中なら何もせず抜ける。
# （手動実行との一瞬のレースは残るが、誤スタンプしても翌日のバックフィル
#  --refetch-recent 8 が同じ日を再取得するためデータは欠損しない）
if [ -d "$SCRIPT_DIR/.run-lock" ]; then
  OTHER_PID="$(cat "$SCRIPT_DIR/.run-lock/pid" 2>/dev/null || true)"
  if [ -n "$OTHER_PID" ] && ps -o command= -p "$OTHER_PID" 2>/dev/null | grep -q 'run_mori_daily\.sh'; then
    log "another run in progress (pid=$OTHER_PID) — skip without stamping"
    exit 0
  fi
fi

log "=== catchup start (last_success=${LAST_SUCCESS:-none}) ==="
# 今回の実行で新しく書かれた本体ログだけを失敗原因の判定に使う
# （過去の認証エラー行に反応して誤通知しないため）
CRON_LOG="$LOG_DIR/mori-cron.log"
CRON_LOG_START="$(wc -l < "$CRON_LOG" 2>/dev/null || echo 0)"
/bin/bash "$SCRIPT_DIR/run_mori_daily.sh"
EXIT_CODE=$?

if [ "$EXIT_CODE" -eq 0 ]; then
  echo "$TODAY" > "$STAMP_FILE"
  rm -f "$FAIL_COUNT_FILE"
  log "=== catchup success — stamped $TODAY ==="
else
  FAILS=$(( $(cat "$FAIL_COUNT_FILE" 2>/dev/null || echo 0) + 1 ))
  echo "$FAILS" > "$FAIL_COUNT_FILE"
  log "=== catchup failed (exit=$EXIT_CODE, consecutive=$FAILS) — will retry on next launchd fire ==="
  # 認証失効はユーザー操作(--login)がないと永久に直らないため即通知。
  # それ以外の原因（API変更・ネットワーク等）も、連続 N 回失敗したら通知して
  # サイレント停止を防ぐ。
  if tail -n +$((CRON_LOG_START + 1)) "$CRON_LOG" 2>/dev/null | grep -q "認証が失効"; then
    notify "mori の認証が失効しています。リポジトリ直下で .venv/bin/python mori_fetch.py --login を実行してください。"
  elif [ "$FAILS" -ge "$NOTIFY_AFTER_FAILURES" ]; then
    notify "mori の取得が ${FAILS} 回連続で失敗しています。logs/mori-cron.log を確認してください。"
  fi
fi

exit "$EXIT_CODE"
