"""Single-host, process-lifetime exclusive lock (remedy for Codex issue 1).

``fcntl.flock(LOCK_EX | LOCK_NB)`` on ONE fixed host-wide file (correction R2):
``/tmp/haloflow-demo-locks/reminders-demo.lock``. The location is a constant,
not a setting, and is not keyed to the database URL, so no configuration
(different directory, equivalent TCP/socket URL) can produce a second lock
namespace. At most one demonstrator process runs per host.

The kernel releases the lock when the holding process dies, so a crash never
leaves a stale lock. The file is never unlinked or recreated: unlinking would
let a second process lock a new inode while the first still holds the old one.
The directory must be a real directory owned by this user with no group/other
access, and the file is opened with ``O_NOFOLLOW``; otherwise start-up is
refused.

Scope: one host. This is not a distributed fencing guarantee.
"""

from __future__ import annotations

import fcntl
import os
import stat
from pathlib import Path

from haloflow_demo.settings import StartupRefused

LOCK_DIR = Path("/tmp/haloflow-demo-locks")
LOCK_PATH = LOCK_DIR / "reminders-demo.lock"


def _check_dir(path: Path) -> None:
    try:
        path.mkdir(mode=0o700, exist_ok=True)
        st = os.lstat(path)
    except OSError as exc:
        raise StartupRefused("process_lock_unavailable", str(exc)) from exc
    if not stat.S_ISDIR(st.st_mode):
        raise StartupRefused("process_lock_unavailable", "lock directory is not a directory")
    if st.st_uid != os.getuid():
        raise StartupRefused("process_lock_unavailable", "lock directory owned by another user")
    if st.st_mode & 0o077:
        raise StartupRefused("process_lock_unavailable", "lock directory is group/other accessible")


class ProcessLock:
    """``ProcessLock()`` uses the fixed host-wide path. The ``path`` argument
    exists for unit tests of the mechanism only; the runner never passes it."""

    def __init__(self, path: Path = LOCK_PATH) -> None:
        self.path = path
        self._fd: int | None = None

    @property
    def held(self) -> bool:
        return self._fd is not None

    def acquire(self) -> None:
        if self._fd is not None:
            raise RuntimeError("ProcessLock already held by this object")
        _check_dir(self.path.parent)
        try:
            fd = os.open(self.path, os.O_RDWR | os.O_CREAT | os.O_CLOEXEC | os.O_NOFOLLOW, 0o600)
        except OSError as exc:
            raise StartupRefused("process_lock_unavailable", str(exc)) from exc
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            os.close(fd)
            raise StartupRefused("process_lock_held", str(self.path)) from exc
        except BaseException:
            os.close(fd)
            raise
        self._fd = fd

    def release(self) -> None:
        if self._fd is None:
            return
        fd, self._fd = self._fd, None
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)
