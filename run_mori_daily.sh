#!/bin/bash
# 日次スケジューラ（macOS: launchd 経由の run_mori_catchup.sh / Linux: cron）から実行される本体。
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
# テストから差し替えられるように（既定はリポジトリ直下の .run.lock）
LOCK_FILE="${MORI_RUN_LOCK_FILE:-$SCRIPT_DIR/.run.lock}"
# 別プロセス実行中で何もしなかった時の終了コード（EX_TEMPFAIL。runlock.py の SKIP_EXIT と一致させる）。
# 0 を返すと catchup が「成功」と誤認してスタンプを書く（formal/CatchupStamp.tla の反例）。
SKIP_EXIT=75

mkdir -p "$LOG_DIR"

# 二重起動防止。ロックファイルを fd 9 で開き、flock(2) を非ブロッキングで取る（runlock.py）。
# ロックはこのシェルと子プロセスが fd 9 を開いている間だけ保持され、kill -9 や電源断でも
# OS が解放する。旧方式（mkdir + pid ファイル + 残骸回収）は回収手順そのものが競合して
# 二重起動し得た（formal/LockReclaim.tla の反例）ため、残骸回収という手順を持たない方式にした。
# ロックファイルは消さない（消すと別 inode への flock が並立する）。
if ! exec 9>>"$LOCK_FILE"; then
  echo "$(date '+%F %T') cannot open lock file $LOCK_FILE" >> "$LOG_FILE"
  exit 1
fi
"$PYTHON_BIN" "$SCRIPT_DIR/runlock.py" 9
LOCK_RC=$?
if [ "$LOCK_RC" -eq "$SKIP_EXIT" ]; then
  echo "$(date '+%F %T') another run in progress, skip" >> "$LOG_FILE"
  exit $SKIP_EXIT
elif [ "$LOCK_RC" -ne 0 ]; then
  echo "$(date '+%F %T') cannot take run lock (rc=$LOCK_RC)" >> "$LOG_FILE"
  exit 1
fi
# mori_fetch.py も同じロックを取る。取得済みの fd を伝えて、子が自分の親に弾かれないようにする
export MORI_RUN_LOCK_FILE="$LOCK_FILE"
export MORI_RUN_LOCK_FD=9

# 簡易ローテーション: 2MB を超えたら直近2000行だけ残す（ロック取得後なので競合しない）
if [ -f "$LOG_FILE" ] && [ "$(wc -c < "$LOG_FILE")" -gt 2097152 ]; then
  tail -2000 "$LOG_FILE" > "$LOG_FILE.tmp" && mv "$LOG_FILE.tmp" "$LOG_FILE"
fi

# 直近何日を取得済みでも再取得するか。mori の文字起こし遅延(公称最大7日)+1日が既定。
# 遅延がそれ以上になる環境では MORI_REFETCH_DAYS=14 のように上書きできる。
REFETCH_DAYS="${MORI_REFETCH_DAYS:-8}"

{
  echo "=== $(date '+%Y-%m-%d %H:%M:%S') START (backfill --refetch-recent $REFETCH_DAYS) ==="
  "$PYTHON_BIN" "$PY_SCRIPT" --refetch-recent "$REFETCH_DAYS"
  EXIT_CODE=$?
  echo "=== $(date '+%Y-%m-%d %H:%M:%S') END (exit=$EXIT_CODE) ==="
  echo ""
} >> "$LOG_FILE" 2>&1

exit ${EXIT_CODE:-1}
