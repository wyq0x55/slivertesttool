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

The platform startup guard is best effort. Owned execution requires Windows
job containment before descendants start; POSIX execution uses process groups.
"""

from __future__ import annotations

import logging
import os
import signal
import subprocess
import sys
import threading
import time

logger = logging.getLogger("silver.process_guard")

IS_WINDOWS = sys.platform.startswith("win")

# winnt.h / winbase.h constants.
_JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9
_JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
_PROCESS_TERMINATE = 0x0001
_PROCESS_SET_QUOTA = 0x0100


_shutting_down = threading.Event()
_pending_cleanup = {}
_cleanup_lock = threading.Lock()


def retain_cleanup(owner, cleanup) -> None:
    with _cleanup_lock:
        _pending_cleanup[id(owner)] = (owner, cleanup)


def release_cleanup(owner) -> None:
    with _cleanup_lock:
        _pending_cleanup.pop(id(owner), None)


def retry_pending_cleanup() -> None:
    with _cleanup_lock:
        pending = list(_pending_cleanup.values())
    for owner, cleanup in pending:
        try:
            cleanup()
        except Exception:
            logger.exception("Owned process cleanup retry failed")
        else:
            release_cleanup(owner)


def begin_shutdown() -> None:
    """Mark this process as shutting down (set before Silver is disposed).

    Disposing the pool force-exits instances that tasks are still running on.
    The resulting runner errors are caused by the shutdown, not by the test or
    the environment, so the runner checks this flag and leaves the attempt to
    startup recovery instead of finalising it as ERROR.
    """
    _shutting_down.set()


def is_shutting_down() -> bool:
    return _shutting_down.is_set()


def pid_is_running(pid: int) -> bool:
    """Best-effort check that a process with *pid* exists on this host.

    Errs on the side of "running" when the answer is unknown, so a caller never
    treats a live owner as dead.
    """
    pid = int(pid)
    if pid <= 0:
        return False
    if IS_WINDOWS:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        kernel32.GetExitCodeProcess.restype = wintypes.BOOL
        kernel32.GetExitCodeProcess.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
        kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
        handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            # ERROR_INVALID_PARAMETER: no such process. Anything else (e.g.
            # access denied) means it exists.
            return ctypes.get_last_error() != 87
        try:
            code = wintypes.DWORD()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return True
            return code.value == 259  # STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:
        return True
    return True


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

    Successful close confirms termination before releasing the handle. Failed
    cleanup retains the handle for retry. Owner exit also closes the handle
    through the kernel, terminating any remaining assigned processes.
    """

    def __init__(self, handle) -> None:
        self._handle = handle
        self._close_lock = threading.Lock()

    def close(self) -> None:
        """Terminate all assigned processes and release the job handle."""
        with self._close_lock:
            if self._handle is not None:
                import ctypes

                kernel32 = _kernel32()
                if not kernel32.TerminateJobObject(self._handle, 1):
                    raise _last_error("TerminateJobObject")
                deadline = time.monotonic() + 5
                while True:
                    accounting = _BasicAccountingInformation()
                    if not kernel32.QueryInformationJobObject(
                            self._handle, 1, ctypes.byref(accounting),
                            ctypes.sizeof(accounting), None):
                        raise _last_error("QueryInformationJobObject")
                    if accounting.ActiveProcesses == 0:
                        break
                    if time.monotonic() >= deadline:
                        raise TimeoutError("Owned process tree did not terminate")
                    time.sleep(0.01)
                if not kernel32.CloseHandle(self._handle):
                    raise _last_error("CloseHandle")
                self._handle = None

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


def require_process_job(pid: int) -> KillOnCloseJob | None:
    """Bind a gated child before it can start descendants; fail closed."""
    if not IS_WINDOWS:
        return None
    job = KillOnCloseJob.create()
    if job is None or not job.assign_pid(pid):
        if job is not None:
            job.close()
        raise OSError(f"Could not contain child process {pid}")
    return job


def kill_process_tree(pid: int, job: KillOnCloseJob | None = None) -> None:
    """Kill an owned tree even when its original parent has already exited."""
    if IS_WINDOWS:
        if job is None:
            raise OSError(f"Process {pid} has no Windows job ownership")
        job.close()
    else:
        try:
            os.killpg(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


def popen_owned(command, **kwargs) -> subprocess.Popen:
    """Start a contained command without a Windows spawn/assignment race."""
    retry_pending_cleanup()
    if not IS_WINDOWS:
        return subprocess.Popen(command, start_new_session=True, **kwargs)
    gate = (
        "import subprocess,sys; "
        "sys.stdin.buffer.read(1) == b'!' or sys.exit(1); "
        "sys.exit(subprocess.call(sys.argv[1:]))"
    )
    child = subprocess.Popen(
        [sys.executable, "-c", gate, *command], stdin=subprocess.PIPE, **kwargs)
    try:
        child._owned_job = require_process_job(child.pid)
        child.stdin.write(b"!")
        child.stdin.flush()
        child.stdin.close()
        child.stdin = None
        return child
    except BaseException:
        job = getattr(child, "_owned_job", None)
        try:
            if job is not None:
                job.close()
        except Exception:
            retain_cleanup(child, lambda: kill_process_tree(child.pid, job))
            logger.exception("Could not close failed child ownership")
        finally:
            actions = (child.kill, lambda: child.wait(timeout=2),
                       lambda: child.stdin.close() if child.stdin is not None else None)
            for action in actions:
                try:
                    action()
                except Exception:
                    logger.exception("Failed child cleanup stage")
        raise


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
    kernel32.TerminateJobObject.restype = wintypes.BOOL
    kernel32.TerminateJobObject.argtypes = (wintypes.HANDLE, wintypes.UINT)
    kernel32.QueryInformationJobObject.restype = wintypes.BOOL
    kernel32.QueryInformationJobObject.argtypes = (
        wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD, wintypes.LPVOID)
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

    class _BasicAccountingInformation(_ctypes.Structure):
        _fields_ = [
            ("TotalUserTime", _ctypes.c_longlong),
            ("TotalKernelTime", _ctypes.c_longlong),
            ("ThisPeriodTotalUserTime", _ctypes.c_longlong),
            ("ThisPeriodTotalKernelTime", _ctypes.c_longlong),
            ("TotalPageFaultCount", _wintypes.DWORD),
            ("TotalProcesses", _wintypes.DWORD),
            ("ActiveProcesses", _wintypes.DWORD),
            ("TotalTerminatedProcesses", _wintypes.DWORD),
        ]

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
