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
- アクセストークン 1 時間 / リフレッシュトークン 30 日。**リフレッシュ成功のたびに新しい 30 日トークンが発行される**ため、毎日 cron が動いていれば再ログイン不要
- Mori MCP は読み取り専用（レコードの追加・編集・削除は不可能）

## セットアップ

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

# 初回のみ: ブラウザで mori にサインイン
.venv/bin/python mori_fetch.py --login
```

## 使い方

```bash
.venv/bin/python mori_fetch.py --days-ago 1        # 前日分（cron が毎日実行）
.venv/bin/python mori_fetch.py --date 2026-08-21   # 指定日
.venv/bin/python mori_fetch.py                     # 未取得日を自動バックフィル（過去30日）
.venv/bin/python mori_fetch.py --list-tools        # MCP ツールスキーマ表示（デバッグ用）
```

保存先: `data/mori_transcript_YYYY-MM-DD.txt`

```
# セッションタイトル
[HH:MM:SS] 話者名: 発言テキスト
...

---

# 次のセッション
...
```

終了コード: `0` = 保存成功 / `1` = API・認証エラー（ファイル変更なし）/ `2` = データなし（0 バイトファイル作成でスキップマーク）

## cron

```
5 0 * * * ~/mori-kikori/run_mori_daily.sh
```

毎日 0:05 に前日分を取得する（パスは clone 先に合わせて変更）。
ログ: `logs/mori-cron.log`

## トラブルシューティング

- **「認証が失効しています」エラー**: 30 日以上実行されなかった等でリフレッシュトークンが失効。
  `.venv/bin/python mori_fetch.py --login` で再サインインする
- **レート制限**: Transcript fetch は 10 回/分のため、セッション間に 7 秒待機している。
  1 日のセッション数が多いと数分かかるのは正常
- **テスト**: `.venv/bin/python -m pytest tests/ -q`
