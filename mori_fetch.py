"""mori (https://mori.to/) の会話 Transcript を Mori MCP 経由で日次保存するツール。

mori には公開 REST API がないため、公式 MCP サーバー https://mcp.mori.to を
MCP クライアントとして直接呼び出す。認証は OAuth 2.1（初回のみ --login で
ブラウザサインイン。以後は毎回起動時にリフレッシュトークンで自前更新する。
mcp SDK はトークン有効期限をプロセス内にしか保持せず、再起動後に期限切れ
アクセストークンを送って 401 → フル再認可に落ちるため、SDK 任せにしない）。

使い方:
    python mori_fetch.py --login          # 初回認証（ブラウザが開く）
    python mori_fetch.py --list-tools     # MCP ツール一覧とスキーマを表示
    python mori_fetch.py --date 2026-08-21
    python mori_fetch.py --days-ago 1     # 前日分（cron 用）
    python mori_fetch.py                  # 未取得日を自動バックフィル

終了コード（limitless_fetch.py と同じ規約）:
    0 = データあり保存成功 / 1 = API・認証エラー / 2 = データなし（空ファイルでスキップマーク）

※ 空ファイルのスキップマークは「全取得が成功してデータが無かった」場合のみ作る
   （セッション0件、または全セッションの本文が正当に空）。通信エラー等の異常は
   ファイルを作らず 1 で終了し、次回バックフィルで再試行される。
"""

import argparse
import asyncio
import http.server
import json
import os
import sys
import time
import urllib.parse
import webbrowser
from contextlib import AsyncExitStack
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import httpx2
from mcp import ClientSession
from mcp.client.auth import OAuthClientProvider
from mcp.client.streamable_http import streamable_http_client
from mcp.shared.auth import AuthorizationCodeResult, OAuthClientMetadata, OAuthToken

from auth_store import TOKENS_FILE, FileTokenStorage

MCP_URL = "https://mcp.mori.to"
TOKEN_ENDPOINT = "https://mcp.mori.to/oauth/token"
TIMEZONE = ZoneInfo("Asia/Tokyo")
CALLBACK_PORT = 8976
CALLBACK_PATH = "/callback"
LOGIN_TIMEOUT_SEC = 300
# 実際に使う読み取りスコープのみ要求する（最小権限）
SCOPES = "mori.sessions:read mori.transcripts:read"

# レート制限: transcript fetch 10回/分, list 60回/分
TRANSCRIPT_FETCH_INTERVAL_SEC = 7
LIST_PAGE_INTERVAL_SEC = 1
BACKFILL_DAY_INTERVAL_SEC = 7
LIST_PAGE_SIZE = 50

DATA_DIR = os.path.join(os.path.dirname(__file__), "data")
FILE_PREFIX = "mori_transcript_"

LOGIN_HINT = "認証が失効しています。`python mori_fetch.py --login` を実行してください。"


# ---------------------------------------------------------------------------
# OAuth
# ---------------------------------------------------------------------------


class AuthRequiredError(Exception):
    """保存済みトークンが無効で、ブラウザ再認証が必要な状態。"""


_last_refresh_monotonic: float | None = None

# アクセストークンは1時間有効。長時間実行ではこの間隔で再リフレッシュする
REFRESH_MARGIN_SEC = 40 * 60


async def _discover_token_endpoint(client: httpx2.AsyncClient) -> str:
    """OAuth サーバーメタデータからトークンエンドポイントを検出する。

    取得できない場合は既知の URL にフォールバックする。
    """
    try:
        resp = await client.get(f"{MCP_URL}/.well-known/oauth-authorization-server")
        if resp.status_code == 200:
            endpoint = resp.json().get("token_endpoint")
            if isinstance(endpoint, str) and endpoint.startswith("https://"):
                return endpoint
    except (httpx2.HTTPError, ValueError):
        pass
    return TOKEN_ENDPOINT


async def ensure_fresh_token(
    storage: FileTokenStorage | None = None,
    transport: "httpx2.AsyncBaseTransport | None" = None,
) -> None:
    """保存済みリフレッシュトークンでアクセストークンを更新して保存する。

    毎回の実行冒頭で呼ぶ。mcp SDK は保存トークンの有効期限を復元しないため、
    ここで必ず新しいアクセストークン（1時間有効）に差し替えてから接続する。
    リフレッシュトークンも更新されるたびに30日延命される。

    storage / transport はテスト用の注入ポイント（本番は既定値でよい）。
    """
    global _last_refresh_monotonic
    storage = storage or FileTokenStorage()
    tokens = await storage.get_tokens()
    client_info = await storage.get_client_info()
    if not tokens or not tokens.refresh_token or not client_info:
        raise AuthRequiredError(LOGIN_HINT)

    data = {
        "grant_type": "refresh_token",
        "refresh_token": tokens.refresh_token,
        "client_id": client_info.client_id,
        "resource": MCP_URL,
    }
    try:
        async with httpx2.AsyncClient(timeout=30, transport=transport) as client:
            endpoint = await _discover_token_endpoint(client)
            resp = await client.post(endpoint, data=data)
    except httpx2.HTTPError as e:
        raise RuntimeError(f"トークンエンドポイントに接続できません: {e}") from e

    if resp.status_code in (400, 401):
        # invalid_grant 等 — リフレッシュトークン自体が無効
        raise AuthRequiredError(f"トークン更新拒否 (HTTP {resp.status_code}): {resp.text[:200]} — {LOGIN_HINT}")
    if resp.status_code != 200:
        # 5xx 等の一時障害は認証失効ではない
        raise RuntimeError(f"トークンエンドポイント一時障害 (HTTP {resp.status_code}): {resp.text[:200]}")

    new_tokens = OAuthToken.model_validate(resp.json())
    if not new_tokens.refresh_token:
        # ローテーションされない実装への保険: 既存のリフレッシュトークンを維持
        new_tokens = new_tokens.model_copy(update={"refresh_token": tokens.refresh_token})
    await storage.set_tokens(new_tokens)
    _last_refresh_monotonic = time.monotonic()


async def refresh_if_stale() -> None:
    """アクセストークンの残り寿命が心許なければ再リフレッシュする（長時間実行用）。"""
    if _last_refresh_monotonic is None or time.monotonic() - _last_refresh_monotonic > REFRESH_MARGIN_SEC:
        await ensure_fresh_token()


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
            code = (params.get("code") or [None])[0]
            error = (params.get("error") or [None])[0]
            if code is None and error is None:
                # 認可レスポンス以外のアクセス（favicon 等）は無視して待ち続ける
                self.send_response(400)
                self.end_headers()
                return
            result["code"] = code
            result["error"] = error
            result["state"] = (params.get("state") or [None])[0]
            result["iss"] = (params.get("iss") or [None])[0]
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(
                "<html><body><h2>mori 認証が完了しました。このタブは閉じて構いません。</h2></body></html>".encode()
            )

        def log_message(self, *args) -> None:
            pass

    server = http.server.HTTPServer(("localhost", CALLBACK_PORT), Handler)
    server.timeout = 1
    deadline = time.monotonic() + LOGIN_TIMEOUT_SEC
    try:
        while not result:
            if time.monotonic() > deadline:
                raise RuntimeError(f"認証待ちが {LOGIN_TIMEOUT_SEC} 秒でタイムアウトしました。")
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
        client_name="mori-kikori",
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
            raise AuthRequiredError(LOGIN_HINT)

        async def callback_handler() -> AuthorizationCodeResult:
            raise AuthRequiredError(LOGIN_HINT)

    return OAuthClientProvider(
        server_url=MCP_URL,
        client_metadata=metadata,
        storage=FileTokenStorage(),
        redirect_handler=redirect_handler,
        callback_handler=callback_handler,
    )


def _find_auth_error(exc: BaseException) -> AuthRequiredError | None:
    """ExceptionGroup の入れ子から AuthRequiredError を掘り出す。"""
    if isinstance(exc, AuthRequiredError):
        return exc
    if isinstance(exc, BaseExceptionGroup):
        for sub in exc.exceptions:
            found = _find_auth_error(sub)
            if found:
                return found
    return None


# ---------------------------------------------------------------------------
# MCP セッション
# ---------------------------------------------------------------------------


class MoriClient:
    """Mori MCP への接続と、ツール呼び出しの薄いラッパー。"""

    def __init__(self, interactive: bool = False) -> None:
        self._interactive = interactive
        self._stack: AsyncExitStack | None = None
        self.session: ClientSession | None = None

    async def __aenter__(self) -> "MoriClient":
        self._stack = AsyncExitStack()
        try:
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
        except BaseException:
            await self._stack.aclose()
            raise
        return self

    async def __aexit__(self, *exc) -> None:
        await self._stack.aclose()

    async def call_tool_json(self, name: str, args: dict):
        """ツールを呼び、構造化データ（dict/list）を返す。JSON でない応答はエラー。"""
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
                except json.JSONDecodeError as e:
                    raise RuntimeError(f"MCP tool '{name}' returned non-JSON text: {text[:200]}") from e
        raise RuntimeError(f"MCP tool '{name}' returned empty content")


# ---------------------------------------------------------------------------
# データ取得・整形
# ---------------------------------------------------------------------------


def _today_jst() -> date:
    """Asia/Tokyo の今日。ホスト OS のタイムゾーンに依存しない。"""
    return datetime.now(TIMEZONE).date()


def _parse_iso(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        # naive はホスト TZ 解釈にせず UTC とみなす
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _fmt_time(ts: str | None) -> str:
    dt = _parse_iso(ts)
    if not dt:
        return "--:--:--"
    return dt.astimezone(TIMEZONE).strftime("%H:%M:%S")


def _session_date(session: dict) -> date | None:
    """セッションの開始時刻を Asia/Tokyo の日付に変換する。"""
    dt = _parse_iso(session.get("started_at") or session.get("start_time"))
    if not dt:
        return None
    return dt.astimezone(TIMEZONE).date()


def transcript_to_text(transcript: dict | list, title: str = "") -> str:
    """Transcript を「[HH:MM:SS] 話者: テキスト」形式に整形する。

    発話が1件もなければタイトルがあっても空文字を返す。タイトル行だけで
    「本文あり」と判定されると、発話ゼロの日が保存確定してしまうため。
    """
    if isinstance(transcript, dict):
        utterances = transcript.get("utterances") or []
    else:
        utterances = transcript

    lines: list[str] = []
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

    if not lines:
        return ""
    if title:
        lines.insert(0, f"# {title}")
    return "\n".join(lines)


async def list_sessions_for_date(client: MoriClient, target_day: date) -> list[dict]:
    """指定日のセッション一覧を取得する（offset ページング対応）。

    サーバー側の from/to がどのタイムゾーンの暦日で切られていても取りこぼさない
    よう前後1日を含めて取得し、クライアント側で Asia/Tokyo の開始日で絞る。
    開始時刻を解釈できないセッションは除外せず対象日に含める（無音の欠落より
    重複のほうが害が小さい）。
    """
    from_str = (target_day - timedelta(days=1)).isoformat()
    to_str = (target_day + timedelta(days=1)).isoformat()
    sessions: list[dict] = []
    offset = 0
    while True:
        data = await client.call_tool_json(
            "list_sessions",
            {"from": from_str, "to": to_str, "limit": LIST_PAGE_SIZE, "offset": offset},
        )
        if not isinstance(data, dict) or "sessions" not in data:
            # "sessions" キー欠落を0件成功と誤認すると空マークでデータを永久喪失する
            raise RuntimeError(f"list_sessions returned unexpected shape: {str(data)[:200]}")
        page = data["sessions"] or []
        if not isinstance(page, list):
            raise RuntimeError(f"list_sessions.sessions is not a list: {type(page).__name__}")
        sessions.extend(s for s in page if isinstance(s, dict))
        if len(page) < LIST_PAGE_SIZE:
            break
        offset += LIST_PAGE_SIZE
        await asyncio.sleep(LIST_PAGE_INTERVAL_SEC)

    selected = [s for s in sessions if _session_date(s) in (target_day, None)]
    # ISO文字列の辞書順はオフセット混在(+09:00とZ等)で狂うため、パース済み時刻で並べる
    epoch = datetime.fromtimestamp(0, tz=timezone.utc)
    selected.sort(key=lambda s: _parse_iso(s.get("started_at")) or epoch)
    return selected


def _transcript_uri(session: dict) -> str:
    """セッション ID から Transcript URI を導出する。生 UUID にも対応。"""
    session_id = str(session.get("id") or "")
    if session_id.startswith("mori://session/"):
        return session_id.replace("mori://session/", "mori://transcript/session/", 1)
    if session_id.startswith("mori://transcript/"):
        return session_id
    return f"mori://transcript/session/{session_id}"


async def fetch_transcript(client: MoriClient, session: dict) -> dict:
    """1セッションの Transcript を取得する。想定外のレスポンス形状はエラー。"""
    uri = _transcript_uri(session)
    data = await client.call_tool_json("fetch", {"uri": uri})
    if isinstance(data, dict):
        transcript = (data.get("object") or {}).get("transcript")
        if isinstance(transcript, dict):
            return transcript
    raise RuntimeError(f"fetch({uri}) returned unexpected shape (no object.transcript)")


def _atomic_write(path: str, content: str) -> None:
    """一時ファイル経由で書き込み、途中クラッシュでも壊れたファイルを残さない。

    tmp 名に PID を含め、手動実行と cron の同時書き込みでも衝突しない。
    """
    tmp_path = f"{path}.tmp{os.getpid()}"
    try:
        # 作成時点から 0600（chmod 前の一瞬でも他ユーザーに読ませない）
        fd = os.open(tmp_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(content)
        os.replace(tmp_path, path)
    finally:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)


async def download_single_date(target_day: date, data_dir: str) -> int:
    """指定日の全セッションの Transcript を取得して1ファイルに保存する。

    Returns:
        0 = データあり保存成功
        1 = API・認証エラー（ファイル変更なし・次回再試行）
        2 = 全取得が成功しデータなし — セッション0件または全セッション本文空（空ファイルでスキップマーク）
    """
    out_path = os.path.join(data_dir, f"{FILE_PREFIX}{target_day.isoformat()}.txt")
    print(f"Fetching mori sessions for {target_day} (Asia/Tokyo) ...")

    try:
        # 長時間バックフィルでもアクセストークン(1時間有効)が切れないよう随時更新
        await refresh_if_stale()
        async with MoriClient(interactive=False) as client:
            sessions = await list_sessions_for_date(client, target_day)
            if not sessions:
                if os.path.exists(out_path) and os.path.getsize(out_path) > 0:
                    # 再取得で0件になっても、既存の実データを空マークで潰さない
                    print(f"  → {target_day}: サーバーは0件だが既存データがあるため保持します。", file=sys.stderr)
                    return 0
                print(f"  → {target_day}: セッションはありませんでした。")
                _atomic_write(out_path, "")
                return 2
            texts: list[str] = []
            for i, session in enumerate(sessions):
                if not session.get("id"):
                    # id が無いセッションは取得手段がない。失敗扱いにすると
                    # その日が永久にリトライされ続けるため、警告して飛ばす
                    print(f"  → 警告: id のないセッションをスキップ: {session.get('title')!r}", file=sys.stderr)
                    continue
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
    except BaseExceptionGroup as eg:
        auth_err = _find_auth_error(eg)
        if auth_err:
            print(f"  → 認証エラー: {auth_err}", file=sys.stderr)
        else:
            print(f"  → API error for {target_day}: {eg!r}", file=sys.stderr)
        return 1
    except Exception as e:
        print(f"  → API error for {target_day}: {type(e).__name__}: {e}", file=sys.stderr)
        return 1

    result = "\n\n---\n\n".join(texts)
    if not result.strip():
        # ここに到達した時点で全 fetch は成功している（例外は上で rc=1 になる）。
        # 全セッションが正当に本文空だった日として、スキップマークを作る。
        if os.path.exists(out_path) and os.path.getsize(out_path) > 0:
            print(f"  → {target_day}: サーバーは本文なしだが既存データがあるため保持します。", file=sys.stderr)
            return 0
        print(f"  → {target_day}: セッション {len(sessions)} 件、いずれも本文なし。空マークを作成します。")
        _atomic_write(out_path, "")
        return 2

    _atomic_write(out_path, result)
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
    await ensure_fresh_token()
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


def _refresh_or_exit() -> None:
    """実行冒頭のトークン更新。失敗したら明確なメッセージで終了コード1。"""
    try:
        asyncio.run(ensure_fresh_token())
    except AuthRequiredError as e:
        print(f"認証エラー: {e}", file=sys.stderr)
        sys.exit(1)
    except Exception as e:
        print(f"トークン更新エラー: {type(e).__name__}: {e}", file=sys.stderr)
        sys.exit(1)


def main() -> None:
    parser = argparse.ArgumentParser(description="Mori MCP から指定日のトランスクリプトを取得")
    parser.add_argument("--login", action="store_true", help="初回 OAuth 認証（ブラウザが開く）")
    parser.add_argument("--list-tools", action="store_true", help="MCP ツール一覧とスキーマを表示")
    parser.add_argument("--date", type=str, help="取得する日付 (YYYY-MM-DD)")
    parser.add_argument("--days-ago", type=int, help="何日前のデータを取得するか (0=今日, 1=昨日)")
    parser.add_argument("--start-date", type=str, help="バックフィル開始日 (YYYY-MM-DD)")
    parser.add_argument("--end-date", type=str, help="バックフィル終了日 (YYYY-MM-DD、デフォルト: 昨日)")
    parser.add_argument("--days-back", type=int, default=30, help="バックフィル対象の過去日数 (デフォルト: 30)")
    parser.add_argument(
        "--refetch-recent",
        type=int,
        default=0,
        help="バックフィル後、直近N日を取得済みでも再取得して上書きする（文字起こし遅延の取り込み用）",
    )
    args = parser.parse_args()

    if args.login:
        sys.exit(asyncio.run(do_login()))
    if args.list_tools:
        sys.exit(asyncio.run(do_list_tools()))

    os.makedirs(DATA_DIR, exist_ok=True)
    today = _today_jst()

    if args.date:
        try:
            target = date.fromisoformat(args.date)
        except ValueError:
            print("エラー: 日付は YYYY-MM-DD 形式で指定してください。", file=sys.stderr)
            sys.exit(1)
        _refresh_or_exit()
        sys.exit(asyncio.run(download_single_date(target, DATA_DIR)))

    if args.days_ago is not None:
        if args.days_ago < 0:
            print("エラー: --days-ago は 0 以上を指定してください。", file=sys.stderr)
            sys.exit(1)
        target = today - timedelta(days=args.days_ago)
        _refresh_or_exit()
        sys.exit(asyncio.run(download_single_date(target, DATA_DIR)))

    # デフォルト: 未取得日の自動バックフィル（当日は対象外 — 記録が確定していないため）
    try:
        start = date.fromisoformat(args.start_date) if args.start_date else today - timedelta(days=args.days_back)
        end = date.fromisoformat(args.end_date) if args.end_date else today - timedelta(days=1)
    except ValueError:
        print("エラー: 日付は YYYY-MM-DD 形式で指定してください。", file=sys.stderr)
        sys.exit(1)

    missing = find_missing_dates(DATA_DIR, start, end)

    # mori は文字起こし完了まで最大7日かかることがあるため、直近N日は
    # 取得済みでも再取得して遅れて確定した発話を取り込む（--refetch-recent）
    refetch: list[date] = []
    if args.refetch_recent > 0:
        refetch = [end - timedelta(days=n) for n in range(args.refetch_recent) if end - timedelta(days=n) >= start]
        refetch = [d for d in refetch if d not in missing]

    targets = sorted(set(missing) | set(refetch))
    if not targets:
        print(f"{start} から {end} までの全日付が処理済みです（空マーク含む）。")
        return

    _refresh_or_exit()
    print(f"=== mori 自動ダウンロード開始: 未取得 {len(missing)} 件 / 再取得 {len(refetch)} 件 ===")
    failed: list[date] = []
    for i, target in enumerate(targets, 1):
        print(f"\n[{i}/{len(targets)}] {target} を処理中...")
        rc = asyncio.run(download_single_date(target, DATA_DIR))
        if rc == 1:
            failed.append(target)
        if i < len(targets):
            time.sleep(BACKFILL_DAY_INTERVAL_SEC)

    if failed:
        print(f"失敗した日付: {', '.join(str(d) for d in failed)}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
