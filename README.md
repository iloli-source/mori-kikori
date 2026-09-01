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

前提:

- **Python 3.11 以上**（`BaseExceptionGroup` を使用しているため。`python3 --version` で確認。
  macOS 付属の `/usr/bin/python3` は古いことが多いので、満たさない場合は `brew install python` 等で導入する）
- 初回認証にはブラウザが必要（OAuth のリダイレクトを `localhost:8976` で受けるため、**同じマシンのブラウザ**でサインインする）。ヘッドレスサーバーに入れる場合は手元のPCから `ssh -L 8976:localhost:8976 <server>` でポートフォワードした上で `--login` を実行し、表示されるURLを手元のブラウザで開く

```bash
git clone https://github.com/iloli-source/mori-kikori.git
cd mori-kikori          # 以降のコマンドはすべてリポジトリ直下で実行する

python3 -c 'import sys; assert sys.version_info >= (3, 11), f"Python 3.11+ が必要: {sys.version}"'
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

# 初回のみ: ブラウザで mori にサインイン
.venv/bin/python mori_fetch.py --login

# 動作確認: まず昨日1日分だけ取得してみる（1日あたり1〜3分程度かかる）
.venv/bin/python mori_fetch.py --days-ago 1
ls -la data/            # mori_transcript_YYYY-MM-DD.txt ができていれば成功
```

昨日録音がなかった場合は 0 バイトのスキップマークが作られ、終了コード `2` で終わる（これも正常。
中身が入るのは録音があった日だけ）。

いきなり引数なしで実行すると過去30日のバックフィルが走り、レート制限回避の
待機（セッション間7秒）のため数十分かかることがある。まず1日分で疎通確認を推奨。

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
| `--list-tools` | MCP ツール一覧とスキーマを表示（要・認証済み） |

単日系（`--date` / `--days-ago`）とバックフィル系（`--start-date` / `--end-date` / `--days-back` / `--refetch-recent`）は排他で、併用するとエラーで拒否される。不正な範囲（開始日 > 終了日、`--days-back 0` 等）も黙って成功扱いにはならずエラーになる。

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

終了コード（**単日実行** `--date` / `--days-ago` のとき）: `0` = 保存成功 / `1` = API・認証エラー（ファイル変更なし・次回バックフィルで再試行）/ `2` = 全取得が成功しデータなし（0 バイトファイルでスキップマーク）

**バックフィル実行**（引数なし・`--refetch-recent` 等）の終了コードは `0` = 全日付処理完了（データなしの日はスキップマークを書いて成功扱い）/ `1` = 1日以上失敗、の2値。`2` は返らないので、日次ラッパーの監視は `0` / `非0` で判定する。

再取得時にサーバーが一時的に 0 件・縮小データを返しても、既存の実データを空マークや
より小さい内容で上書きすることはない（サイズ比較によるヒューリスティックなガード）。一時的な接続断は
バックオフ付きで最大 2 回（15 秒 → 45 秒）自動リトライし、それでも失敗した日は次回の
バックフィル（launchd 運用なら毎時リトライ、cron なら翌晩）で再試行される。

書き込みはアトミック（tmp → rename）で、途中クラッシュしても壊れたファイルは残らない。

## スケジュール実行

日次実行は `run_mori_daily.sh`（バックフィルモード）が担う。
単日取得ではなくバックフィルで動かすことで、**過去に失敗した日が翌日以降に自動で再試行される**。
さらに直近 8 日は取得済みでも再取得して上書きする（mori の文字起こしは最大 7 日遅れる
ことがあるため。遅延の末尾が深夜の実行タイミングを跨いでも拾えるよう +1 日の余裕を持たせている）。
ログ: `logs/mori-cron.log`。二重起動は PID 生存確認付きロックで防止。

### macOS: launchd（推奨）

cron は Mac がスリープ/電源断中に発火せず、起きた後も取りこぼし分を実行しない。
macOS では launchd + `run_mori_catchup.sh` を使うと次の 3 経路で起動する:

- 毎日 0:15（`StartCalendarInterval`。Mac のシステム時刻基準。スリープで跨いだ場合は起床直後に発火）
- ログイン時（`RunAtLoad`。電源断で 0:15 を逃した日のキャッチアップ）
- 1 時間ごと（`StartInterval`。当日失敗時の自動リトライ。スリープ中の分は発火しないが、上の2経路が拾う）

その日まだ成功していない場合のみ日次実行が走る（成功済みの日はスタンプ
`logs/.last-success-date` により即スキップ）。失敗した日はスタンプが残らず、次の発火で自動再試行される。

登録はインストーラ1発（plist 生成 → 構文検証 → 登録 → 登録確認まで自動。再実行すれば更新にもなる）:

```bash
./install_launchd.sh
```

> 注意:
> - `RunAtLoad` により **登録した瞬間に初回実行が始まる**。まだ取得していない過去分が多いと
>   初回は数十分かかる（進捗は `tail -f logs/mori-cron.log`）
> - LaunchAgent は **GUI ログインセッションが必要**。ログインしっぱなしにしない Mac mini 等の
>   常時稼働サーバーでは、launchd ではなく下記の cron を使う

**実際に取得まで成功したこと**の確認（登録成功≠実行成功。`catchup start` は開始しか意味しない）:

```bash
cat logs/.last-success-date        # 今日の日付になっていれば当日実行まで完了
tail -3 logs/mori-cron.log         # 「END (exit=0)」で終わっていれば取得成功
```

手動で発火させたい場合は `launchctl kickstart gui/$(id -u)/com.iloli.mori-kikori` のあと上記2つで確認する
（本日成功済みなら `logs/mori-catchup.log` に `already succeeded today — skip` が増える）。

解除は `launchctl bootout gui/$(id -u)/com.iloli.mori-kikori`。
手動で plist を配置したい場合は `com.iloli.mori-kikori.plist.example` のコメントを参照
（`/path/to/mori-kikori` の置換に sed を使う場合、パスに `&` や `|` が含まれると壊れる点に注意。
インストーラは python 置換なのでこの問題がない）。

### Linux 等: cron

```
5 0 * * * /absolute/path/to/mori-kikori/run_mori_daily.sh
```

常時起動のマシンであれば cron で十分。注意点:

- **cron の発火時刻はホストのタイムゾーン基準**。JST 0:05 に動かしたい場合、ホストが UTC なら `CRON_TZ=Asia/Tokyo` を crontab の先頭に書くか、`5 15 * * *`（UTC）と読み替える。取得対象日の計算自体はスクリプト内で常に JST なのでズレない
- パスは `~` に頼らず絶対パスで書く（cron の `~` 展開はシェル依存）
- 初回の `--login` はブラウザが必要（ヘッドレスサーバーの場合は上記セットアップのポートフォワード手順を参照）

## 動作確認（動いているか不安になったら）

| 見る場所 | 内容 |
|---|---|
| `data/` | 取得結果本体。`mori_transcript_昨日.txt` があれば正常（0 バイトは「録音なしの日」のマーク） |
| `logs/.last-success-date` | launchd キャッチアップの最終成功日（JST）。今日の日付なら「今日の実行（＝昨日までの取り込み）」は完了済み。cron 運用ではこのファイルは作られない |
| `logs/mori-cron.log` | 取得処理の本体ログ（何日分を取得し、失敗が何か） |
| `logs/mori-catchup.log` | launchd キャッチアップの判定ログ。成功済みの日も毎時 `already succeeded today — skip` が1行残るので、**この行が毎時増えていれば launchd は生きている** |
| `logs/mori-launchd.log` | launchd が捕まえた標準出力/エラー（通常は空。ここに Python の traceback が出ていたら環境異常） |

認証失効すると `logs/mori-cron.log` に「認証が失効しています」が記録され続け、launchd 運用の macOS では**デスクトップ通知**でも知らせる。数日 `data/` が増えていないと気づいたら、まず上の表の順にログを見る。

## トラブルシューティング

- **「認証が失効しています」エラー**: 30 日以上実行されなかった等でリフレッシュトークンが失効。
  リポジトリ直下で `.venv/bin/python mori_fetch.py --login` を実行して再サインインする
- **30 日を超えて止まっていた場合**: 再ログイン後、デフォルトのバックフィルは過去 30 日分しか
  見ないため、それより前の欠落日は `--days-back 90` や `--start-date 2026-06-01` のように
  範囲を広げて一度手動実行すれば回収できる
- **8 日以上遅れて文字起こしが確定した場合**: 自動再取得の窓（既定は直近 8 日 = 公称最大遅延
  7 日 + 1 日）を過ぎた日は空マークのままになる。気づいたら `--date YYYY-MM-DD` で個別に
  再取得すれば取り込める（既存の実データをより小さい内容で上書きすることはない）。
  遅延が常態的に長い場合は環境変数 `MORI_REFETCH_DAYS=14` のように窓自体を広げられる
- **launchd に登録したのに何も起きない**: 上記「動作確認」の順に確認。`launchctl print` で
  登録が見えない場合は `./install_launchd.sh` をやり直す。Python の traceback や
  venv 未作成のエラーは `logs/mori-cron.log` に出る（`logs/mori-launchd.log` は
  スクリプト自体が起動できなかった場合にだけ内容が入る）
- **`--login` が「接続拒否」やタイムアウトで失敗する**: ポート `8976` を別プロセスが使っていないか
  確認（`lsof -i :8976`）し、ブラウザのタブを閉じてから `--login` をやり直す。認可画面の応答が
  速すぎて一時的に競合することがあるが、再実行で通る
- **レート制限**: Transcript fetch は 10 回/分のため、セッション間に 7 秒待機している。
  1 日のセッション数が多いと数分かかるのは正常
- **テスト**: `.venv/bin/python -m pytest tests/ -q`
