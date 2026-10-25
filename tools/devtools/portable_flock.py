"""fcntl.flock on every host: the same call, operations and errors on POSIX and on Windows.

POSIX: fcntl.flock itself. Windows has no fcntl; LockFileEx and UnlockFileEx over the whole
file give the same semantics: a shared or exclusive lock per open handle, released when the
handle closes, and a non-blocking request that cannot be granted raises BlockingIOError.
"""

from __future__ import annotations

import os
from typing import IO

if os.name != "nt":
    from fcntl import LOCK_EX, LOCK_NB, LOCK_SH, LOCK_UN, flock
else:
    import ctypes
    import errno
    import msvcrt
    from ctypes import wintypes

    LOCK_SH, LOCK_EX, LOCK_NB, LOCK_UN = 1, 2, 4, 8  # fcntl's values
    _EXCLUSIVE = 0x2  # LOCKFILE_EXCLUSIVE_LOCK
    _FAIL_IMMEDIATELY = 0x1  # LOCKFILE_FAIL_IMMEDIATELY
    _ERROR_LOCK_VIOLATION = 33
    _ERROR_NOT_LOCKED = 158
    _WHOLE = 0xFFFFFFFF  # bytes to lock, low and high words: the whole file

    class _Overlapped(ctypes.Structure):
        _fields_ = [
            ("Internal", ctypes.c_size_t),
            ("InternalHigh", ctypes.c_size_t),
            ("Offset", wintypes.DWORD),
            ("OffsetHigh", wintypes.DWORD),
            ("hEvent", wintypes.HANDLE),
        ]

    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _kernel32.LockFileEx.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.POINTER(_Overlapped),
    ]
    _kernel32.LockFileEx.restype = wintypes.BOOL
    _kernel32.UnlockFileEx.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.POINTER(_Overlapped),
    ]
    _kernel32.UnlockFileEx.restype = wintypes.BOOL

    def flock(file: IO | int, operation: int) -> None:
        """fcntl.flock(file, operation) with LockFileEx."""
        fd = file if isinstance(file, int) else file.fileno()
        handle = msvcrt.get_osfhandle(fd)
        overlapped = _Overlapped()
        if operation & LOCK_UN:
            if not _kernel32.UnlockFileEx(
                handle, 0, _WHOLE, _WHOLE, ctypes.byref(overlapped)
            ):
                error = ctypes.get_last_error()
                if (
                    error != _ERROR_NOT_LOCKED
                ):  # flock: unlocking an unlocked file is fine
                    raise ctypes.WinError(error)
            return
        flags = (_EXCLUSIVE if operation & LOCK_EX else 0) | (
            _FAIL_IMMEDIATELY if operation & LOCK_NB else 0
        )
        if not _kernel32.LockFileEx(
            handle, flags, 0, _WHOLE, _WHOLE, ctypes.byref(overlapped)
        ):
            error = ctypes.get_last_error()
            if error == _ERROR_LOCK_VIOLATION:
                raise BlockingIOError(errno.EAGAIN, "the lock is held")
            raise ctypes.WinError(error)


__all__ = ["LOCK_EX", "LOCK_NB", "LOCK_SH", "LOCK_UN", "flock"]
