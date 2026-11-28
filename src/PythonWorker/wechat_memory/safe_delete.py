"""Fail-closed, handle-bound deletion of a confirmed task tree on local NTFS.

This is not a general rmtree replacement. Keep the context open throughout the
*final* metadata/tree fingerprint check and deletion. Every ancestor from the
volume root, every directory and every regular file stays open without
FILE_SHARE_WRITE or FILE_SHARE_DELETE. Pre-existing writers/mappings, rename
handles, unsupported filesystems/APIs and resource exhaustion refuse cleanup.
Readers use duplicates of those same handles, never a reopened pathname.

Only the enumerated handles are deleted, bottom up, with FileDispositionInfo.
A concurrently added child is never adopted/deleted: it invalidates the plan or
makes its parent's non-recursive disposition fail. Deletion is not atomic; an
I/O error or a late new child may leave a partially removed *confirmed* job.
No chmod, ACL changes, POSIX-delete semantics, path-based delete or fallback.

References: Microsoft Learn CreateFileW (sharing and OPEN_REPARSE_POINT),
SetFileInformationByHandle / FILE_DISPOSITION_INFO. Only stdlib ctypes is used.
Kernel/administrator/volume-device writes are outside this protection boundary.
NTFS alternate data streams are separate streams: these handles lock the unnamed
(default) stream, not a concurrently-created named stream. The application does
not write ADS, but callers must not treat ADS as covered by this guarantee.
"""
from __future__ import annotations

import ctypes
import os
import stat
from pathlib import Path
from typing import BinaryIO


class SafeDeleteError(Exception):
    """A fixed caller-safe skip reason, never native exception text or paths."""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


def _key(info: os.stat_result) -> tuple[int, ...]:
    return (
        info.st_dev, info.st_ino, info.st_mode, info.st_nlink,
        info.st_size, info.st_mtime_ns, info.st_ctime_ns,
    )


def _check_info(info: os.stat_result) -> None:
    if (
        getattr(info, "st_file_attributes", 0) & 0x400
        or not (stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode))
        or (stat.S_ISREG(info.st_mode) and info.st_nlink != 1)
    ):
        raise SafeDeleteError("unsafe_path")


class _WindowsApi:
    """Small, explicitly typed Win32 surface; constructed only on Windows."""

    def __init__(self) -> None:
        from ctypes import wintypes as w

        self.dll = ctypes.WinDLL("kernel32", use_last_error=True)
        signatures = {
            "CreateFileW": ([w.LPCWSTR, w.DWORD, w.DWORD, w.LPVOID,
                             w.DWORD, w.DWORD, w.HANDLE], w.HANDLE),
            "CloseHandle": ([w.HANDLE], w.BOOL),
            "GetFileType": ([w.HANDLE], w.DWORD),
            "GetDriveTypeW": ([w.LPCWSTR], w.UINT),
            "GetFinalPathNameByHandleW": ([w.HANDLE, w.LPWSTR, w.DWORD, w.DWORD], w.DWORD),
            "GetVolumeInformationByHandleW": (
                [w.HANDLE, w.LPWSTR, w.DWORD, w.LPDWORD, w.LPDWORD,
                 w.LPDWORD, w.LPWSTR, w.DWORD], w.BOOL),
            "SetFileInformationByHandle": ([w.HANDLE, ctypes.c_int, w.LPVOID, w.DWORD], w.BOOL),
        }
        for name, (args, result) in signatures.items():
            function = getattr(self.dll, name)
            function.argtypes = args
            function.restype = result

    def open(self, native_path: str, *, deletable: bool) -> int:
        # GENERIC_READ is intentional even for ancestors: attribute-only access
        # does not participate in all share-access checks. Never share W or D.
        handle = self.dll.CreateFileW(
            native_path, 0x80000000 | (0x10000 if deletable else 0),
            0x1, None, 3, 0x02000000 | 0x00200000, None,
        )
        if handle == ctypes.c_void_p(-1).value:
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            if self.dll.GetFileType(handle) != 1:  # FILE_TYPE_DISK
                raise SafeDeleteError("unsafe_path")
            return handle
        except BaseException:
            self.dll.CloseHandle(handle)
            raise

    def final_path(self, handle: int) -> str:
        buffer = ctypes.create_unicode_buffer(32768)
        count = self.dll.GetFinalPathNameByHandleW(handle, buffer, len(buffer), 0x1)
        if not count or count >= len(buffer) or not buffer.value.startswith("\\\\?\\Volume{"):
            raise SafeDeleteError("safe_delete_unavailable")
        return buffer.value.rstrip("\\")

    def volume_root(self, handle: int) -> str:
        filesystem = ctypes.create_unicode_buffer(261)
        if not self.dll.GetVolumeInformationByHandleW(
            handle, None, 0, None, None, None, filesystem, len(filesystem),
        ) or filesystem.value != "NTFS":
            raise SafeDeleteError("safe_delete_unavailable")
        # Bind subsequent opens to a volume GUID, not a mutable DOS drive alias.
        return self.final_path(handle)

    def dispose(self, handle: int) -> None:
        # FILE_DISPOSITION_INFO contains a one-byte BOOLEAN, not a Win32 BOOL.
        delete_file = ctypes.c_ubyte(1)
        if not self.dll.SetFileInformationByHandle(handle, 4, ctypes.byref(delete_file), 1):
            raise ctypes.WinError(ctypes.get_last_error())


class ProtectedTree:
    """Retain identity/content protection from final revalidation to deletion."""

    def __init__(self, directory: Path) -> None:
        self.directory = directory
        self._fds: dict[Path, int] = {}
        self._infos: dict[Path, os.stat_result] = {}
        self._children: dict[Path, tuple[str, ...]] = {}
        self._api: _WindowsApi | None = None
        self._native_root = ""
        self._anchor = Path(directory.anchor)

    def __enter__(self) -> ProtectedTree:
        if os.name != "nt":
            # POSIX advisory locks / fd-safe rmtree do not exclude outside
            # content writers. Do not pretend they provide the same guarantee.
            raise SafeDeleteError("safe_delete_unavailable")
        try:
            if (
                not self.directory.is_absolute()
                or len(self.directory.drive) != 2 or self.directory.drive[1] != ":"
                or any(part in {".", ".."} for part in self.directory.parts)
            ):
                raise SafeDeleteError("unsafe_path")
            self._api = _WindowsApi()
            if self._api.dll.GetDriveTypeW(str(self._anchor)) != 3:  # DRIVE_FIXED
                raise SafeDeleteError("safe_delete_unavailable")
            self._acquire(self._anchor, "\\\\?\\" + str(self._anchor), deletable=False)
            self._native_root = self._api.volume_root(self._handle(self._anchor))
            for path in (*reversed(self.directory.parents), self.directory):
                if path == self._anchor:
                    continue
                self._acquire(path, self._native_path(path), deletable=path == self.directory)
                if not stat.S_ISDIR(self._infos[path].st_mode):
                    raise SafeDeleteError("unsafe_path")
            return self
        except BaseException as exc:
            self.close()
            if isinstance(exc, OSError):
                raise SafeDeleteError(self._open_failure(exc)) from None
            if isinstance(exc, (AttributeError, NotImplementedError)):
                raise SafeDeleteError("safe_delete_unavailable") from None
            raise

    def __exit__(self, *_args) -> None:
        self.close()

    @staticmethod
    def _open_failure(exc: OSError) -> str:
        return "in_use" if getattr(exc, "winerror", None) in {32, 33} else "protection_failed"

    def _native_path(self, path: Path) -> str:
        return self._native_root + "\\" + str(path.relative_to(self._anchor))

    def _handle(self, path: Path) -> int:
        import msvcrt

        return msvcrt.get_osfhandle(self._fds[path])

    def _acquire(self, path: Path, native: str, *, deletable: bool) -> None:
        import msvcrt

        assert self._api is not None
        handle = self._api.open(native, deletable=deletable)
        try:
            fd = msvcrt.open_osfhandle(handle, os.O_RDONLY | os.O_BINARY)
        except BaseException:
            self._api.dll.CloseHandle(handle)
            raise
        # Ownership has passed to the CRT fd. Register it before any inspection,
        # so even a failed stat/type check is released by the context's finally.
        self._fds[path] = fd
        info = os.fstat(fd)
        _check_info(info)
        if self._native_root and self._api.final_path(handle).casefold() != native.rstrip("\\").casefold():
            # OPEN_REPARSE_POINT affects the last component only. Verify the
            # opened object's canonical name as well, so an ancestor redirected
            # *during acquisition* cannot bind an external object to our plan.
            raise SafeDeleteError("unsafe_path")
        self._infos[path] = info

    def stat(self, path: Path) -> os.stat_result:
        if path not in self._fds:
            if not path.is_relative_to(self.directory) or path.parent not in self._fds:
                raise SafeDeleteError("unsafe_path")
            try:
                self._acquire(path, self._native_path(path), deletable=True)
            except OSError as exc:
                raise SafeDeleteError(self._open_failure(exc)) from None
        info = os.fstat(self._fds[path])
        _check_info(info)
        return info

    def open_file(self, path: Path) -> BinaryIO:
        # Never open a path while protected. In particular, ordinary CRT opens
        # do not share DELETE and would conflict with our own deletion handle.
        if path not in self._fds or not stat.S_ISREG(self._infos[path].st_mode):
            raise SafeDeleteError("unsafe_path")
        fd = os.dup(self._fds[path])
        try:
            stream = os.fdopen(fd, "rb")
        except BaseException:
            os.close(fd)
            raise
        try:
            stream.seek(0)
        except BaseException:
            stream.close()
            raise
        return stream

    def names(self, path: Path) -> tuple[str, ...]:
        if path not in self._fds or not stat.S_ISDIR(self._infos[path].st_mode):
            raise SafeDeleteError("unsafe_path")
        with os.scandir(self._native_path(path)) as entries:
            names = tuple(sorted(entry.name for entry in entries))
        before = self._children.setdefault(path, names)
        if before != names:
            raise SafeDeleteError("changed")
        return names

    def delete(self) -> None:
        assert self._api is not None
        owned = {path for path in self._fds if path.is_relative_to(self.directory)}
        children: dict[Path, set[Path]] = {}
        for path in owned:
            if path != self.directory:
                children.setdefault(path.parent, set()).add(path)
        # No new enumeration may enlarge the deletion plan. Check both held
        # identities and exact membership before the first irreversible step.
        for path in owned:
            info = self.stat(path)
            if _key(info) != _key(self._infos[path]):
                raise SafeDeleteError("changed")
            if stat.S_ISDIR(info.st_mode):
                if path not in self._children:
                    raise SafeDeleteError("changed")
                if {path / name for name in self.names(path)} != children.get(path, set()):
                    raise SafeDeleteError("changed")
        # Preserve readable metadata as long as possible on partial failure.
        metadata = {self.directory / "manifest.json", self.directory / "state.json"}
        order = sorted(owned - metadata - {self.directory}, key=lambda p: len(p.parts), reverse=True)
        order.extend(sorted(owned & metadata))
        order.append(self.directory)
        for path in order:
            try:
                self._api.dispose(self._handle(path))
                os.close(self._fds.pop(path))
            except OSError:
                raise SafeDeleteError("delete_failed") from None
        # A foreign reader can keep a directory delete-pending. A fresh
        # replacement must also never be treated as successful deletion.
        try:
            os.lstat(self._native_path(self.directory))
        except FileNotFoundError:
            return
        except OSError:
            pass
        raise SafeDeleteError("delete_failed")

    def close(self) -> None:
        first_error = None
        for path in reversed(tuple(self._fds)):
            try:
                os.close(self._fds.pop(path))
            except OSError as exc:
                first_error = first_error or exc
        if first_error is not None:
            raise SafeDeleteError("protection_failed") from None
