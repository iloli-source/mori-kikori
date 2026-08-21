"""mori (https://mori.to/) の会話 Transcript を Mori MCP 経由で日次保存するツール。

mori には公開 REST API がないため、公式 MCP サーバー https://mcp.mori.to を
MCP クライアントとして直接呼び出す。認証は OAuth 2.1（初回のみ --login で
ブラウザサインイン。以後はリフレッシュトークンで自動更新）。

使い方:
    python mori_fetch.py --login          # 初回認証（ブラウザが開く）
    python mori_fetch.py --list-tools     # MCP ツール一覧とスキーマを表示
    python mori_fetch.py --date 2026-08-21
    python mori_fetch.py --days-ago 1     # 前日分（cron 用）
    python mori_fetch.py                  # 未取得日を自動バックフィル

終了コード（limitless_fetch.py と同じ規約）:
    0 = データあり保存成功 / 1 = API・認証エラー / 2 = API成功・データなし
"""

import argparse
import asyncio
import http.server
from contextlib import AsyncExitStack
import json
import os
import sys
import threading
import urllib.parse
import webbrowser
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import httpx2
from mcp import ClientSession
from mcp.client.auth import OAuthClientProvider
from mcp.client.streamable_http import streamable_http_client
from mcp.shared.auth import AuthorizationCodeResult, OAuthClientMetadata

from auth_store import TOKENS_FILE, FileTokenStorage

MCP_URL = "https://mcp.mori.to"
TIMEZONE = ZoneInfo("Asia/Tokyo")
CALLBACK_PORT = 8976
CALLBACK_PATH = "/callback"
SCOPES = "mori.sessions:read mori.journals:read mori.transcripts:read mori.search:read"

# Transcript fetch は 10回/分 制限のため、呼び出し間隔を空ける
TRANSCRIPT_FETCH_INTERVAL_SEC = 7

DATA_DIR = os.path.join(os.path.dirname(__file__), "data")
FILE_PREFIX = "mori_transcript_"


# ---------------------------------------------------------------------------
# OAuth
# ---------------------------------------------------------------------------


class AuthRequiredError(Exception):
    """保存済みトークンが無効で、ブラウザ再認証が必要な状態。"""


def _run_callback_server() -> AuthorizationCodeResult:
    """localhost でリダイレクトを1回だけ受けて認可コードを返す。"""
    result: dict = {}

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            parsed = urllib.parse.urlparse(self.path)
            if parsed.path != CALLBACK_PATH:
                self.send_response(404)
                self.end_headers()
                return
            params = urllib.parse.parse_qs(parsed.query)
            result["code"] = (params.get("code") or [None])[0]
            result["state"] = (params.get("state") or [None])[0]
            result["iss"] = (params.get("iss") or [None])[0]
            result["error"] = (params.get("error") or [None])[0]
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(
                "<html><body><h2>mori 認証が完了しました。このタブは閉じて構いません。</h2></body></html>".encode()
            )

        def log_message(self, *args) -> None:
            pass

    server = http.server.HTTPServer(("localhost", CALLBACK_PORT), Handler)
    try:
        while "code" not in result and "error" not in result:
            server.handle_request()
    finally:
        server.server_close()

    if result.get("error"):
        raise RuntimeError(f"認可エラー: {result['error']}")
    if not result.get("code"):
        raise RuntimeError("認可コードを受け取れませんでした。")
    return AuthorizationCodeResult(code=result["code"], state=result.get("state"), iss=result.get("iss"))


def build_oauth_provider(interactive: bool) -> OAuthClientProvider:
    """OAuth プロバイダを構築する。

    interactive=True: ブラウザを開いてサインイン（--login 用）
    interactive=False: 保存済みトークンのみ使用。切れていたら AuthRequiredError
    """
    metadata = OAuthClientMetadata(
        redirect_uris=[f"http://localhost:{CALLBACK_PORT}{CALLBACK_PATH}"],
        token_endpoint_auth_method="none",
        grant_types=["authorization_code", "refresh_token"],
        response_types=["code"],
        client_name="mori-daily-fetch",
        scope=SCOPES,
    )

    if interactive:

        async def redirect_handler(url: str) -> None:
            print(f"ブラウザで mori にサインインしてください:\n{url}", flush=True)
            webbrowser.open(url)

        async def callback_handler() -> AuthorizationCodeResult:
            loop = asyncio.get_running_loop()
            return await loop.run_in_executor(None, _run_callback_server)

    else:

        async def redirect_handler(url: str) -> None:
            raise AuthRequiredError("認証が失効しています。`python mori_fetch.py --login` を実行してください。")

        async def callback_handler() -> AuthorizationCodeResult:
            raise AuthRequiredError("認証が失効しています。`python mori_fetch.py --login` を実行してください。")

    return OAuthClientProvider(
        server_url=MCP_URL,
        client_metadata=metadata,
        storage=FileTokenStorage(),
        redirect_handler=redirect_handler,
        callback_handler=callback_handler,
    )


# ---------------------------------------------------------------------------
# MCP セッション
# ---------------------------------------------------------------------------


class MoriClient:
    """Mori MCP への接続と、ツール呼び出しの薄いラッパー。"""

    def __init__(self, interactive: bool = False) -> None:
        self._interactive = interactive
        self._stack = None
        self.session: ClientSession | None = None

    async def __aenter__(self) -> "MoriClient":
        self._stack = AsyncExitStack()
        auth = build_oauth_provider(self._interactive)
        http_client = httpx2.AsyncClient(
            auth=auth,
            follow_redirects=True,
            timeout=httpx2.Timeout(30, read=300),
        )
        await self._stack.enter_async_context(http_client)
        read, write = await self._stack.enter_async_context(
            streamable_http_client(MCP_URL, http_client=http_client)
        )
        self.session = await self._stack.enter_async_context(ClientSession(read, write))
        await self.session.initialize()
        return self

    async def __aexit__(self, *exc) -> None:
        await self._stack.aclose()

    async def call_tool_json(self, name: str, args: dict):
        """ツールを呼び、構造化データ（dict/list）を返す。"""
        result = await self.session.call_tool(name, args)
        if result.is_error:
            texts = [c.text for c in result.content if getattr(c, "text", None)]
            raise RuntimeError(f"MCP tool '{name}' error: {' / '.join(texts) or 'unknown'}")
        if result.structured_content is not None:
            return result.structured_content
        for c in result.content:
            text = getattr(c, "text", None)
            if text:
                try:
                    return json.loads(text)
                except json.JSONDecodeError:
                    return text
        return None


# ---------------------------------------------------------------------------
# データ取得・整形
# ---------------------------------------------------------------------------


def _parse_iso(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None


def _fmt_time(ts: str | None) -> str:
    dt = _parse_iso(ts)
    if not dt:
        return ""
    return dt.astimezone(TIMEZONE).strftime("%H:%M:%S")


def _session_date(session: dict) -> date | None:
    """セッションの開始時刻を Asia/Tokyo の日付に変換する。"""
    dt = _parse_iso(session.get("started_at") or session.get("start_time"))
    if not dt:
        return None
    return dt.astimezone(TIMEZONE).date()


def transcript_to_text(transcript: dict | list, title: str = "") -> str:
    """Transcript を「[HH:MM:SS] 話者: テキスト」形式に整形する。"""
    lines: list[str] = []
    if title:
        lines.append(f"# {title}")

    if isinstance(transcript, dict):
        utterances = transcript.get("utterances") or []
    else:
        utterances = transcript

    for u in utterances:
        if not isinstance(u, dict):
            continue
        text = (u.get("text") or "").strip()
        if not text:
            continue
        ts = _fmt_time(u.get("started_at") or u.get("start_time"))
        speaker = u.get("speaker_name") or u.get("speaker") or ""
        prefix = f"{speaker}: " if speaker else ""
        lines.append(f"[{ts}] {prefix}{text}")
    return "\n".join(lines)


LIST_PAGE_SIZE = 50


async def list_sessions_for_date(client: MoriClient, target_day: date) -> list[dict]:
    """指定日のセッション一覧を取得する（offset ページング対応）。"""
    day_str = target_day.isoformat()
    sessions: list[dict] = []
    offset = 0
    while True:
        data = await client.call_tool_json(
            "list_sessions",
            {"from": day_str, "to": day_str, "limit": LIST_PAGE_SIZE, "offset": offset},
        )
        page = data.get("sessions") or [] if isinstance(data, dict) else []
        sessions.extend(s for s in page if isinstance(s, dict))
        if len(page) < LIST_PAGE_SIZE:
            break
        offset += LIST_PAGE_SIZE
    # サーバー側の日付絞り込みに依存せず、クライアント側でも必ず絞る
    sessions = [s for s in sessions if _session_date(s) == target_day]
    sessions.sort(key=lambda s: s.get("started_at") or "")
    return sessions


def _transcript_uri(session: dict) -> str:
    """セッション URI (mori://session/<id>) から Transcript URI を導出する。"""
    session_uri = session.get("id") or ""
    return session_uri.replace("mori://session/", "mori://transcript/session/", 1)


async def fetch_transcript(client: MoriClient, session: dict) -> dict:
    """1セッションの Transcript を取得する。"""
    data = await client.call_tool_json("fetch", {"uri": _transcript_uri(session)})
    if isinstance(data, dict):
        obj = data.get("object") or {}
        return obj.get("transcript") or obj or data
    return {}


async def download_single_date(target_day: date, data_dir: str) -> int:
    """指定日の全セッションの Transcript を取得して1ファイルに保存する。

    Returns:
        0 = データあり保存成功 / 1 = API失敗（ファイル変更なし） / 2 = データなし（0byteファイル作成）
    """
    out_path = os.path.join(data_dir, f"{FILE_PREFIX}{target_day.isoformat()}.txt")
    print(f"Fetching mori sessions for {target_day} (Asia/Tokyo) ...")

    try:
        async with MoriClient(interactive=False) as client:
            sessions = await list_sessions_for_date(client, target_day)
            texts: list[str] = []
            for i, session in enumerate(sessions):
                title = session.get("title") or ""
                transcript = await fetch_transcript(client, session)
                text = transcript_to_text(transcript, title=title)
                if text.strip():
                    texts.append(text)
                if i < len(sessions) - 1:
                    await asyncio.sleep(TRANSCRIPT_FETCH_INTERVAL_SEC)
    except AuthRequiredError as e:
        print(f"  → 認証エラー: {e}", file=sys.stderr)
        return 1
    except Exception as e:
        print(f"  → API error for {target_day}: {type(e).__name__}: {e}", file=sys.stderr)
        return 1

    result = "\n\n---\n\n".join(texts)
    if not result.strip():
        print(f"  → {target_day}: トランスクリプトは見つかりませんでした。")
        with open(out_path, "w", encoding="utf-8") as f:
            f.write("")
        return 2

    with open(out_path, "w", encoding="utf-8") as f:
        f.write(result)
    print(f"  → 保存しました: {out_path}")
    return 0


def find_missing_dates(data_dir: str, start_date: date, end_date: date) -> list[date]:
    """指定範囲内で、まだダウンロードされていない日付を返す。"""
    missing = []
    current = start_date
    while current <= end_date:
        path = os.path.join(data_dir, f"{FILE_PREFIX}{current.isoformat()}.txt")
        if not os.path.exists(path):
            missing.append(current)
        current += timedelta(days=1)
    return missing


# ---------------------------------------------------------------------------
# 認証・デバッグ用サブコマンド
# ---------------------------------------------------------------------------


async def do_login() -> int:
    """ブラウザで OAuth 認証を行い、接続確認まで実施する。"""
    async with MoriClient(interactive=True) as client:
        tools = await client.session.list_tools()
        names = [t.name for t in tools.tools]
        print(f"認証成功。利用可能ツール: {', '.join(names)}")
        print(f"トークン保存先: {TOKENS_FILE}")
    return 0


async def do_list_tools() -> int:
    """ツール一覧と入力スキーマを表示する（デバッグ用）。"""
    async with MoriClient(interactive=False) as client:
        tools = await client.session.list_tools()
        for t in tools.tools:
            print(f"== {t.name} ==")
            print(f"  {t.description}")
            print(json.dumps(t.input_schema, ensure_ascii=False, indent=2))
    return 0


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(description="Mori MCP から指定日のトランスクリプトを取得")
    parser.add_argument("--login", action="store_true", help="初回 OAuth 認証（ブラウザが開く）")
    parser.add_argument("--list-tools", action="store_true", help="MCP ツール一覧とスキーマを表示")
    parser.add_argument("--date", type=str, help="取得する日付 (YYYY-MM-DD)")
    parser.add_argument("--days-ago", type=int, help="何日前のデータを取得するか (例: 1 = 昨日)")
    parser.add_argument("--start-date", type=str, help="バックフィル開始日 (YYYY-MM-DD)")
    parser.add_argument("--end-date", type=str, help="バックフィル終了日 (YYYY-MM-DD)")
    parser.add_argument("--days-back", type=int, default=30, help="バックフィル対象の過去日数 (デフォルト: 30)")
    args = parser.parse_args()

    if args.login:
        sys.exit(asyncio.run(do_login()))
    if args.list_tools:
        sys.exit(asyncio.run(do_list_tools()))

    os.makedirs(DATA_DIR, exist_ok=True)

    if args.date:
        try:
            target = date.fromisoformat(args.date)
        except ValueError:
            print("エラー: 日付は YYYY-MM-DD 形式で指定してください。", file=sys.stderr)
            sys.exit(1)
        sys.exit(asyncio.run(download_single_date(target, DATA_DIR)))

    if args.days_ago:
        target = date.today() - timedelta(days=args.days_ago)
        sys.exit(asyncio.run(download_single_date(target, DATA_DIR)))

    # デフォルト: 未取得日の自動バックフィル
    try:
        start = date.fromisoformat(args.start_date) if args.start_date else date.today() - timedelta(days=args.days_back)
        end = date.fromisoformat(args.end_date) if args.end_date else date.today()
    except ValueError:
        print("エラー: 日付は YYYY-MM-DD 形式で指定してください。", file=sys.stderr)
        sys.exit(1)

    missing = find_missing_dates(DATA_DIR, start, end)
    if not missing:
        print(f"{start} から {end} までの全データがダウンロード済みです。")
        return

    print(f"=== mori 自動ダウンロード開始: 未取得 {len(missing)} 件 ===")
    failed: list[date] = []
    for i, target in enumerate(missing, 1):
        print(f"\n[{i}/{len(missing)}] {target} を処理中...")
        rc = asyncio.run(download_single_date(target, DATA_DIR))
        if rc == 1:
            failed.append(target)

    if failed:
        print(f"失敗した日付: {', '.join(str(d) for d in failed)}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
