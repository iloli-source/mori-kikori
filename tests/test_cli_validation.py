"""CLI 引数バリデーションのテスト。

矛盾した引数・不正な範囲を黙って無視/成功扱いせず、明確なエラー(exit 1)で
落とすことを保証する。exit 2 は「全取得成功でデータなし」の契約値なので
バリデーションエラーには使わない。
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import mori_fetch  # noqa: E402
from mori_fetch import LOGIN_HINT, main  # noqa: E402


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    """バリデーション不備でネットワーク経路に到達したら即失敗させる。

    (実装前の RED 状態で実ログイン待ち・実フェッチにハングしないための安全網)
    """

    def _forbidden(*args, **kwargs):
        raise AssertionError("validation should reject before any network call")

    monkeypatch.setattr(mori_fetch, "_refresh_or_exit", _forbidden)
    monkeypatch.setattr(mori_fetch, "do_login", _forbidden)
    monkeypatch.setattr(mori_fetch, "do_list_tools", _forbidden)
    monkeypatch.setattr(mori_fetch, "download_with_retry", _forbidden)


def run_main(argv, capsys):
    old_argv = sys.argv
    sys.argv = ["mori_fetch.py", *argv]
    try:
        with pytest.raises(SystemExit) as exc_info:
            main()
    finally:
        sys.argv = old_argv
    captured = capsys.readouterr()
    return exc_info.value.code, captured.err


class TestConflictingArguments:
    def test_date_and_days_ago_conflict(self, capsys):
        code, err = run_main(["--date", "2026-08-21", "--days-ago", "1"], capsys)
        assert code == 1
        assert "併用できません" in err

    def test_date_with_backfill_options_conflict(self, capsys):
        code, err = run_main(["--date", "2026-08-21", "--refetch-recent", "8"], capsys)
        assert code == 1
        assert "併用できません" in err

    def test_days_ago_with_start_date_conflict(self, capsys):
        code, err = run_main(["--days-ago", "1", "--start-date", "2026-08-01"], capsys)
        assert code == 1
        assert "併用できません" in err

    def test_login_with_fetch_options_conflict(self, capsys):
        code, err = run_main(["--login", "--date", "2026-08-21"], capsys)
        assert code == 1
        assert "併用できません" in err

    def test_login_with_list_tools_conflict(self, capsys):
        # 併用すると --list-tools が黙って無視される偽成功を防ぐ
        code, err = run_main(["--login", "--list-tools"], capsys)
        assert code == 1
        assert "併用できません" in err


class TestRangeValidation:
    def test_start_after_end_errors(self, capsys):
        code, err = run_main(
            ["--start-date", "2026-08-31", "--end-date", "2026-08-01"], capsys
        )
        assert code == 1
        assert "エラー" in err

    def test_days_back_zero_errors(self, capsys):
        code, err = run_main(["--days-back", "0"], capsys)
        assert code == 1
        assert "エラー" in err

    def test_negative_refetch_recent_errors(self, capsys):
        code, err = run_main(["--refetch-recent", "-1"], capsys)
        assert code == 1
        assert "エラー" in err

    def test_end_date_today_or_future_errors(self, capsys):
        # バックフィルは当日を対象にしない契約。当日以降の --end-date は
        # 未確定日の空マークを作ってしまうため拒否する
        from datetime import date as _date

        import mori_fetch as mf

        today = mf._today_jst()
        code, err = run_main(["--end-date", today.isoformat()], capsys)
        assert code == 1
        assert "エラー" in err
        assert isinstance(today, _date)


class TestArgparseErrorExitCode:
    def test_invalid_int_value_exits_1_not_2(self, capsys):
        # argparse デフォルトのエラー exit 2 は「全取得成功でデータなし」の
        # 契約値と衝突するため、引数エラーは exit 1 に統一する
        code, err = run_main(["--days-ago", "foo"], capsys)
        assert code == 1
        assert "エラー" in err

    def test_unknown_option_exits_1(self, capsys):
        code, err = run_main(["--no-such-option"], capsys)
        assert code == 1


class TestLoginHint:
    def test_login_hint_uses_venv_python(self):
        # 認証失効メッセージをそのままコピペして実行できること
        # (システム python では ModuleNotFoundError になるため venv パス必須)
        assert ".venv/bin/python" in LOGIN_HINT
