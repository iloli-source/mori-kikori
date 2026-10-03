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
# - exit 75 は「別プロセスが実行中で何もしなかった」。成功でも失敗でもないので
#   スタンプも失敗カウントも触らず、次の発火に任せる（formal/CatchupStamp.tla）。
# - 取りこぼし日の回収は run_mori_daily.sh のバックフィルモード(--refetch-recent 8)が担う。
# - 多重起動防止は flock(2)（runlock.py）。本処理のロックは run_mori_daily.sh が取る。

set -u
export TZ=Asia/Tokyo

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
LOG_DIR="$SCRIPT_DIR/logs"
LOG_FILE="$LOG_DIR/mori-catchup.log"
STAMP_FILE="$LOG_DIR/.last-success-date"
FAIL_COUNT_FILE="$LOG_DIR/.consecutive-failures"
NOTIFY_AFTER_FAILURES=3
PYTHON_BIN="$SCRIPT_DIR/.venv/bin/python3"
# daily.sh が「別プロセス実行中で何もしなかった」ことを示す終了コード（daily.sh の SKIP_EXIT と一致させる）
SKIP_EXIT=75
# テストから差し替えられるように（既定は同じディレクトリの run_mori_daily.sh / .catchup.lock）
DAILY_SCRIPT="${MORI_DAILY_SCRIPT:-$SCRIPT_DIR/run_mori_daily.sh}"
CATCHUP_LOCK_FILE="${MORI_CATCHUP_LOCK_FILE:-$SCRIPT_DIR/.catchup.lock}"

mkdir -p "$LOG_DIR"

notify() {
  # macOS のみデスクトップ通知（osascript が無い環境では黙ってスキップ）
  command -v osascript >/dev/null 2>&1 || return 0
  osascript -e "display notification \"$1\" with title \"mori-kikori\"" 2>/dev/null || true
}

log() {
  echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*" >> "$LOG_FILE"
}

# 連続失敗カウンタを1つ進める（FAILS に現在の連続回数を残す）
count_failure() {
  FAILS=$(( $(cat "$FAIL_COUNT_FILE" 2>/dev/null || echo 0) + 1 ))
  echo "$FAILS" > "$FAIL_COUNT_FILE"
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
# ほぼ同時に発火し得る）。ログのローテーションと失敗カウンタの読み書きを直列化する。
# daily.sh と同じ flock 方式: kill -9 や再起動でも OS がロックを解放するので、
# 残骸回収が不要で永久スキップにも陥らない。
if ! exec 8>>"$CATCHUP_LOCK_FILE"; then
  log "cannot open catchup lock file $CATCHUP_LOCK_FILE"
  exit 1
fi
"$PYTHON_BIN" "$SCRIPT_DIR/runlock.py" 8
LOCK_RC=$?
if [ "$LOCK_RC" -eq "$SKIP_EXIT" ]; then
  log "another catchup in progress — skip"
  exit 0
elif [ "$LOCK_RC" -ne 0 ]; then
  # ロック確認自体ができない（.venv が壊れている等）。黙って止まらないよう失敗として数える
  count_failure
  log "=== cannot take catchup lock (rc=$LOCK_RC, consecutive=$FAILS) ==="
  if [ "$FAILS" -ge "$NOTIFY_AFTER_FAILURES" ]; then
    notify "mori の取得が ${FAILS} 回連続で失敗しています。logs/mori-catchup.log を確認してください。"
  fi
  exit 1
fi

# 簡易ローテーション: 毎時のスキップ行で肥大しないよう、512KB を超えたら直近500行だけ残す。
# ロック取得後に行うことで、同時起動とのローテーション競合によるログ喪失を防ぐ。
if [ -f "$LOG_FILE" ] && [ "$(wc -c < "$LOG_FILE")" -gt 524288 ]; then
  tail -500 "$LOG_FILE" > "$LOG_FILE.tmp" && mv "$LOG_FILE.tmp" "$LOG_FILE"
fi

log "=== catchup start (last_success=${LAST_SUCCESS:-none}) ==="
# 今回の実行で新しく書かれた本体ログだけを失敗原因の判定に使う
# （過去の認証エラー行に反応して誤通知しないため）
CRON_LOG="$LOG_DIR/mori-cron.log"
CRON_LOG_START="$(wc -l < "$CRON_LOG" 2>/dev/null || echo 0)"
/bin/bash "$DAILY_SCRIPT"
EXIT_CODE=$?

if [ "$EXIT_CODE" -eq "$SKIP_EXIT" ]; then
  # 別プロセス（手動実行など）が本処理中。成功でも失敗でもないのでスタンプも失敗カウントも
  # 触らず、次の発火に任せる。ここでスタンプすると、その実行が失敗しても当日は再試行されない
  log "=== daily skipped (another run in progress) — not stamping, not counted as failure ==="
  exit 0
fi

if [ "$EXIT_CODE" -eq 0 ]; then
  echo "$TODAY" > "$STAMP_FILE"
  rm -f "$FAIL_COUNT_FILE"
  log "=== catchup success — stamped $TODAY ==="
else
  count_failure
  log "=== catchup failed (exit=$EXIT_CODE, consecutive=$FAILS) — will retry on next launchd fire ==="
  # 認証失効はユーザー操作(--login)がないと永久に直らないため即通知。
  # それ以外の原因（API変更・ネットワーク等）も、連続 N 回失敗したら通知して
  # サイレント停止を防ぐ。
  # daily.sh 側のログローテーションで行数が減っていたらオフセットを先頭に戻す
  # （過大オフセットで空を読み、失効通知を空振りさせないため）
  CUR_LINES="$(wc -l < "$CRON_LOG" 2>/dev/null || echo 0)"
  [ "$CUR_LINES" -lt "$CRON_LOG_START" ] && CRON_LOG_START=0
  if tail -n +$((CRON_LOG_START + 1)) "$CRON_LOG" 2>/dev/null | grep -q "認証が失効"; then
    notify "mori の認証が失効しています。リポジトリ直下で .venv/bin/python mori_fetch.py --login を実行してください。"
  elif [ "$FAILS" -ge "$NOTIFY_AFTER_FAILURES" ]; then
    notify "mori の取得が ${FAILS} 回連続で失敗しています。logs/mori-cron.log を確認してください。"
  fi
fi

exit "$EXIT_CODE"
