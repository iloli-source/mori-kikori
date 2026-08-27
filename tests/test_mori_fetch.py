"""mori_fetch.py の純粋関数のユニットテスト。"""

import os
import sys
from datetime import date

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from mori_fetch import (  # noqa: E402
    _session_date,
    _transcript_uri,
    find_missing_dates,
    transcript_to_text,
)


class TestTranscriptToText:
    def test_formats_utterances_with_time(self):
        # Arrange: Mori MCP 実レスポンス形式（started_at, JST オフセット付き）
        transcript = {
            "utterances": [
                {"started_at": "2026-08-21T09:30:00+09:00", "text": "おはよう"},
                {"started_at": "2026-08-21T09:30:05+09:00", "text": "おはようございます"},
            ]
        }

        # Act
        result = transcript_to_text(transcript, title="朝会")

        # Assert
        assert result == "# 朝会\n[09:30:00] おはよう\n[09:30:05] おはようございます"

    def test_includes_speaker_name_when_present(self):
        transcript = {
            "utterances": [
                {"started_at": "2026-08-21T09:00:00+09:00", "speaker_name": "話者A", "text": "テスト"}
            ]
        }
        assert transcript_to_text(transcript) == "[09:00:00] 話者A: テスト"

    def test_converts_utc_to_jst(self):
        transcript = {"utterances": [{"started_at": "2026-08-21T03:00:00Z", "text": "test"}]}
        assert transcript_to_text(transcript) == "[12:00:00] test"

    def test_skips_empty_text(self):
        transcript = {"utterances": [{"text": "  "}, {"text": "メモ"}]}
        assert transcript_to_text(transcript) == "[--:--:--] メモ"

    def test_empty_transcript_returns_empty(self):
        assert transcript_to_text({}) == ""


class TestSessionDate:
    def test_uses_started_at_in_jst(self):
        session = {"started_at": "2026-08-21T22:28:02+09:00"}
        assert _session_date(session) == date(2026, 8, 21)

    def test_converts_utc_to_jst_date(self):
        # UTC 2026-08-20 20:00 = JST 2026-08-21 05:00
        session = {"started_at": "2026-08-20T20:00:00Z"}
        assert _session_date(session) == date(2026, 8, 21)

    def test_returns_none_without_start(self):
        assert _session_date({}) is None


class TestTranscriptUri:
    def test_derives_transcript_uri_from_session_uri(self):
        session = {"id": "mori://session/d12f88e9-7b6f-4f36-bac2-e93ebe5a210f"}
        assert _transcript_uri(session) == "mori://transcript/session/d12f88e9-7b6f-4f36-bac2-e93ebe5a210f"

    def test_accepts_raw_uuid(self):
        session = {"id": "d12f88e9-7b6f-4f36-bac2-e93ebe5a210f"}
        assert _transcript_uri(session) == "mori://transcript/session/d12f88e9-7b6f-4f36-bac2-e93ebe5a210f"

    def test_passes_through_transcript_uri(self):
        session = {"id": "mori://transcript/session/abc"}
        assert _transcript_uri(session) == "mori://transcript/session/abc"


class TestFindMissingDates:
    def test_detects_missing_dates(self, tmp_path):
        # Arrange: 8/20 だけ存在
        (tmp_path / "mori_transcript_2026-08-20.txt").write_text("x")

        # Act
        missing = find_missing_dates(str(tmp_path), date(2026, 8, 19), date(2026, 8, 21))

        # Assert
        assert missing == [date(2026, 8, 19), date(2026, 8, 21)]

    def test_all_present_returns_empty(self, tmp_path):
        (tmp_path / "mori_transcript_2026-08-20.txt").write_text("")
        assert find_missing_dates(str(tmp_path), date(2026, 8, 20), date(2026, 8, 20)) == []


class TestParseIso:
    def test_naive_timestamp_is_treated_as_utc(self):
        from mori_fetch import _fmt_time

        # naive な "03:00" は UTC とみなし JST 12:00 になる（ホストTZ非依存）
        assert _fmt_time("2026-08-21T03:00:00") == "12:00:00"

    def test_invalid_timestamp_returns_placeholder(self):
        from mori_fetch import _fmt_time

        assert _fmt_time("not-a-date") == "--:--:--"
        assert _fmt_time(None) == "--:--:--"


class TestTitleOnlyTranscript:
    def test_title_without_utterances_returns_empty(self):
        # 発話ゼロならタイトルがあっても本文なし扱い（保存確定させない）
        assert transcript_to_text({"utterances": []}, title="タイトルだけ") == ""

    def test_title_prepended_when_utterances_exist(self):
        transcript = {"utterances": [{"started_at": "2026-08-21T09:00:00+09:00", "text": "あ"}]}
        assert transcript_to_text(transcript, title="T") == "# T\n[09:00:00] あ"
