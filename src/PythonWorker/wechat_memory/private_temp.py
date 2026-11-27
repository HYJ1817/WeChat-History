"""Private temporary storage shared by the worker's database, media and PDF paths.

The native app starts every worker with a unique ``WECHAT_MEMORY_TEMP_ROOT``.
All plaintext that must touch disk lives below that root, so the UI can delete
the whole tree after a normal close, a request timeout, or a forced worker
termination.  When no root is supplied (direct source runs and tests), a
process-owned directory is created under ``%LOCALAPPDATA%`` and removed at exit.

A lock file in each root and each session lets a later worker distinguish an
abandoned directory from a live one without relying on age alone.  On Windows
the lock is an ``msvcrt.locking`` byte lock; on POSIX it is ``flock``.  Both are
released automatically when the owning process exits.
"""
from __future__ import annotations

import atexit
import os
import shutil
import tempfile
import threading
import time
from pathlib import Path
from typing import IO

WORKER_TEMP_ENV = "WECHAT_MEMORY_TEMP_ROOT"
SESSION_PREFIX = "session-"
WORKER_PREFIX = "worker-"
ROOT_LEASE_NAME = "worker.lock"
SESSION_LEASE_NAME = "session.lock"
# Directories from older releases have no lock file; keep the old grace period
# for those, but never use age to judge a locked session.
LEGACY_STALE_SECONDS = 6 * 60 * 60

_worker_root: Path | None = None
_worker_lease: IO[bytes] | None = None
_worker_lock = threading.Lock()


def default_temp_base() -> Path:
    local = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
    return local / "WechatMemoryNative" / "worker-temp"


def _try_lock(path: Path) -> IO[bytes] | None:
    """Open ``path`` and take a non-blocking exclusive lock, or return None."""
    try:
        handle = path.open("a+b")
    except OSError:
        return None
    try:
        if os.name == "nt":
            import msvcrt

            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write(b"1")
                handle.flush()
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        return handle
    except OSError:
        handle.close()
        return None


def _unlock(handle: IO[bytes] | None) -> None:
    if handle is None:
        return
    try:
        try:
            handle.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
    finally:
        try:
            handle.close()
        except OSError:
            pass


def claim_lease(path: Path) -> IO[bytes] | None:
    """Take a non-blocking exclusive lease on ``path``; None means it is held."""
    return _try_lock(path)


def release_lease(handle: IO[bytes] | None) -> None:
    """Release and close a lease returned by :func:`claim_lease`."""
    _unlock(handle)


def lease_active(path: Path) -> bool | None:
    """Whether another process owns ``path``.

    ``None`` means the lease file does not exist (a legacy directory), while
    ``False`` means the file exists but no process holds its lock.
    """
    try:
        if not path.is_file():
            return None
    except OSError:
        return True
    handle = _try_lock(path)
    if handle is None:
        return True
    _unlock(handle)
    return False


def remove_tree(path: Path) -> bool:
    """Best-effort recursive delete with short retries for closing handles."""
    for attempt in range(5):
        shutil.rmtree(path, ignore_errors=True)
        try:
            if not path.exists():
                return True
        except OSError:
            pass
        time.sleep(0.1 * (attempt + 1))
    return False


def _legacy_is_stale(path: Path) -> bool:
    try:
        return path.stat().st_mtime < time.time() - LEGACY_STALE_SECONDS
    except OSError:
        return False


def _remove_if_unleased(directory: Path, lease_name: str) -> None:
    lease = directory / lease_name
    state = lease_active(lease)
    if state is True:
        return
    if state is None:
        if not _legacy_is_stale(directory):
            return
    # No live owner.  The lease file is inside the tree, so close it before the
    # delete; no process ever claims an existing session root.
    remove_tree(directory)


def cleanup_legacy_pdf_temp() -> None:
    """Remove PDF page directories left in %TEMP% by releases before 0.1.x.

    New exports write page images below the worker's private root.  Only this
    application's own prefix is touched, and only after the old grace period.
    """
    try:
        children = list(Path(tempfile.gettempdir()).iterdir())
    except OSError:
        return
    cutoff = time.time() - LEGACY_STALE_SECONDS
    for child in children:
        try:
            if not child.is_dir() or not child.name.startswith("wechat-memory-pdf-"):
                continue
            if child.stat().st_mtime < cutoff:
                remove_tree(child)
        except OSError:
            continue


def cleanup_abandoned_roots(base: Path | None = None, current: Path | None = None) -> None:
    """Remove worker/session roots that have no live lease.

    Called once when a worker starts.  Age alone is only used for directories
    created by older releases, which have no lease file.
    """
    base = base or default_temp_base()
    try:
        children = list(base.iterdir())
    except OSError:
        return
    for child in children:
        try:
            if not child.is_dir():
                continue
            if current is not None and child.resolve() == current.resolve():
                continue
            if child.name.startswith(WORKER_PREFIX):
                _remove_if_unleased(child, ROOT_LEASE_NAME)
            elif child.name.startswith(SESSION_PREFIX):
                _remove_if_unleased(child, SESSION_LEASE_NAME)
        except (OSError, RuntimeError, ValueError):
            continue


def _discard_root(root: Path) -> None:
    global _worker_lease
    _unlock(_worker_lease)
    _worker_lease = None
    remove_tree(root)


def get_worker_temp_root() -> Path:
    """The root every module in this worker may use for private temp files."""
    global _worker_root, _worker_lease
    with _worker_lock:
        if _worker_root is not None:
            return _worker_root
        configured = os.environ.get(WORKER_TEMP_ENV, "").strip()
        if configured:
            root = Path(configured).expanduser()
            try:
                root.mkdir(parents=True, exist_ok=True)
            except OSError:
                root = default_temp_base() / f"{WORKER_PREFIX}{os.getpid()}"
                root.mkdir(parents=True, exist_ok=True)
        else:
            base = default_temp_base()
            base.mkdir(parents=True, exist_ok=True)
            root = Path(tempfile.mkdtemp(prefix=f"{WORKER_PREFIX}{os.getpid()}-", dir=base))
        lease = _try_lock(root / ROOT_LEASE_NAME)
        if lease is None:
            # The configured root is already owned (for example, a second
            # worker was pointed at the same directory).  Use a fresh child so
            # neither worker can delete the other's plaintext.
            try:
                root = Path(tempfile.mkdtemp(prefix=f"{WORKER_PREFIX}{os.getpid()}-", dir=root))
                lease = _try_lock(root / ROOT_LEASE_NAME)
            except OSError:
                lease = None
        if lease is None:
            raise OSError("cannot claim a private worker temporary root")
        _worker_root = root
        _worker_lease = lease
        atexit.register(_discard_root, root)
        return root
