"""実行ロック（fcntl.flock）。シェルスクリプトと mori_fetch.py が同じロックファイルを共有する。

旧方式（mkdir + pid ファイル + 残骸回収）は、mkdir 直後で pid 未記入のロックを残骸と
誤認したり、同じ残骸を2プロセスが同時に回収したりして二重起動し得た
（formal/LockReclaim.tla）。flock は保持プロセスが終了すると OS が解放するため、
残骸回収という手順自体が無く、pid 再利用の誤判定も起きない。

- ロックファイルは削除しない（削除すると別 inode に対する flock が並立する）。
- シェルからは `exec 9>>"$LOCK_FILE"; python3 runlock.py 9` で fd 9 をロックする。
  ロックは「開いたファイル」に付くので、このコマンドが終了してもシェル（と fd を
  継承した子プロセス）が fd を開いている間は保持される。
- シェルがロック済みの fd を子の mori_fetch.py に渡す場合は、環境変数で fd 番号を伝える。
  同じ「開いたファイル」への flock は再取得しても成功するので、子は二重に弾かれない。

終了コード（CLI）: 0 = 取得 / 75 = 他プロセスが保持中 / 1 = 引数・I/O エラー
"""

from __future__ import annotations

import fcntl
import os
import sys
from collections.abc import Iterator
from contextlib import contextmanager

# 他プロセスが実行中で何もしなかったことを示す終了コード（sysexits.h の EX_TEMPFAIL）。
# 0 を返すと catchup が「成功」と誤認してスタンプを書く（formal/CatchupStamp.tla）。
SKIP_EXIT = 75

RUN_LOCK_FILE_ENV = "MORI_RUN_LOCK_FILE"
RUN_LOCK_FD_ENV = "MORI_RUN_LOCK_FD"
DEFAULT_RUN_LOCK_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".run.lock")


class RunLockBusy(RuntimeError):
    """他のプロセスが実行ロックを保持している。"""


def run_lock_path() -> str:
    return os.environ.get(RUN_LOCK_FILE_ENV, "").strip() or DEFAULT_RUN_LOCK_FILE


def _same_file(fd: int, path: str) -> bool:
    try:
        on_disk = os.stat(path)
        opened = os.fstat(fd)
    except OSError:
        return False
    return (opened.st_dev, opened.st_ino) == (on_disk.st_dev, on_disk.st_ino)


def lock_fd(fd: int) -> None:
    """開いている fd を非ブロッキングで排他ロックする。取れなければ RunLockBusy。"""
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        raise RunLockBusy("別のプロセスが実行中です") from None


def _inherited_fd(path: str, fd_env: str | None) -> int | None:
    """親のシェルから渡されたロック済み fd。ロックファイルを指していなければ無視する。"""
    raw = os.environ.get(fd_env, "").strip() if fd_env else ""
    if not raw.isdigit():
        return None
    fd = int(raw)
    return fd if _same_file(fd, path) else None


@contextmanager
def held(path: str | None = None, fd_env: str | None = RUN_LOCK_FD_ENV) -> Iterator[str]:
    """実行ロックを保持する。取得できなければ RunLockBusy。"""
    path = path or run_lock_path()
    inherited = _inherited_fd(path, fd_env)
    if inherited is not None:
        # 親と同じ「開いたファイル」なので、親が取得済みなら即成功する。閉じるのは親の役目
        lock_fd(inherited)
        yield path
        return
    while True:
        fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            lock_fd(fd)
        except BaseException:
            os.close(fd)
            raise
        if _same_file(fd, path):
            break
        os.close(fd)  # open と flock の間にファイルが差し替えられた。取り直す
    try:
        yield path
    finally:
        os.close(fd)


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 1 or not args[0].isdigit():
        print("usage: runlock.py <fd>", file=sys.stderr)
        return 1
    try:
        lock_fd(int(args[0]))
    except RunLockBusy:
        return SKIP_EXIT
    except OSError as e:
        print(f"runlock: fd {args[0]} をロックできません: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
