"""Windows job object that ends a managed ComfyUI process tree together with MediaStudio.

The job is created with JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE and its only handle
belongs to this process, so Windows terminates every process in the job when
MediaStudio exits for any reason, including a crash or a forced kill.

A venv ``python.exe`` is a launcher that starts the real interpreter as a
child. The managed process is therefore created suspended and assigned to the
job before it runs: children inherit the job only from that point on.
"""

from __future__ import annotations

import ctypes
import sys
from ctypes import wintypes

CREATE_SUSPENDED = 0x00000004
_JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
_JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9


class _IoCounters(ctypes.Structure):
    _fields_ = [
        ("ReadOperationCount", ctypes.c_ulonglong),
        ("WriteOperationCount", ctypes.c_ulonglong),
        ("OtherOperationCount", ctypes.c_ulonglong),
        ("ReadTransferCount", ctypes.c_ulonglong),
        ("WriteTransferCount", ctypes.c_ulonglong),
        ("OtherTransferCount", ctypes.c_ulonglong),
    ]


class _BasicLimitInformation(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", ctypes.c_int64),
        ("PerJobUserTimeLimit", ctypes.c_int64),
        ("LimitFlags", wintypes.DWORD),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", wintypes.DWORD),
        ("Affinity", ctypes.c_size_t),
        ("PriorityClass", wintypes.DWORD),
        ("SchedulingClass", wintypes.DWORD),
    ]


class _ExtendedLimitInformation(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", _BasicLimitInformation),
        ("IoInfo", _IoCounters),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


def _kernel32():
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateJobObjectW.argtypes = [wintypes.LPVOID, wintypes.LPCWSTR]
    kernel32.CreateJobObjectW.restype = wintypes.HANDLE
    kernel32.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD]
    kernel32.SetInformationJobObject.restype = wintypes.BOOL
    kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
    kernel32.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
    kernel32.TerminateJobObject.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    return kernel32


def _check(ok: bool, action: str) -> None:
    if not ok:
        raise ctypes.WinError(ctypes.get_last_error(), f"{action} failed")


class KillOnCloseJob:
    """A job whose processes end when its handle closes (explicitly or with this process)."""

    def __init__(self) -> None:
        if sys.platform != "win32":
            raise OSError("Job objects exist only on Windows")
        self._kernel32 = _kernel32()
        handle = self._kernel32.CreateJobObjectW(None, None)
        _check(bool(handle), "CreateJobObject")
        self._handle = handle
        info = _ExtendedLimitInformation()
        info.BasicLimitInformation.LimitFlags = _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        try:
            _check(
                self._kernel32.SetInformationJobObject(
                    handle, _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION, ctypes.byref(info), ctypes.sizeof(info)
                ),
                "SetInformationJobObject",
            )
        except OSError:
            self.close()
            raise

    def assign(self, process_handle: int) -> None:
        _check(self._kernel32.AssignProcessToJobObject(self._handle, process_handle), "AssignProcessToJobObject")

    def terminate(self, exit_code: int = 1) -> None:
        if self._handle:
            self._kernel32.TerminateJobObject(self._handle, exit_code)

    def close(self) -> None:
        handle, self._handle = self._handle, None
        if handle:
            self._kernel32.CloseHandle(handle)


def resume_process(process_handle: int) -> None:
    """Resume every thread of a process created with CREATE_SUSPENDED."""
    ntdll = ctypes.WinDLL("ntdll")
    ntdll.NtResumeProcess.argtypes = [wintypes.HANDLE]
    ntdll.NtResumeProcess.restype = ctypes.c_long
    status = ntdll.NtResumeProcess(process_handle)
    if status != 0:
        raise OSError(f"NtResumeProcess failed with status {status & 0xFFFFFFFF:#010x}")
