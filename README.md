# mori-kikori 🪓🌲

Unofficial daily transcript exporter for [Mori](https://mori.to/) journaling AI.
Fetches your conversation transcripts every day via the official Mori MCP server and saves them as plain text files — like a woodcutter (木こり *kikori*) harvesting logs from the forest (森 *mori*).

[mori](https://mori.to/)（ウェアラブル型ジャーナリング AI）の会話 Transcript を毎日自動でテキスト保存する非公式ツール。
名前の由来: 森（mori）からログを切り出す木こり（kikori）。

> **Disclaimer / 免責**: This is an unofficial community tool and is not affiliated with or endorsed by franky Inc. (the maker of Mori).
> 本ツールは franky Inc. とは無関係の非公式コミュニティツールです。

## 仕組み

mori には公開 REST API がない（API キー・PAT・webhook は「将来対応予定」のみ）。
そのため公式 MCP サーバー **https://mcp.mori.to** を MCP クライアントとして直接呼び出す。

- 認証: OAuth 2.1（PKCE + 動的クライアント登録）。トークンは `tokens.json`（chmod 600）に保存
- アクセストークン 1 時間 / リフレッシュトークン 30 日。**毎回の実行冒頭で自前でリフレッシュしてから接続する**（mcp SDK はトークン有効期限をプロセス内にしか保持せず、再起動後に期限切れトークンを送ってしまうため）。リフレッシュ成功のたびに 30 日延命されるので、cron が 30 日以内に一度でも動いていれば再ログイン不要
- Mori MCP は読み取り専用（レコードの追加・編集・削除は不可能）

## mori MCP の仕様（実測メモ）

公式ドキュメントが乏しいため、`--list-tools` と実運用で確認した仕様を記す（2026-08 時点。予告なく変わる可能性あり）。

- **エンドポイント**: `https://mcp.mori.to`（Streamable HTTP）。トークンエンドポイントは `https://mcp.mori.to/oauth/token`
- **認証**: OAuth 2.1（PKCE + 動的クライアント登録）。スコープは `mori.sessions:read mori.transcripts:read`。アクセストークン 1 時間 / リフレッシュトークン 30 日（リフレッシュ成功で 30 日延命）
- **提供ツール**: `search` / `fetch` / `list_sessions` / `list_journals`（すべて読み取り専用）
- **`list_sessions`**: 引数 `{from, to, limit, offset}`。`limit` は最大 50。ページネーションは `offset` で行う
- **`fetch`**: 引数 `{uri: "mori://transcript/session/<id>"}`。セッション ID `mori://session/<id>` の `session/` を `transcript/session/` に読み替えると transcript URI になる。レスポンスは `object.transcript.utterances[]` に発話が一括で入り、各発話は `{started_at, text, speaker_name?}`（`start_time` / `speaker` という別名で返るケースもある）
- **レート制限**: transcript fetch は約 10 回/分。本ツールはセッション間 7 秒・list ページ間 1 秒・バックフィル日次間 7 秒待機して回避している
- **文字起こしの遅延**: 録音がサーバー側で文字起こしされるまで**最大 7 日程度遅れる**ことがある。当日〜数日前の取得結果は不完全な可能性があるため、日次運用では `--refetch-recent 8` で直近分を再取得し続ける
- **公開 REST API / API キー / webhook**: 現時点では提供なし（「将来対応予定」のアナウンスのみ）

## セットアップ

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

# 初回のみ: ブラウザで mori にサインイン
.venv/bin/python mori_fetch.py --login
```

## 使い方

```bash
.venv/bin/python mori_fetch.py                     # 未取得日を自動バックフィル（過去30日〜昨日）
.venv/bin/python mori_fetch.py --date 2026-08-21   # 指定日のみ
.venv/bin/python mori_fetch.py --days-ago 1        # 前日分のみ（0=今日, 1=昨日）
.venv/bin/python mori_fetch.py --refetch-recent 8  # バックフィル + 直近8日は取得済みでも再取得
.venv/bin/python mori_fetch.py --list-tools        # MCP ツールスキーマ表示（デバッグ用）
```

| オプション | 説明 |
|---|---|
| （引数なし） | バックフィルモード。過去 `--days-back` 日（デフォルト 30）〜昨日のうち未取得日を古い順に取得 |
| `--date YYYY-MM-DD` | 指定日のみ取得 |
| `--days-ago N` | N 日前のみ取得（`0`=今日, `1`=昨日） |
| `--start-date` / `--end-date` | バックフィル範囲を明示指定（`--end-date` デフォルトは昨日） |
| `--days-back N` | バックフィル対象の過去日数（デフォルト 30） |
| `--refetch-recent N` | バックフィル後、直近 N 日を取得済みでも再取得して上書き（文字起こし遅延の取り込み用。日次運用では `8` を推奨） |
| `--login` | 初回 OAuth 認証（ブラウザが開く） |
| `--list-tools` | MCP ツール一覧とスキーマを表示 |

日付は常に **Asia/Tokyo** 基準（ホスト OS のタイムゾーンに依存しない）。
バックフィルは当日を対象にしない（その日の記録がまだ確定していないため）。

保存先: `data/mori_transcript_YYYY-MM-DD.txt`

```
# セッションタイトル
[HH:MM:SS] 話者名: 発言テキスト
...

---

# 次のセッション
...
```

終了コード: `0` = 保存成功 / `1` = API・認証エラー（ファイル変更なし・次回バックフィルで再試行）/ `2` = 全取得が成功しデータなし（0 バイトファイルでスキップマーク）

再取得時にサーバーが一時的に 0 件・縮小データを返しても、既存の実データを空マークや
より小さい内容で上書きすることはない（サイズ比較によるヒューリスティックなガード）。一時的な接続断は
バックオフ付きで最大 2 回（15 秒 → 45 秒）自動リトライし、それでも失敗した日は翌晩の
バックフィルで再試行される。

書き込みはアトミック（tmp → rename）で、途中クラッシュしても壊れたファイルは残らない。

## スケジュール実行

日次実行は `run_mori_daily.sh`（バックフィルモード）が担う。
単日取得ではなくバックフィルで動かすことで、**過去に失敗した日が翌日以降に自動で再試行される**。
さらに直近 8 日は取得済みでも再取得して上書きする（mori の文字起こしは最大 7 日遅れる
ことがあるため。遅延の末尾が深夜の実行タイミングを跨いでも拾えるよう +1 日の余裕を持たせている）。
ログ: `logs/mori-cron.log`。二重起動は PID 生存確認付きロックで防止。

### macOS: launchd（推奨）

cron は Mac がスリープ/電源断中に発火せず、起きた後も取りこぼし分を実行しない。
macOS では launchd + `run_mori_catchup.sh` を使うと「毎日 0:15」「PC を開いた時」「毎時リトライ」の
3 経路で起動し、その日まだ成功していない場合のみ日次実行が走る（成功済みの日はスタンプ
`logs/.last-success-date` により即スキップ）。失敗した日はスタンプが残らず、次の発火で自動再試行される。

```bash
# /path/to/mori-kikori を clone 先に置換してから配置
sed "s|/path/to/mori-kikori|$(pwd)|g" com.iloli.mori-kikori.plist.example \
  > ~/Library/LaunchAgents/com.iloli.mori-kikori.plist
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.iloli.mori-kikori.plist
```

解除は `launchctl bootout gui/$(id -u)/com.iloli.mori-kikori`。

### Linux 等: cron

```
5 0 * * * ~/mori-kikori/run_mori_daily.sh
```

毎日 0:05 (JST) にバックフィルモードで実行する（パスは clone 先に合わせて変更）。
常時起動のマシンであれば cron で十分。

## トラブルシューティング

- **「認証が失効しています」エラー**: 30 日以上実行されなかった等でリフレッシュトークンが失効。
  `.venv/bin/python mori_fetch.py --login` で再サインインする
- **レート制限**: Transcript fetch は 10 回/分のため、セッション間に 7 秒待機している。
  1 日のセッション数が多いと数分かかるのは正常
- **テスト**: `.venv/bin/python -m pytest tests/ -q`
