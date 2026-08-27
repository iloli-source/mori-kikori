"""download_single_date のデータ安全性分岐の統合テスト（MoriClient をスタブ化）。"""

import asyncio
import os
import sys
from datetime import date

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import mori_fetch  # noqa: E402
from mori_fetch import download_single_date  # noqa: E402

DAY = date(2026, 8, 21)
SESSION = {
    "id": "mori://session/abc",
    "started_at": "2026-08-21T10:00:00+09:00",
    "title": "テスト会話",
}
TRANSCRIPT = {
    "object": {
        "transcript": {
            "title": "テスト会話",
            "utterances": [{"started_at": "2026-08-21T10:00:00+09:00", "text": "こんにちは"}],
        }
    }
}


class StubClient:
    """MoriClient の代役。list_sessions / fetch の応答を注入する。"""

    def __init__(self, list_response=None, fetch_response=None, fetch_error=None):
        self.list_response = list_response
        self.fetch_response = fetch_response
        self.fetch_error = fetch_error

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        pass

    async def call_tool_json(self, name, args):
        if name == "list_sessions":
            return self.list_response
        if name == "fetch":
            if self.fetch_error:
                raise self.fetch_error
            return self.fetch_response
        raise AssertionError(f"unexpected tool: {name}")


@pytest.fixture
def stubbed(monkeypatch):
    """MoriClient と refresh_if_stale を差し替え、sleep を殺すフィクスチャ。"""
    holder = {}

    def apply(client: StubClient):
        monkeypatch.setattr(mori_fetch, "MoriClient", lambda interactive=False: client)

        async def no_refresh():
            pass

        monkeypatch.setattr(mori_fetch, "refresh_if_stale", no_refresh)
        monkeypatch.setattr(mori_fetch, "TRANSCRIPT_FETCH_INTERVAL_SEC", 0)
        holder["client"] = client
        return client

    return apply


def _out(tmp_path):
    return tmp_path / f"mori_transcript_{DAY.isoformat()}.txt"


class TestEmptyMarkerSafety:
    def test_no_sessions_creates_empty_marker(self, tmp_path, stubbed):
        stubbed(StubClient(list_response={"sessions": []}))

        rc = asyncio.run(download_single_date(DAY, str(tmp_path)))

        assert rc == 2
        assert _out(tmp_path).exists() and _out(tmp_path).stat().st_size == 0

    def test_no_sessions_never_overwrites_existing_data(self, tmp_path, stubbed):
        _out(tmp_path).write_text("既存の実データ", encoding="utf-8")
        stubbed(StubClient(list_response={"sessions": []}))

        rc = asyncio.run(download_single_date(DAY, str(tmp_path)))

        assert rc == 0
        assert _out(tmp_path).read_text(encoding="utf-8") == "既存の実データ"

    def test_all_empty_transcripts_preserve_existing_data(self, tmp_path, stubbed):
        _out(tmp_path).write_text("既存の実データ", encoding="utf-8")
        empty = {"object": {"transcript": {"title": "T", "utterances": []}}}
        stubbed(StubClient(list_response={"sessions": [SESSION]}, fetch_response=empty))

        rc = asyncio.run(download_single_date(DAY, str(tmp_path)))

        assert rc == 0
        assert _out(tmp_path).read_text(encoding="utf-8") == "既存の実データ"

    def test_all_empty_transcripts_without_existing_marks_done(self, tmp_path, stubbed):
        empty = {"object": {"transcript": {"title": "T", "utterances": []}}}
        stubbed(StubClient(list_response={"sessions": [SESSION]}, fetch_response=empty))

        rc = asyncio.run(download_single_date(DAY, str(tmp_path)))

        assert rc == 2
        assert _out(tmp_path).stat().st_size == 0


class TestStrictShapes:
    def test_missing_sessions_key_is_error_not_empty_success(self, tmp_path, stubbed):
        stubbed(StubClient(list_response={}))

        rc = asyncio.run(download_single_date(DAY, str(tmp_path)))

        assert rc == 1
        assert not _out(tmp_path).exists()

    def test_null_sessions_is_error(self, tmp_path, stubbed):
        stubbed(StubClient(list_response={"sessions": None}))

        rc = asyncio.run(download_single_date(DAY, str(tmp_path)))

        assert rc == 1
        assert not _out(tmp_path).exists()

    def test_session_without_id_fails_day(self, tmp_path, stubbed):
        bad = {"started_at": "2026-08-21T10:00:00+09:00", "title": "idなし"}
        stubbed(StubClient(list_response={"sessions": [bad]}))

        rc = asyncio.run(download_single_date(DAY, str(tmp_path)))

        assert rc == 1
        assert not _out(tmp_path).exists()


class TestFetchOutcomes:
    def test_successful_fetch_writes_formatted_file(self, tmp_path, stubbed):
        stubbed(StubClient(list_response={"sessions": [SESSION]}, fetch_response=TRANSCRIPT))

        rc = asyncio.run(download_single_date(DAY, str(tmp_path)))

        assert rc == 0
        assert _out(tmp_path).read_text(encoding="utf-8") == "# テスト会話\n[10:00:00] こんにちは"

    def test_fetch_error_leaves_no_file(self, tmp_path, stubbed):
        stubbed(StubClient(list_response={"sessions": [SESSION]}, fetch_error=RuntimeError("boom")))

        rc = asyncio.run(download_single_date(DAY, str(tmp_path)))

        assert rc == 1
        assert not _out(tmp_path).exists()


class TestShrinkGuard:
    def test_smaller_refetch_does_not_overwrite_larger_existing(self, tmp_path, stubbed):
        # Arrange: 既存は大きな完全データ
        big = "# 完全データ\n" + "[10:00:00] 発話\n" * 100
        _out(tmp_path).write_text(big, encoding="utf-8")
        stubbed(StubClient(list_response={"sessions": [SESSION]}, fetch_response=TRANSCRIPT))

        # Act: 再取得は小さな部分データしか返さない
        rc = asyncio.run(download_single_date(DAY, str(tmp_path)))

        # Assert: 既存を保持
        assert rc == 0
        assert _out(tmp_path).read_text(encoding="utf-8") == big

    def test_larger_refetch_overwrites(self, tmp_path, stubbed):
        _out(tmp_path).write_text("小", encoding="utf-8")
        stubbed(StubClient(list_response={"sessions": [SESSION]}, fetch_response=TRANSCRIPT))

        rc = asyncio.run(download_single_date(DAY, str(tmp_path)))

        assert rc == 0
        assert "こんにちは" in _out(tmp_path).read_text(encoding="utf-8")


class TestDownloadWithRetry:
    def test_transient_failure_then_success(self, tmp_path, stubbed, monkeypatch):
        from mori_fetch import download_with_retry

        calls = {"n": 0}

        class FlakyClient(StubClient):
            async def call_tool_json(self, name, args):
                if name == "list_sessions":
                    calls["n"] += 1
                    if calls["n"] == 1:
                        raise RuntimeError("Server disconnected")
                    return {"sessions": [SESSION]}
                return TRANSCRIPT

        stubbed(FlakyClient())

        rc = asyncio.run(download_with_retry(DAY, str(tmp_path), retries=1, wait_sec=0))

        assert rc == 0
        assert _out(tmp_path).exists()

    def test_persistent_failure_returns_1(self, tmp_path, stubbed):
        from mori_fetch import download_with_retry

        stubbed(StubClient(list_response={"sessions": [SESSION]}, fetch_error=RuntimeError("down")))

        rc = asyncio.run(download_with_retry(DAY, str(tmp_path), retries=1, wait_sec=0))

        assert rc == 1
        assert not _out(tmp_path).exists()

    def test_default_retries_is_two(self, tmp_path, stubbed):
        from mori_fetch import download_with_retry

        calls = {"n": 0}

        class AlwaysFail(StubClient):
            async def call_tool_json(self, name, args):
                calls["n"] += 1
                raise RuntimeError("down")

        stubbed(AlwaysFail())

        rc = asyncio.run(download_with_retry(DAY, str(tmp_path), wait_sec=0))

        # 初回 + 既定リトライ2回 = list_sessions 呼び出し3回
        assert rc == 1
        assert calls["n"] == 3


class TestNonDictSessionEntries:
    def test_none_entry_is_error_not_empty_success(self, tmp_path, stubbed):
        stubbed(StubClient(list_response={"sessions": [None]}))

        rc = asyncio.run(download_single_date(DAY, str(tmp_path)))

        assert rc == 1
        assert not _out(tmp_path).exists()
