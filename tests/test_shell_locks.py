"""シェルスクリプトのロック／スタンプ挙動（formal/LockReclaim.tla・CatchupStamp.tla の反例を実装で塞ぐ）。

スクリプトを一時ディレクトリへコピーして実行するので、リポジトリ直下の logs/ やロックには触れない。
mori_fetch.py は偽物に差し替えるため、ネットワークにも出ない。
"""

import datetime
import os
import shutil
import signal
import subprocess
import sys
import time
import zoneinfo
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SKIP_EXIT = 75

# 本物の runlock でロックを確認してから印を残す偽 mori_fetch.py
FAKE_FETCH = """
import os, sys, time
import runlock
here = os.path.dirname(os.path.abspath(__file__))
try:
    with runlock.held():
        with open(os.path.join(here, "fetch-invoked"), "a") as f:
            f.write(" ".join(sys.argv[1:]) + "\\n")
        time.sleep(float(os.environ.get("FAKE_SLEEP", "0")))
        sys.exit(int(os.environ.get("FAKE_EXIT", "0")))
except runlock.RunLockBusy:
    sys.exit(75)
"""

HOLDER = "import sys, runlock\nwith runlock.held(sys.argv[1], fd_env=None):\n    print('held', flush=True)\n    sys.stdin.read()\n"


@pytest.fixture
def repo(tmp_path):
    """スクリプト・runlock.py・偽 mori_fetch.py だけを置いた一時リポジトリ。"""
    for name in ("run_mori_daily.sh", "run_mori_catchup.sh", "runlock.py"):
        if (ROOT / name).exists():
            shutil.copy(ROOT / name, tmp_path / name)
    (tmp_path / "mori_fetch.py").write_text(FAKE_FETCH)
    (tmp_path / ".venv" / "bin").mkdir(parents=True)
    (tmp_path / ".venv" / "bin" / "python3").symlink_to(sys.executable)
    return tmp_path


def _env(repo: Path, **extra) -> dict:
    env = {k: v for k, v in os.environ.items() if not k.startswith("MORI_")}
    return dict(env, TZ="Asia/Tokyo", **extra)


def _run(repo: Path, script: str, **extra):
    return subprocess.run(["/bin/bash", str(repo / script)], capture_output=True, text=True, env=_env(repo, **extra), timeout=60)


def _hold(repo: Path, lock_name: str) -> subprocess.Popen:
    """ロックファイルを flock で保持する別プロセス（手動実行や先行実行の代役）。"""
    proc = subprocess.Popen(
        [sys.executable, "-c", HOLDER, str(repo / lock_name)], cwd=ROOT, stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True
    )
    assert proc.stdout.readline().strip() == "held"
    return proc


def _release(proc: subprocess.Popen) -> None:
    proc.stdin.close()
    proc.wait(timeout=30)
    proc.stdout.close()


def _today() -> str:
    return datetime.datetime.now(zoneinfo.ZoneInfo("Asia/Tokyo")).date().isoformat()


# ---------- run_mori_daily.sh ----------


def test_daily_runs_fetch_under_the_lock_and_returns_its_exit_code(repo):
    ok = _run(repo, "run_mori_daily.sh")
    assert ok.returncode == 0, ok.stderr
    assert (repo / "fetch-invoked").read_text() == "--refetch-recent 8\n"
    failed = _run(repo, "run_mori_daily.sh", FAKE_EXIT="1")
    assert failed.returncode == 1
    assert "END (exit=1)" in (repo / "logs" / "mori-cron.log").read_text()


def test_daily_exits_75_and_does_nothing_while_another_run_holds_the_lock(repo):
    holder = _hold(repo, ".run.lock")
    try:
        r = _run(repo, "run_mori_daily.sh")
    finally:
        _release(holder)
    assert r.returncode == SKIP_EXIT, r.stderr
    assert not (repo / "fetch-invoked").exists()
    assert "skip" in (repo / "logs" / "mori-cron.log").read_text()


def test_daily_is_not_fooled_by_lock_dir_without_pid(repo):
    # 旧方式の反例（LockReclaimBugEmptyPid）: 先行プロセスが mkdir した直後・pid 未記入の状態を
    # 残骸と誤認して回収し、2つ目も本処理に入る。新方式では保持者がいる限り必ず弾かれる
    (repo / ".run-lock").mkdir()
    holder = _hold(repo, ".run.lock")
    try:
        r = _run(repo, "run_mori_daily.sh")
    finally:
        _release(holder)
    assert r.returncode == SKIP_EXIT, r.stderr
    assert not (repo / "fetch-invoked").exists()


def test_simultaneous_daily_runs_admit_exactly_one(repo):
    procs = [
        subprocess.Popen(["/bin/bash", str(repo / "run_mori_daily.sh")], env=_env(repo, FAKE_SLEEP="2"), stderr=subprocess.DEVNULL)
        for _ in range(6)
    ]
    codes = sorted(p.wait(timeout=60) for p in procs)
    assert codes == [0] + [SKIP_EXIT] * 5
    assert len((repo / "fetch-invoked").read_text().splitlines()) == 1


def test_lock_is_released_when_holder_is_killed(repo):
    running = subprocess.Popen(
        ["/bin/bash", str(repo / "run_mori_daily.sh")], env=_env(repo, FAKE_SLEEP="30"), start_new_session=True
    )
    try:
        deadline = time.monotonic() + 30
        while not (repo / "fetch-invoked").exists():
            assert time.monotonic() < deadline, "偽 mori_fetch.py が起動しなかった"
            time.sleep(0.05)
        assert _run(repo, "run_mori_daily.sh").returncode == SKIP_EXIT
    finally:
        os.killpg(running.pid, signal.SIGKILL)
        running.wait(timeout=30)
    r = _run(repo, "run_mori_daily.sh")
    assert r.returncode == 0, r.stderr  # 残骸回収なしで即取得できる


def test_daily_never_deletes_the_lock_file(repo):
    assert _run(repo, "run_mori_daily.sh").returncode == 0
    assert (repo / ".run.lock").is_file()


# ---------- run_mori_catchup.sh ----------


def _fake_daily(repo: Path, exit_code: int) -> Path:
    f = repo / f"fake_daily_{exit_code}.sh"
    f.write_text(f'#!/bin/bash\necho fake >> "$(dirname "$0")/daily-invoked"\nexit {exit_code}\n')
    f.chmod(0o755)
    return f


@pytest.mark.parametrize("code,expect_stamp,expect_fail", [(0, True, False), (1, False, True), (SKIP_EXIT, False, False)])
def test_catchup_stamp_and_failcount_by_exit_code(repo, code, expect_stamp, expect_fail):
    r = _run(repo, "run_mori_catchup.sh", MORI_DAILY_SCRIPT=str(_fake_daily(repo, code)))
    assert r.returncode == (0 if code in (0, SKIP_EXIT) else code), r.stderr
    assert (repo / "daily-invoked").exists()
    assert (repo / "logs" / ".last-success-date").exists() is expect_stamp
    assert (repo / "logs" / ".consecutive-failures").exists() is expect_fail
    if code == SKIP_EXIT:
        assert "skipped" in (repo / "logs" / "mori-catchup.log").read_text()


def test_skip_does_not_reset_an_existing_failure_count(repo):
    (repo / "logs").mkdir()
    (repo / "logs" / ".consecutive-failures").write_text("2\n")
    _run(repo, "run_mori_catchup.sh", MORI_DAILY_SCRIPT=str(_fake_daily(repo, SKIP_EXIT)))
    assert (repo / "logs" / ".consecutive-failures").read_text().strip() == "2"


def test_catchup_does_not_stamp_while_manual_run_holds_the_run_lock(repo):
    # CatchupStampBug の反例: 手動実行がロック保持中に catchup が daily を起動 → daily は何もせず終了
    # → 旧実装は exit 0 を成功とみなしてスタンプし、手動実行が失敗してもその日は再試行されない
    manual = _hold(repo, ".run.lock")
    try:
        r = _run(repo, "run_mori_catchup.sh")
    finally:
        _release(manual)
    assert r.returncode == 0, r.stderr
    assert not (repo / "fetch-invoked").exists()
    assert not (repo / "logs" / ".last-success-date").exists()
    assert not (repo / "logs" / ".consecutive-failures").exists()
    # ロックが空けば次の発火で実行され、スタンプされる
    r = _run(repo, "run_mori_catchup.sh")
    assert r.returncode == 0, r.stderr
    assert (repo / "logs" / ".last-success-date").read_text().strip() == _today()


def test_second_catchup_exits_without_invoking_daily(repo):
    first = _hold(repo, ".catchup.lock")
    try:
        r = _run(repo, "run_mori_catchup.sh", MORI_DAILY_SCRIPT=str(_fake_daily(repo, 0)))
    finally:
        _release(first)
    assert r.returncode == 0, r.stderr
    assert not (repo / "daily-invoked").exists()
    assert not (repo / "logs" / ".last-success-date").exists()


def test_catchup_skips_when_already_stamped_today(repo):
    (repo / "logs").mkdir()
    (repo / "logs" / ".last-success-date").write_text(_today())
    r = _run(repo, "run_mori_catchup.sh", MORI_DAILY_SCRIPT=str(_fake_daily(repo, 0)))
    assert r.returncode == 0
    assert not (repo / "daily-invoked").exists()


def test_catchup_counts_failure_when_lock_helper_is_broken(repo):
    # ロック確認自体が失敗（python が無い等）した場合は「未実施」ではなく失敗として通知対象にする
    (repo / ".venv" / "bin" / "python3").unlink()
    r = _run(repo, "run_mori_catchup.sh")
    assert r.returncode != 0
    assert not (repo / "logs" / ".last-success-date").exists()
    assert (repo / "logs" / ".consecutive-failures").read_text().strip() == "1"
