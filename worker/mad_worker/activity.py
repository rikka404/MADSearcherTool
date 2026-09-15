"""Shared worker activity versus exclusive maintenance, released on process exit."""
import os
from contextlib import contextmanager

from .errors import UserError
from .owned_paths import checked


@contextmanager
def workspace_activity(workspace, *, exclusive=False):
    path = checked(workspace, workspace / ".activity.lock")
    with path.open("a+b") as stream:
        if os.name == "nt":
            import ctypes
            import msvcrt
            from ctypes import wintypes

            class Overlapped(ctypes.Structure):
                _fields_ = [("Internal", ctypes.c_size_t), ("InternalHigh", ctypes.c_size_t),
                            ("Offset", wintypes.DWORD), ("OffsetHigh", wintypes.DWORD), ("hEvent", wintypes.HANDLE)]

            kernel = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel.LockFileEx.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD,
                                         wintypes.DWORD, wintypes.DWORD, ctypes.POINTER(Overlapped)]
            kernel.LockFileEx.restype = wintypes.BOOL
            kernel.UnlockFileEx.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD,
                                           wintypes.DWORD, ctypes.POINTER(Overlapped)]
            kernel.UnlockFileEx.restype = wintypes.BOOL
            handle, overlapped = msvcrt.get_osfhandle(stream.fileno()), Overlapped()
            if not kernel.LockFileEx(handle, 1 | (2 if exclusive else 0), 0, 1, 0, ctypes.byref(overlapped)):
                error = ctypes.get_last_error()
                if error == 33:
                    raise UserError("工作区有任务正在运行或正在清理，请完成后重试。", "busy")
                raise OSError(error, "无法获取工作区活动锁")
            try:
                yield
            finally:
                kernel.UnlockFileEx(handle, 0, 1, 0, ctypes.byref(overlapped))
        else:
            import fcntl
            try:
                fcntl.flock(stream, (fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH) | fcntl.LOCK_NB)
            except BlockingIOError:
                raise UserError("工作区有任务正在运行或正在清理，请完成后重试。", "busy")
            try:
                yield
            finally:
                fcntl.flock(stream, fcntl.LOCK_UN)
