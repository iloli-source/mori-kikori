"""全テスト共通: 実行ロックをテスト用の一時ファイルへ向け、リポジトリ直下の .run.lock に触れない。"""

import pytest


@pytest.fixture(autouse=True)
def isolated_run_lock(tmp_path, monkeypatch):
    monkeypatch.setenv("MORI_RUN_LOCK_FILE", str(tmp_path / "test-run.lock"))
    monkeypatch.delenv("MORI_RUN_LOCK_FD", raising=False)
