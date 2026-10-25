"""Single-host, process-lifetime exclusive lock (remedy for Codex issue 1).

``fcntl.flock(LOCK_EX | LOCK_NB)`` on a file keyed to the canonical demo
instance. The kernel releases the lock when the holding process dies, so a
crash never leaves a stale lock. The file is never unlinked or recreated:
unlinking would let a second process lock a new inode while the first still
holds the old one.

Scope: one host. This is not a distributed fencing guarantee.
"""

from __future__ import annotations

import fcntl
import os
from pathlib import Path

from haloflow.demo.settings import DemoDbTarget, StartupRefused


def lock_path_for(lock_dir: str | os.PathLike[str], target: DemoDbTarget) -> Path:
    return Path(lock_dir) / f"haloflow-demo-{target.key_hash}.lock"


class ProcessLock:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._fd: int | None = None

    @property
    def held(self) -> bool:
        return self._fd is not None

    def acquire(self) -> None:
        if self._fd is not None:
            raise RuntimeError("ProcessLock already held by this object")
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT | os.O_CLOEXEC, 0o600)
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
