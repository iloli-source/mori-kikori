"""ensure_fresh_token / auth_store / _atomic_write のユニットテスト。"""

import asyncio
import json
import os
import stat
import sys
import time

import httpx2
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import mori_fetch  # noqa: E402
from auth_store import FileTokenStorage  # noqa: E402
from mori_fetch import AuthRequiredError, _atomic_write, ensure_fresh_token  # noqa: E402

CLIENT_INFO = {
    "client_id": "test-client",
    "redirect_uris": ["http://localhost:8976/callback"],
    "token_endpoint_auth_method": "none",
}
OLD_TOKENS = {"access_token": "old-access", "token_type": "Bearer", "expires_in": 3600, "refresh_token": "old-refresh"}


def _seed_storage(tmp_path, obtained_at: float | None = None) -> FileTokenStorage:
    data = {"tokens": OLD_TOKENS, "client_info": CLIENT_INFO}
    if obtained_at is not None:
        data["obtained_at"] = obtained_at
    path = tmp_path / "tokens.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return FileTokenStorage(str(path))


def _transport(token_status: int, token_body: dict | str = "", discovery_status: int = 404):
    """.well-known とトークンエンドポイントを演じる MockTransport。"""
    calls = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        calls.append((request.method, str(request.url)))
        if ".well-known" in str(request.url):
            return httpx2.Response(discovery_status, json={"token_endpoint": mori_fetch.TOKEN_ENDPOINT})
        if isinstance(token_body, dict):
            return httpx2.Response(token_status, json=token_body)
        return httpx2.Response(token_status, text=token_body)

    return httpx2.MockTransport(handler), calls


class TestEnsureFreshToken:
    def test_success_rotates_tokens_and_persists(self, tmp_path):
        # Arrange
        storage = _seed_storage(tmp_path)
        transport, _ = _transport(
            200, {"access_token": "new-access", "token_type": "Bearer", "expires_in": 3600, "refresh_token": "new-refresh"}
        )

        # Act
        asyncio.run(ensure_fresh_token(storage=storage, transport=transport))

        # Assert
        saved = json.loads((tmp_path / "tokens.json").read_text())
        assert saved["tokens"]["access_token"] == "new-access"
        assert saved["tokens"]["refresh_token"] == "new-refresh"
        assert saved["obtained_at"] == pytest.approx(time.time(), abs=10)

    def test_keeps_old_refresh_token_when_not_rotated(self, tmp_path):
        storage = _seed_storage(tmp_path)
        transport, _ = _transport(200, {"access_token": "new-access", "token_type": "Bearer", "expires_in": 3600})

        asyncio.run(ensure_fresh_token(storage=storage, transport=transport))

        saved = json.loads((tmp_path / "tokens.json").read_text())
        assert saved["tokens"]["refresh_token"] == "old-refresh"

    def test_400_raises_auth_required(self, tmp_path):
        storage = _seed_storage(tmp_path)
        transport, _ = _transport(400, {"error": "invalid_grant"})

        with pytest.raises(AuthRequiredError):
            asyncio.run(ensure_fresh_token(storage=storage, transport=transport))

    def test_503_is_transient_not_auth_error(self, tmp_path):
        storage = _seed_storage(tmp_path)
        transport, _ = _transport(503, "service unavailable")

        with pytest.raises(RuntimeError):
            asyncio.run(ensure_fresh_token(storage=storage, transport=transport))

    def test_missing_tokens_raises_auth_required(self, tmp_path):
        storage = FileTokenStorage(str(tmp_path / "tokens.json"))

        with pytest.raises(AuthRequiredError):
            asyncio.run(ensure_fresh_token(storage=storage, transport=httpx2.MockTransport(lambda r: httpx2.Response(500))))


class TestFileTokenStorage:
    def test_expires_in_reflects_remaining_lifetime(self, tmp_path):
        # Arrange: 1000 秒前に取得した 3600 秒トークン
        storage = _seed_storage(tmp_path, obtained_at=time.time() - 1000)

        # Act
        tokens = asyncio.run(storage.get_tokens())

        # Assert
        assert tokens.expires_in == pytest.approx(2600, abs=10)

    def test_expired_token_reports_zero(self, tmp_path):
        storage = _seed_storage(tmp_path, obtained_at=time.time() - 7200)
        tokens = asyncio.run(storage.get_tokens())
        assert tokens.expires_in == 0

    def test_corrupted_file_returns_none(self, tmp_path):
        path = tmp_path / "tokens.json"
        path.write_text("{not json", encoding="utf-8")
        assert asyncio.run(FileTokenStorage(str(path)).get_tokens()) is None


class TestAtomicWrite:
    def test_writes_content_with_0600_and_no_tmp_left(self, tmp_path):
        target = tmp_path / "out.txt"

        _atomic_write(str(target), "こんにちは")

        assert target.read_text(encoding="utf-8") == "こんにちは"
        assert stat.S_IMODE(target.stat().st_mode) == 0o600
        assert list(tmp_path.glob("*.tmp*")) == []
