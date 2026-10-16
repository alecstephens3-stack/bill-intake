"""One copy at a time.

A second launch while the first is still reading (a double click that looked like it did
nothing) must not start a second run over the same folder: two runs would race to move the same
files and enter the same bills. The second copy finds the first one's review page and points the
person there instead.

The lock is an OS file lock, not a "pid file". The OS drops it when the process ends, even in a
crash, so there is never a stale lock to clean up by hand. The page address lives in a separate
file next to it, because on Windows a locked byte range cannot be read by another process.
"""
from __future__ import annotations

import os
from pathlib import Path

try:
    import fcntl
except ImportError:          # Windows
    fcntl = None
try:
    import msvcrt
except ImportError:          # macOS, Linux
    msvcrt = None


class InstanceLock:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.url_path = self.path.with_name(self.path.name + ".url")
        self._fd: int | None = None

    def acquire(self) -> bool:
        """True if this is the only copy. False if another copy holds the lock."""
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            if fcntl:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            elif msvcrt:
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        except OSError:
            os.close(fd)
            return False
        self._fd = fd
        return True

    def publish(self, url: str) -> None:
        """Tell a later copy where this copy's page is."""
        self.url_path.write_text(url, encoding="utf-8")

    def other_url(self) -> str | None:
        try:
            return self.url_path.read_text(encoding="utf-8").strip() or None
        except OSError:
            return None

    def release(self) -> None:
        if self._fd is None:
            return
        try:
            self.url_path.unlink()
        except OSError:
            pass
        try:
            if fcntl:
                fcntl.flock(self._fd, fcntl.LOCK_UN)
            elif msvcrt:
                os.lseek(self._fd, 0, os.SEEK_SET)
                msvcrt.locking(self._fd, msvcrt.LK_UNLCK, 1)
        finally:
            os.close(self._fd)
            self._fd = None

    def __enter__(self) -> "InstanceLock":
        return self

    def __exit__(self, *exc) -> None:
        self.release()
