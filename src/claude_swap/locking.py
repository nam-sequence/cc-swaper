"""File locking for concurrent access protection."""

from __future__ import annotations

import os
import stat
import sys
import time
from pathlib import Path
from typing import IO

# Platform-specific imports for file locking
if sys.platform == "win32":
    import msvcrt
else:
    import fcntl

from claude_swap.exceptions import LockError


class FileLock:
    """Cross-process file lock using platform-specific APIs."""

    def __init__(
        self, lock_path: Path, timeout: float = 10.0, *, shared: bool = False
    ):
        self.lock_path = lock_path
        self.timeout = timeout
        self.shared = shared
        self._lock_file: IO | None = None
        self._locked = False

    def acquire(self, timeout: float | None = None) -> bool:
        """Acquire exclusive (or configured shared) lock with timeout.

        Args:
            timeout: Maximum seconds to wait for lock. Defaults to the
                timeout given at construction.

        Returns:
            True if lock acquired, False if timeout.
        """
        if timeout is None:
            timeout = self.timeout
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        flags = os.O_CREAT | os.O_RDWR
        if hasattr(os, "O_CLOEXEC"):
            flags |= os.O_CLOEXEC
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            descriptor = os.open(self.lock_path, flags, 0o600)
            info = os.fstat(descriptor)
            if not stat.S_ISREG(info.st_mode):
                os.close(descriptor)
                raise LockError(f"Lock path is not a regular file: {self.lock_path}")
            if hasattr(os, "getuid") and info.st_uid != os.getuid():
                os.close(descriptor)
                raise LockError(f"Lock file has an unexpected owner: {self.lock_path}")
            if hasattr(os, "fchmod"):
                os.fchmod(descriptor, 0o600)
            self._lock_file = os.fdopen(descriptor, "r+")
        except OSError as exc:
            raise LockError(f"Could not open lock file {self.lock_path}: {exc}") from exc

        start = time.monotonic()
        while True:
            try:
                if sys.platform == "win32":
                    if self.shared:
                        raise LockError("Shared file locks are not supported on Windows")
                    # Windows: use msvcrt for file locking
                    msvcrt.locking(self._lock_file.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    # POSIX: use fcntl for file locking
                    lock_mode = fcntl.LOCK_SH if self.shared else fcntl.LOCK_EX
                    fcntl.flock(self._lock_file.fileno(), lock_mode | fcntl.LOCK_NB)
                self._locked = True
                return True
            except (BlockingIOError, OSError):
                if time.monotonic() - start > timeout:
                    self._lock_file.close()
                    self._lock_file = None
                    return False
                time.sleep(0.1)

    def release(self) -> None:
        """Release the lock."""
        if self._lock_file and self._locked:
            if sys.platform == "win32":
                # Windows: unlock using msvcrt
                try:
                    msvcrt.locking(self._lock_file.fileno(), msvcrt.LK_UNLCK, 1)
                except OSError:
                    pass  # File may already be unlocked
            else:
                # POSIX: unlock using fcntl
                fcntl.flock(self._lock_file.fileno(), fcntl.LOCK_UN)
            self._lock_file.close()
            self._lock_file = None
            self._locked = False

    def __enter__(self) -> FileLock:
        if not self.acquire():
            raise LockError("Failed to acquire lock - another instance may be running")
        return self

    def __exit__(self, *args) -> None:
        self.release()
