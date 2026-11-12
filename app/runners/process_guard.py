"""Process-lifetime guards that keep Silver from outliving the worker.

Each live Silver process holds one license. The worker disposes its instances
from an ``atexit`` hook, but on Windows that hook only runs when the worker is
asked to stop in a way Python can handle:

* ``Popen.terminate()`` on Windows is ``TerminateProcess``: no signal handler,
  no ``atexit``, no dispose. Silver children survive and keep their licenses.
* A console ``CTRL_BREAK_EVENT`` sent to a child started in its own process
  group arrives as ``SIGBREAK``, which the worker can turn into an orderly
  Huey shutdown (and therefore a clean pool dispose).

Two pieces are provided:

``request_stop`` / ``child_creationflags``
    The launcher starts the worker in a new process group and asks it to stop
    with ``CTRL_BREAK_EVENT`` (``SIGTERM`` on POSIX), so the worker's own
    shutdown path runs before any hard kill.

``KillOnCloseJob``
    A Windows Job Object with ``JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE``. Processes
    assigned to it, and every process they start afterwards (Silver included),
    are terminated by the kernel when the last handle to the job closes, which
    happens even when the owner is killed with ``TerminateProcess``. This is
    the backstop for the cases a graceful stop cannot cover.

Everything is best effort and a no-op off Windows: a guard that cannot be set
up is logged and skipped, never allowed to stop the platform from starting.
"""

from __future__ import annotations

import logging
import os
import signal
import subprocess
import sys

logger = logging.getLogger("silver.process_guard")

IS_WINDOWS = sys.platform.startswith("win")

# winnt.h / winbase.h constants.
_JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9
_JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
_PROCESS_TERMINATE = 0x0001
_PROCESS_SET_QUOTA = 0x0100


def child_creationflags() -> int:
    """``Popen`` creation flags for a child that must receive ``request_stop``.

    On Windows the child gets its own process group: ``CTRL_BREAK_EVENT`` can
    only target a process group, and a separate group also stops the console's
    Ctrl+C from reaching the child directly, so the launcher alone sequences
    the shutdown.
    """
    if IS_WINDOWS:
        return subprocess.CREATE_NEW_PROCESS_GROUP
    return 0


def request_stop(proc: subprocess.Popen) -> bool:
    """Ask *proc* to shut down in a way its own cleanup handlers observe.

    Returns ``True`` when the request was delivered. ``False`` means the caller
    must fall back to a hard stop (for example a launcher with no console, for
    which ``GenerateConsoleCtrlEvent`` is unavailable).
    """
    if proc.poll() is not None:
        return True
    try:
        if IS_WINDOWS:
            os.kill(proc.pid, signal.CTRL_BREAK_EVENT)
        else:
            proc.send_signal(signal.SIGTERM)
        return True
    except (OSError, ValueError) as exc:
        logger.warning("Could not deliver a graceful stop to pid %s: %s", proc.pid, exc)
        return False


def install_break_handler(handler) -> bool:
    """Route Windows ``SIGBREAK`` (``CTRL_BREAK_EVENT``) to *handler*.

    Python's default ``SIGBREAK`` action terminates the process without running
    ``atexit``, so the worker must claim it before it can shut down cleanly.
    """
    sigbreak = getattr(signal, "SIGBREAK", None)
    if sigbreak is None:
        return False
    signal.signal(sigbreak, handler)
    return True


class KillOnCloseJob:
    """A Windows Job Object whose processes die with its last handle.

    Keep the instance referenced for the owner's whole lifetime: the handle is
    deliberately never closed explicitly, so the kernel closes it when the
    owning process exits (cleanly or not) and terminates the job's processes.
    """

    def __init__(self, handle) -> None:
        self._handle = handle

    @classmethod
    def create(cls) -> "KillOnCloseJob | None":
        if not IS_WINDOWS:
            return None
        try:
            kernel32 = _kernel32()
            handle = kernel32.CreateJobObjectW(None, None)
            if not handle:
                raise _last_error("CreateJobObjectW")
            info = _ExtendedLimitInformation()
            info.BasicLimitInformation.LimitFlags = _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            import ctypes

            if not kernel32.SetInformationJobObject(
                    handle, _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
                    ctypes.byref(info), ctypes.sizeof(info)):
                error = _last_error("SetInformationJobObject")
                kernel32.CloseHandle(handle)
                raise error
            return cls(handle)
        except Exception:  # noqa: BLE001 - a missing guard must not stop startup
            logger.exception("Could not create a kill-on-close job object")
            return None

    def assign_current_process(self) -> bool:
        """Put the calling process (and its future children) into the job."""
        kernel32 = _kernel32()
        return self._assign(kernel32.GetCurrentProcess(), "current process")

    def assign_pid(self, pid: int) -> bool:
        """Put the process *pid* (and its future children) into the job."""
        kernel32 = _kernel32()
        process = kernel32.OpenProcess(_PROCESS_SET_QUOTA | _PROCESS_TERMINATE, False, int(pid))
        if not process:
            logger.warning("Could not open pid %s for job assignment: %s",
                           pid, _last_error("OpenProcess"))
            return False
        try:
            return self._assign(process, f"pid {pid}")
        finally:
            kernel32.CloseHandle(process)

    def _assign(self, process, label: str) -> bool:
        try:
            if _kernel32().AssignProcessToJobObject(self._handle, process):
                return True
            logger.warning("Could not assign %s to the kill-on-close job: %s",
                           label, _last_error("AssignProcessToJobObject"))
        except Exception:  # noqa: BLE001 - best effort
            logger.exception("Could not assign %s to the kill-on-close job", label)
        return False


def _kernel32():
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateJobObjectW.restype = wintypes.HANDLE
    kernel32.CreateJobObjectW.argtypes = (wintypes.LPVOID, wintypes.LPCWSTR)
    kernel32.SetInformationJobObject.restype = wintypes.BOOL
    kernel32.SetInformationJobObject.argtypes = (
        wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD)
    kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
    kernel32.AssignProcessToJobObject.argtypes = (wintypes.HANDLE, wintypes.HANDLE)
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    kernel32.GetCurrentProcess.argtypes = ()
    kernel32.CloseHandle.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
    return kernel32


def _last_error(call: str) -> OSError:
    import ctypes

    code = ctypes.get_last_error()
    return OSError(code, f"{call} failed: {ctypes.FormatError(code)}")


if IS_WINDOWS:
    import ctypes as _ctypes
    from ctypes import wintypes as _wintypes

    class _BasicLimitInformation(_ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", _wintypes.LARGE_INTEGER),
            ("PerJobUserTimeLimit", _wintypes.LARGE_INTEGER),
            ("LimitFlags", _wintypes.DWORD),
            ("MinimumWorkingSetSize", _ctypes.c_size_t),
            ("MaximumWorkingSetSize", _ctypes.c_size_t),
            ("ActiveProcessLimit", _wintypes.DWORD),
            ("Affinity", _ctypes.c_size_t),
            ("PriorityClass", _wintypes.DWORD),
            ("SchedulingClass", _wintypes.DWORD),
        ]

    class _IoCounters(_ctypes.Structure):
        _fields_ = [(name, _ctypes.c_ulonglong) for name in (
            "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
            "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]

    class _ExtendedLimitInformation(_ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", _BasicLimitInformation),
            ("IoInfo", _IoCounters),
            ("ProcessMemoryLimit", _ctypes.c_size_t),
            ("JobMemoryLimit", _ctypes.c_size_t),
            ("PeakProcessMemoryUsed", _ctypes.c_size_t),
            ("PeakJobMemoryUsed", _ctypes.c_size_t),
        ]
