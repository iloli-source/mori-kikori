"""formal/RefetchSettle.tla の回帰テスト。

文字起こしは最大7日遅れて確定する。修正前は「実行日から見て直近 N 日」しか再取得しないため、
確定前に取得（空マーク／部分）したあと N 日以上実行が成功しないと、その日は二度と再取得されなかった。
"""

import asyncio
import os
import sys
import time
from datetime import date, datetime, timedelta

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import mori_fetch  # noqa: E402
from mori_fetch import TIMEZONE, find_unsettled_dates  # noqa: E402

TODAY = date(2026, 9, 30)


def _write(data_dir, day: date, fetched_on: date, content: str = "") -> str:
    path = os.path.join(str(data_dir), f"mori_transcript_{day.isoformat()}.txt")
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)
    ts = datetime(fetched_on.year, fetched_on.month, fetched_on.day, 3, 0, tzinfo=TIMEZONE).timestamp()
    os.utime(path, (ts, ts))
    return path


class TestFindUnsettledDates:
    def test_file_fetched_before_transcripts_could_settle_is_unsettled(self, tmp_path):
        day = date(2026, 9, 14)
        _write(tmp_path, day, fetched_on=day + timedelta(days=1))
        assert find_unsettled_dates(str(tmp_path), day, day, 8) == [day]

    def test_file_fetched_after_settle_period_is_final(self, tmp_path):
        day = date(2026, 9, 14)
        _write(tmp_path, day, fetched_on=day + timedelta(days=8))
        assert find_unsettled_dates(str(tmp_path), day, day, 8) == []

    def test_missing_file_is_not_reported(self, tmp_path):
        day = date(2026, 9, 14)
        assert find_unsettled_dates(str(tmp_path), day, day, 8) == []

    def test_non_empty_file_is_also_checked(self, tmp_path):
        # 部分的に取れている日も、遅れて確定した発話が追記される
        day = date(2026, 9, 14)
        _write(tmp_path, day, fetched_on=day + timedelta(days=2), content="部分")
        assert find_unsettled_dates(str(tmp_path), day, day, 8) == [day]


class TestBackfillSelectsUnsettledDates:
    @pytest.fixture
    def run_backfill(self, tmp_path, monkeypatch):
        fetched: list[date] = []

        async def fake_download(target_day, data_dir, **kwargs):
            fetched.append(target_day)
            return 0

        monkeypatch.setattr(mori_fetch, "DATA_DIR", str(tmp_path))
        monkeypatch.setattr(mori_fetch, "_today_jst", lambda: TODAY)
        monkeypatch.setattr(mori_fetch, "_refresh_or_exit", lambda: None)
        monkeypatch.setattr(mori_fetch, "download_with_retry", fake_download)
        monkeypatch.setattr(mori_fetch, "BACKFILL_DAY_INTERVAL_SEC", 0)

        def run(argv):
            monkeypatch.setattr(sys, "argv", ["mori_fetch.py", *argv])
            try:
                mori_fetch.main()
            except SystemExit as e:
                assert e.code in (0, None)
            return fetched

        # バックフィル範囲（過去30日）を全日「確定後に取得済み」で埋めておく
        for n in range(1, 31):
            day = TODAY - timedelta(days=n)
            _write(tmp_path, day, fetched_on=TODAY, content="確定")
        return run

    def test_empty_marker_written_before_a_long_outage_is_refetched(self, tmp_path, run_backfill):
        # 反例: D の翌日に空マーク → 8日以上実行が成功しない間に文字起こしが確定 → 再開時 D は窓の外
        day = TODAY - timedelta(days=16)
        _write(tmp_path, day, fetched_on=day + timedelta(days=1))

        fetched = run_backfill(["--refetch-recent", "8"])

        assert day in fetched

    def test_settled_dates_outside_the_window_are_not_refetched(self, run_backfill):
        fetched = run_backfill(["--refetch-recent", "8"])
        assert fetched == [TODAY - timedelta(days=n) for n in range(8, 0, -1)]

    def test_plain_backfill_without_refetch_keeps_existing_files(self, tmp_path, run_backfill):
        day = TODAY - timedelta(days=16)
        _write(tmp_path, day, fetched_on=day + timedelta(days=1))
        assert run_backfill([]) == []


class TestVerifiedFetchUpdatesFetchTime:
    """内容を保持して書き換えない分岐でも「この時点で確認した」ことを更新時刻に残す。
    残さないと、確定済みの日が毎回「未確定」と判定されて再取得され続ける。"""

    def test_kept_existing_data_is_marked_as_fetched_now(self, tmp_path, monkeypatch):
        from test_download import DAY, StubClient

        path = _write(tmp_path, DAY, fetched_on=DAY + timedelta(days=1), content="既存の実データ")
        monkeypatch.setattr(mori_fetch, "MoriClient", lambda interactive=False: StubClient(list_response={"sessions": []}))

        async def no_refresh():
            pass

        monkeypatch.setattr(mori_fetch, "refresh_if_stale", no_refresh)

        assert asyncio.run(mori_fetch.download_single_date(DAY, str(tmp_path))) == 0

        assert open(path, encoding="utf-8").read() == "既存の実データ"
        assert os.path.getmtime(path) == pytest.approx(time.time(), abs=60)

    def test_shrink_guard_keeps_content_but_marks_as_fetched_now(self, tmp_path, monkeypatch):
        from test_download import DAY, SESSION, TRANSCRIPT, StubClient

        big = "# 完全データ\n" + "[10:00:00] 発話\n" * 100
        path = _write(tmp_path, DAY, fetched_on=DAY + timedelta(days=1), content=big)
        monkeypatch.setattr(
            mori_fetch, "MoriClient", lambda interactive=False: StubClient(list_response={"sessions": [SESSION]}, fetch_response=TRANSCRIPT)
        )

        async def no_refresh():
            pass

        monkeypatch.setattr(mori_fetch, "refresh_if_stale", no_refresh)
        monkeypatch.setattr(mori_fetch, "TRANSCRIPT_FETCH_INTERVAL_SEC", 0)

        assert asyncio.run(mori_fetch.download_single_date(DAY, str(tmp_path))) == 0

        assert open(path, encoding="utf-8").read() == big
        assert os.path.getmtime(path) == pytest.approx(time.time(), abs=60)
