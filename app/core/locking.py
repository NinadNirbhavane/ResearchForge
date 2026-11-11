"""Cross-process locking for mutation boundaries (UI-01).

The extraction record uses read-modify-write. Without coordination, a CLI
decision and a browser decision (or two browser tabs) could overwrite
each other. This module provides one small, cross-platform exclusive
lock used at the shared decision boundary, so both interfaces serialize
through the same mechanism.

Scope (documented honestly): this serializes *decision writes* for one
workspace (extraction candidates and whole-record decisions). It does not
lock extraction, drafting, synthesis or export. The lock is advisory —
only code that calls it is protected.

Implementation: a per-path in-process `threading.Lock` (also serializes
threads of the same process, where OS file locks are not reliably
per-handle) plus an OS-level byte-range lock (`msvcrt.locking` on Windows,
`fcntl.flock` on POSIX) for other processes.
"""
import os
import threading
import time
from contextlib import contextmanager
from pathlib import Path

_thread_locks = {}
_guard = threading.Lock()


def _thread_lock(path: Path) -> threading.Lock:
    key = str(path.resolve())
    with _guard:
        return _thread_locks.setdefault(key, threading.Lock())


def lock_path(root: Path) -> Path:
    """Single decision lock per workspace (coarse, deliberately so)."""
    return root / "sources" / "extracted_notes" / ".decisions.lock"


class LockTimeout(TimeoutError):
    """Could not acquire the decision lock within the timeout."""


def _acquire_os(handle, timeout: float) -> None:
    deadline = time.monotonic() + timeout
    handle.seek(0, os.SEEK_END)
    if handle.tell() == 0:
        handle.write(b"\0")
        handle.flush()
    while True:
        try:
            if os.name == "nt":
                import msvcrt
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            return
        except OSError:
            if time.monotonic() >= deadline:
                raise LockTimeout(
                    "another process is writing decisions; retry shortly")
            time.sleep(0.05)


def _release_os(handle) -> None:
    try:
        if os.name == "nt":
            import msvcrt
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    except OSError:
        pass


@contextmanager
def exclusive_lock(path: Path, timeout: float = 10.0):
    """Serialize a mutation across threads and processes."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    thread_guard = _thread_lock(path)
    if not thread_guard.acquire(timeout=timeout):
        raise LockTimeout("another request is writing; retry shortly")
    handle = None
    try:
        handle = open(path, "a+b")
        _acquire_os(handle, timeout)
        yield
    finally:
        if handle is not None:
            _release_os(handle)
            handle.close()
        thread_guard.release()
