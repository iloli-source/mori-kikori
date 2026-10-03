"""formal/LockReclaim.tla / TokenRefresh.tla の回帰テスト（実プロセスで検証。本番のロックは使わない）。"""

import os
import subprocess
import sys

import pytest

ROOT = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, ROOT)

import mori_fetch  # noqa: E402
import runlock  # noqa: E402

# ロックを取れたら "held"、取れなければ "busy" を出力し、stdin が閉じるまで保持する子プロセス
HOLDER = """
import sys
import runlock
try:
    with runlock.held(sys.argv[1]):
        print('held', flush=True)
        sys.stdin.read()
except runlock.RunLockBusy:
    print('busy', flush=True)
"""


def _spawn(lock, **kwargs) -> subprocess.Popen:
    return subprocess.Popen(
        [sys.executable, "-c", HOLDER, str(lock)], cwd=ROOT, stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True, **kwargs
    )


def _finish(proc: subprocess.Popen) -> None:
    proc.stdin.close()
    proc.wait(timeout=30)
    proc.stdout.close()


class TestRunLock:
    def test_second_process_is_refused_while_first_holds(self, tmp_path):
        lock = tmp_path / "run.lock"
        first = _spawn(lock)
        assert first.stdout.readline().strip() == "held"
        second = _spawn(lock)
        assert second.stdout.readline().strip() == "busy"
        _finish(second)
        _finish(first)
        third = _spawn(lock)
        assert third.stdout.readline().strip() == "held"
        _finish(third)

    def test_many_simultaneous_starters_admit_exactly_one(self, tmp_path):
        # 旧方式の反例: 残骸（死んだ pid）を複数プロセスが同時に回収すると全員が実行に入る
        lock = tmp_path / "run.lock"
        lock.write_text("999999\n")
        procs = [_spawn(lock) for _ in range(8)]
        answers = [p.stdout.readline().strip() for p in procs]
        for proc in procs:
            _finish(proc)
        assert answers.count("held") == 1
        assert answers.count("busy") == 7

    def test_killed_holder_releases_lock(self, tmp_path):
        lock = tmp_path / "run.lock"
        first = _spawn(lock)
        assert first.stdout.readline().strip() == "held"
        first.kill()
        first.wait(timeout=30)
        first.stdin.close()
        first.stdout.close()
        second = _spawn(lock)
        assert second.stdout.readline().strip() == "held"
        _finish(second)

    def test_leftover_pid_of_live_unrelated_process_does_not_block(self, tmp_path):
        # pid 再利用: ファイルに生存中の無関係な pid が残っていても、保持者がいなければ取得できる
        lock = tmp_path / "run.lock"
        lock.write_text(f"{os.getpid()}\n")
        with runlock.held(str(lock)):
            pass

    def test_child_accepts_lock_already_held_on_inherited_fd(self, tmp_path):
        # run_mori_daily.sh がロック済みの fd を mori_fetch.py に渡す経路
        lock = tmp_path / "run.lock"
        fd = os.open(lock, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            runlock.lock_fd(fd)
            env = dict(os.environ, MORI_RUN_LOCK_FD=str(fd))
            child = _spawn(lock, env=env, pass_fds=(fd,))
            assert child.stdout.readline().strip() == "held"
            _finish(child)
            # fd を知らされない（= 手動起動の）プロセスは弾かれる
            env.pop("MORI_RUN_LOCK_FD")
            stranger = _spawn(lock, env=env)
            assert stranger.stdout.readline().strip() == "busy"
            _finish(stranger)
        finally:
            os.close(fd)

    def test_inherited_fd_pointing_elsewhere_is_ignored(self, tmp_path, monkeypatch):
        lock = tmp_path / "run.lock"
        other = os.open(tmp_path / "other", os.O_RDWR | os.O_CREAT, 0o600)
        holder = _spawn(lock)
        try:
            assert holder.stdout.readline().strip() == "held"
            monkeypatch.setenv("MORI_RUN_LOCK_FD", str(other))
            with pytest.raises(runlock.RunLockBusy):
                with runlock.held(str(lock)):
                    pass
        finally:
            _finish(holder)
            os.close(other)

    def test_cli_locks_given_fd(self, tmp_path):
        lock = tmp_path / "run.lock"
        fd = os.open(lock, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            assert runlock.main([str(fd)]) == 0
            holder = _spawn(lock)
            assert holder.stdout.readline().strip() == "busy"
            _finish(holder)
        finally:
            os.close(fd)
        holder = _spawn(lock)
        assert holder.stdout.readline().strip() == "held"
        fd = os.open(lock, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            assert runlock.main([str(fd)]) == runlock.SKIP_EXIT
        finally:
            os.close(fd)
            _finish(holder)

    def test_cli_rejects_bad_arguments(self):
        assert runlock.main([]) == 1
        assert runlock.main(["abc"]) == 1


class TestMoriFetchTakesRunLock:
    """手動の mori_fetch.py も日次実行と同じロックを取る（取得ファイルとトークン更新の直列化）。"""

    @pytest.fixture(autouse=True)
    def no_network(self, monkeypatch):
        def _forbidden(*args, **kwargs):
            raise AssertionError("ロック未取得のままトークン・ネットワーク経路に到達した")

        for name in ("_refresh_or_exit", "do_login", "do_list_tools", "download_with_retry", "ensure_fresh_token"):
            monkeypatch.setattr(mori_fetch, name, _forbidden)

    @pytest.mark.parametrize(
        "argv",
        [["--date", "2026-08-21"], ["--days-ago", "1"], ["--refetch-recent", "8"], [], ["--list-tools"], ["--login"]],
    )
    def test_exits_75_without_touching_tokens_when_lock_is_held(self, argv, tmp_path, monkeypatch, capsys):
        lock = tmp_path / "held.lock"
        monkeypatch.setenv("MORI_RUN_LOCK_FILE", str(lock))
        monkeypatch.setattr(mori_fetch, "DATA_DIR", str(tmp_path / "data"))
        monkeypatch.setattr(sys, "argv", ["mori_fetch.py", *argv])
        holder = _spawn(lock)
        try:
            assert holder.stdout.readline().strip() == "held"
            with pytest.raises(SystemExit) as exc_info:
                mori_fetch.main()
        finally:
            _finish(holder)
        assert exc_info.value.code == runlock.SKIP_EXIT
        assert "実行中" in capsys.readouterr().err
