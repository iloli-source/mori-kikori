#!/bin/bash
# macOS launchd への登録を安全に行うインストーラ。
# clone 先がどこでも（パスに & や | や空白を含んでも）壊れないよう、
# sed ではなく python の文字列置換で plist を生成し、登録後に検証まで行う。
# 再実行すると既存登録を解除してから登録し直す（更新にも使える）。

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
LABEL="com.iloli.mori-kikori"
PLIST_DEST="$HOME/Library/LaunchAgents/$LABEL.plist"

if [ ! -x "$SCRIPT_DIR/.venv/bin/python3" ]; then
  echo "エラー: $SCRIPT_DIR/.venv がありません。先に README のセットアップ（venv 作成〜 --login）を済ませてください。" >&2
  exit 1
fi
if [ ! -f "$SCRIPT_DIR/tokens.json" ]; then
  echo "エラー: tokens.json がありません。先に .venv/bin/python mori_fetch.py --login で認証してください。" >&2
  exit 1
fi

mkdir -p "$HOME/Library/LaunchAgents" "$SCRIPT_DIR/logs"

REPO_DIR="$SCRIPT_DIR" DEST="$PLIST_DEST" python3 - <<'EOF'
import os
repo = os.environ["REPO_DIR"]
src = open(os.path.join(repo, "com.iloli.mori-kikori.plist.example"), encoding="utf-8").read()
out = src.replace("/path/to/mori-kikori", repo)
assert "/path/to/mori-kikori" not in out
open(os.environ["DEST"], "w", encoding="utf-8").write(out)
EOF

plutil -lint "$PLIST_DEST" >/dev/null

# 既存登録があれば解除してから登録（未登録なら bootout は無害に失敗する）
launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST_DEST"
launchctl print "gui/$(id -u)/$LABEL" >/dev/null

echo "✅ 登録完了: ${LABEL}（毎日 0:15 + ログイン時 + 毎時リトライ）"
echo
echo "RunAtLoad により初回実行がいま始まっています。未取得の過去分が多いと数十分かかります。"
echo "  進捗:     tail -f \"$SCRIPT_DIR/logs/mori-cron.log\""
echo "  完了確認: cat \"$SCRIPT_DIR/logs/.last-success-date\"  ← 今日の日付になれば成功"
echo "  解除:     launchctl bootout gui/\$(id -u)/${LABEL}"
