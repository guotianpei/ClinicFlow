"""The local state directory of the synthetic installer (Option B B2). SYNTHETIC ONLY.

`$HALOFLOW_SYNTHETIC_STATE` holds `state.json` (0600) and `state.lock` (0600) in a 0700
directory owned by the current uid. Every open is no-follow. The lock file is flock'ed
exclusively for the whole run and its inode is pinned: before every write the path is
`lstat`ed again and must still be that inode. Writes go to a `dir_fd`-relative temp file,
fsync, `os.replace`, then a directory fsync. A crash anywhere leaves the previous
`state.json` intact.

The flock is a same-host convenience refusal only; installation safety comes from the
database protocol (guard.py).
"""

from __future__ import annotations

import errno
import fcntl
import json
import os
import stat
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Final

from haloflow.synthetic.errors import SyntheticRefused
from haloflow.synthetic.hooks import fire

PHASES: Final = ("intent", "stamped", "migrated", "logins", "tenant", "profile", "activated",
                 "settings", "staff", "ready")
STATE_FILE: Final = "state.json"
LOCK_FILE: Final = "state.lock"
_TMP_PREFIX: Final = ".state.json.tmp-"
_NOFOLLOW: Final = os.O_NOFOLLOW | os.O_CLOEXEC


def next_phase(phase: str) -> str:
    if phase not in PHASES or phase == PHASES[-1]:
        raise ValueError("no successor phase")
    return PHASES[PHASES.index(phase) + 1]


class StateDir:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    @contextmanager
    def locked(self) -> Iterator[LockedState]:
        dir_fd = _open_dir(self.path)
        lock_fd = -1
        try:
            try:
                lock_fd = os.open(LOCK_FILE, os.O_RDWR | os.O_CREAT | _NOFOLLOW, 0o600,
                                  dir_fd=dir_fd)
            except OSError as error:
                code = "STATE_SYMLINK" if error.errno == errno.ELOOP else "STATE_PERMISSIONS"
                raise SyntheticRefused(code) from None
            st = os.fstat(lock_fd)
            if not stat.S_ISREG(st.st_mode) or st.st_uid != os.getuid():
                raise SyntheticRefused("STATE_PERMISSIONS")
            if stat.S_IMODE(st.st_mode) != 0o600:
                os.fchmod(lock_fd, 0o600)
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                raise SyntheticRefused("STATE_LOCKED") from None
            for name in os.listdir(dir_fd):                 # stale temps of a dead writer
                if name.startswith(_TMP_PREFIX):
                    os.unlink(name, dir_fd=dir_fd)
            yield LockedState(dir_fd, lock_fd, (st.st_dev, st.st_ino))
        finally:
            if lock_fd >= 0:
                os.close(lock_fd)
            os.close(dir_fd)


def _open_dir(path: Path) -> int:
    if not path.is_absolute():
        raise SyntheticRefused("STATE_PATH_INVALID")
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        raise SyntheticRefused("STATE_MISSING") from None
    if stat.S_ISLNK(st.st_mode):
        raise SyntheticRefused("STATE_SYMLINK")
    try:
        fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | _NOFOLLOW)
    except OSError:
        raise SyntheticRefused("STATE_SYMLINK") from None
    st = os.fstat(fd)
    if st.st_uid != os.getuid() or stat.S_IMODE(st.st_mode) != 0o700:
        os.close(fd)
        raise SyntheticRefused("STATE_PERMISSIONS")
    return fd


class LockedState:
    def __init__(self, dir_fd: int, lock_fd: int, inode: tuple[int, int]) -> None:
        self._dir_fd = dir_fd
        self._lock_fd = lock_fd
        self._inode = inode

    def read(self) -> dict[str, Any] | None:
        try:
            fd = os.open(STATE_FILE, os.O_RDONLY | _NOFOLLOW, dir_fd=self._dir_fd)
        except FileNotFoundError:
            return None
        except OSError as error:
            code = "STATE_SYMLINK" if error.errno == errno.ELOOP else "STATE_PERMISSIONS"
            raise SyntheticRefused(code) from None
        try:
            st = os.fstat(fd)
            if (not stat.S_ISREG(st.st_mode) or st.st_uid != os.getuid()
                    or stat.S_IMODE(st.st_mode) != 0o600):
                raise SyntheticRefused("STATE_PERMISSIONS")
            raw = b""
            while chunk := os.read(fd, 65536):
                raw += chunk
        finally:
            os.close(fd)
        try:
            value = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            value = None
        if not isinstance(value, dict):
            raise SyntheticRefused("STATE_CORRUPT")
        return value

    def write(self, value: dict[str, Any]) -> None:
        self._check_lock()
        data = json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
        tmp = f"{_TMP_PREFIX}{os.getpid()}"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | _NOFOLLOW, 0o600,
                     dir_fd=self._dir_fd)
        try:
            view = memoryview(data)
            while view:
                view = view[os.write(fd, view):]
            os.fsync(fd)
        finally:
            os.close(fd)
        fire("state.after_temp_fsync", {"phase": value.get("phase")})
        self._check_lock()
        os.replace(tmp, STATE_FILE, src_dir_fd=self._dir_fd, dst_dir_fd=self._dir_fd)
        os.fsync(self._dir_fd)

    def _check_lock(self) -> None:
        try:
            st = os.stat(LOCK_FILE, dir_fd=self._dir_fd, follow_symlinks=False)
        except FileNotFoundError:
            raise SyntheticRefused("STATE_LOCK_REPLACED") from None
        if (st.st_dev, st.st_ino) != self._inode:
            raise SyntheticRefused("STATE_LOCK_REPLACED")
