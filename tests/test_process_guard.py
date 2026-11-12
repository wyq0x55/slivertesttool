"""Worker shutdown must reach the worker's own Silver cleanup, or kill Silver.

On Windows ``Popen.terminate()`` is ``TerminateProcess``: the worker's atexit
pool dispose never runs and Silver keeps its licenses. The launcher therefore
asks the worker to stop with CTRL_BREAK_EVENT, and both processes sit in
kill-on-close job objects so a hard kill still takes Silver with it.
"""
from __future__ import annotations

import signal
import subprocess
import sys
import textwrap
from pathlib import Path
from unittest.mock import Mock

import pytest

from app.runners import process_guard

windows_only = pytest.mark.skipif(not process_guard.IS_WINDOWS, reason="Windows process semantics")


def test_child_gets_its_own_process_group_on_windows_only():
    expected = subprocess.CREATE_NEW_PROCESS_GROUP if process_guard.IS_WINDOWS else 0
    assert process_guard.child_creationflags() == expected


def test_request_stop_uses_ctrl_break_on_windows_and_sigterm_elsewhere(monkeypatch):
    proc = Mock(pid=4321)
    proc.poll.return_value = None
    kills = []
    monkeypatch.setattr(process_guard.os, "kill", lambda pid, sig: kills.append((pid, sig)))

    assert process_guard.request_stop(proc) is True
    if process_guard.IS_WINDOWS:
        assert kills == [(4321, signal.CTRL_BREAK_EVENT)]
        proc.send_signal.assert_not_called()
    else:
        proc.send_signal.assert_called_once_with(signal.SIGTERM)
    proc.terminate.assert_not_called()


def test_request_stop_reports_an_undeliverable_request(monkeypatch):
    proc = Mock(pid=4321)
    proc.poll.return_value = None
    proc.send_signal.side_effect = OSError("no console")

    def no_console(*_args):
        raise OSError("no console")

    monkeypatch.setattr(process_guard.os, "kill", no_console)
    assert process_guard.request_stop(proc) is False


def test_request_stop_is_a_no_op_for_an_exited_child():
    proc = Mock(pid=1)
    proc.poll.return_value = 0
    assert process_guard.request_stop(proc) is True
    proc.send_signal.assert_not_called()


def test_job_object_is_not_created_off_windows(monkeypatch):
    monkeypatch.setattr(process_guard, "IS_WINDOWS", False)
    assert process_guard.KillOnCloseJob.create() is None


def _wait_for_exit(pid: int, timeout_ms: int) -> bool:
    import ctypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.restype = ctypes.c_void_p
    handle = kernel32.OpenProcess(0x00100000, False, pid)  # SYNCHRONIZE
    if not handle:
        return True  # already gone
    try:
        return kernel32.WaitForSingleObject(ctypes.c_void_p(handle), timeout_ms) == 0
    finally:
        kernel32.CloseHandle(ctypes.c_void_p(handle))


@windows_only
def test_closing_the_job_kills_the_process_and_children_it_starts_later():
    # The parent starts its child only after joining the job, like the worker
    # launching Silver after binding itself.
    script = textwrap.dedent("""
        import subprocess, sys, time
        sys.stdin.readline()
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
        print(child.pid, flush=True)
        time.sleep(120)
    """)
    parent = subprocess.Popen([sys.executable, "-c", script], stdin=subprocess.PIPE,
                              stdout=subprocess.PIPE, text=True)
    child_pid = None
    try:
        job = process_guard.KillOnCloseJob.create()
        assert job is not None
        assert job.assign_pid(parent.pid)
        parent.stdin.write("go\n")
        parent.stdin.flush()
        child_pid = int(parent.stdout.readline())

        import ctypes

        ctypes.WinDLL("kernel32").CloseHandle(ctypes.c_void_p(job._handle))
        assert parent.wait(timeout=10) is not None
        assert _wait_for_exit(child_pid, 10000)
    finally:
        if parent.poll() is None:
            parent.kill()
        if child_pid is not None and not _wait_for_exit(child_pid, 0):
            subprocess.run(["taskkill", "/F", "/PID", str(child_pid)], check=False,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


@windows_only
def test_ctrl_break_reaches_the_childs_exit_hooks(tmp_path):
    marker = tmp_path / "atexit.txt"
    script = textwrap.dedent(f"""
        import atexit, threading, time
        from app.runners import process_guard
        stop = threading.Event()
        atexit.register(lambda: open({str(marker)!r}, "w").write("disposed"))
        process_guard.install_break_handler(lambda signum, frame: stop.set())
        print("ready", flush=True)
        while not stop.wait(0.1):
            pass
    """)
    child = subprocess.Popen([sys.executable, "-c", script], stdout=subprocess.PIPE, text=True,
                             creationflags=process_guard.child_creationflags(),
                             cwd=str(Path(__file__).resolve().parent.parent))
    try:
        assert child.stdout.readline().strip() == "ready"
        if not process_guard.request_stop(child):
            pytest.skip("this test process has no console to deliver CTRL_BREAK_EVENT")
        assert child.wait(timeout=20) == 0
        assert marker.read_text() == "disposed"
    finally:
        if child.poll() is None:
            child.kill()


@windows_only
def test_ctrl_break_stops_a_huey_consumer_and_runs_atexit(tmp_path):
    # The same wiring as run_worker.main(): Huey's stop handler on SIGBREAK.
    marker = tmp_path / "pool-shutdown.txt"
    script = textwrap.dedent(f"""
        import atexit, threading
        from huey import MemoryHuey
        from app.runners import process_guard
        atexit.register(lambda: open({str(marker)!r}, "w").write("disposed"))
        huey = MemoryHuey("guard-test")
        consumer = huey.create_consumer(workers=2, worker_type="thread")
        process_guard.install_break_handler(consumer._handle_stop_signal)
        threading.Timer(0.5, lambda: print("ready", flush=True)).start()
        consumer.run()
    """)
    child = subprocess.Popen([sys.executable, "-c", script], stdout=subprocess.PIPE, text=True,
                             creationflags=process_guard.child_creationflags(),
                             cwd=str(Path(__file__).resolve().parent.parent))
    try:
        assert child.stdout.readline().strip() == "ready"
        if not process_guard.request_stop(child):
            pytest.skip("this test process has no console to deliver CTRL_BREAK_EVENT")
        assert child.wait(timeout=30) == 0
        assert marker.read_text() == "disposed"
    finally:
        if child.poll() is None:
            child.kill()


def _dead_pid() -> int:
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait(timeout=30)
    return proc.pid


def test_pid_liveness():
    import os

    assert process_guard.pid_is_running(os.getpid())
    assert not process_guard.pid_is_running(_dead_pid())
    assert not process_guard.pid_is_running(0)


def test_shutdown_flag(monkeypatch):
    monkeypatch.setattr(process_guard, "_shutting_down", process_guard.threading.Event())
    assert not process_guard.is_shutting_down()
    process_guard.begin_shutdown()
    assert process_guard.is_shutting_down()
