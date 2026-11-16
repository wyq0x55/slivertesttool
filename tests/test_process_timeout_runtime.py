"""Exercise real OS containment with fake native API and bridge processes."""

import os
import subprocess
import sys
import textwrap
import threading
import time
from pathlib import Path

import pytest

from app.runners.process_guard import pid_is_running
from app.runners.silver_runner import (
    RunContext, RunnerCancelled, RunnerError, SilverRunner, _SilverSession,
)
from app.services.ut_pipeline.bridge import BridgeSettings, UtAgentBridge


def assert_stopped(pid):
    def alive():
        if sys.platform.startswith("linux"):
            status = Path(f"/proc/{pid}/stat")
            try:
                if status.read_text().rsplit(")", 1)[1].split()[0] == "Z":
                    return False
            except FileNotFoundError:
                return False
        return pid_is_running(pid)

    if os.name == "nt":
        assert not alive()
        return
    deadline = time.monotonic() + 5
    while alive() and time.monotonic() < deadline:
        time.sleep(0.02)
    assert not alive()


@pytest.fixture
def native_api(tmp_path, monkeypatch):
    packages = tmp_path / "common" / "ext-tools"
    if sys.platform.startswith("linux"):
        packages /= "linux64"
    packages /= "python3/lib/site-packages"
    module = packages / "synopsys/silver/remotescripting"
    module.mkdir(parents=True)
    (packages / "Pyro4.py").write_text(
        "from types import SimpleNamespace\nconfig = SimpleNamespace()\n",
        encoding="utf-8")
    (module / "remotesilverapi.py").write_text(textwrap.dedent("""
        import os, subprocess, sys, time
        from pathlib import Path

        class LocalSilverNative:
            def __init__(self, **options):
                self.process = subprocess.Popen(
                    [sys.executable, "-c", "import time; time.sleep(120)"])
                Path(os.environ["FAKE_SILVER_PID"]).write_text(str(self.process.pid))
                self.pause("launch")

            def pause(self, name):
                Path(os.environ["FAKE_SILVER_PID"] + "." + name).touch()
                if os.environ.get("FAKE_SILVER_HANG") == name:
                    time.sleep(120)
                if os.environ.get("FAKE_SILVER_DELAY"):
                    time.sleep(float(os.environ["FAKE_SILVER_DELAY"]))

            def open(self, *args):
                self.pause("open")

            def set_silver_property(self, *args):
                self.pause("property")

            def run_until(self, *args, **kwargs):
                self.pause("run")
                return True

            def clear(self):
                self.pause("clear")

            def exit(self, **kwargs):
                self.pause("exit")
                self.process.kill()
                self.process.wait(timeout=5)

            def add_module(self, **kwargs):
                self.pause("add")
                return "module-id"

            def set_module_property(self, *args):
                self.pause("module")

            def save(self, path):
                self.pause("save")
                Path(path).write_text("<workspace/>")
    """), encoding="utf-8")
    marker = tmp_path / "native.pid"
    monkeypatch.setenv("SILVER_HOME", str(tmp_path))
    monkeypatch.setenv("FAKE_SILVER_PID", str(marker))
    monkeypatch.setenv("EXECUTION_TIMEOUT", "2")
    return tmp_path, marker


@pytest.mark.parametrize("phase", ["launch", "open", "property", "run", "clear", "exit"])
def test_silver_hang_kills_native_tree(native_api, monkeypatch, phase):
    root, marker = native_api
    monkeypatch.setenv("FAKE_SILVER_HANG", phase)
    started = time.monotonic()
    session = None
    try:
        with pytest.raises(RunnerError, match="^timeout$"):
            session = _SilverSession(
                str(root), {}, time.monotonic() + 2)
            method, args = {
                "open": ("open", ("model.sil",)),
                "property": ("set_silver_property", ("name", True)),
                "run": ("run_until", ("condition",)),
                "clear": ("clear", ()),
                "exit": ("exit", ()),
            }.get(phase, ("clear", ()))
            getattr(session, method)(*args)
        assert time.monotonic() - started < 8
        assert marker.is_file()
        assert_stopped(int(marker.read_text()))
        if session is not None:
            assert session.process.poll() is not None
    finally:
        if session is not None:
            session.stop()


def test_silver_calls_share_one_deadline(native_api, monkeypatch):
    root, marker = native_api
    monkeypatch.setenv("FAKE_SILVER_DELAY", "0.4")
    session = _SilverSession(str(root), {}, time.monotonic() + 2)
    try:
        with pytest.raises(RunnerError, match="^timeout$"):
            for _ in range(8):
                session.clear()
        assert_stopped(int(marker.read_text()))
    finally:
        session.stop()


@pytest.mark.parametrize("pooled", [False, True])
def test_runner_execution_timeout_reaps_session(native_api, monkeypatch, pooled):
    root, marker = native_api
    model = root / "model.sil"
    model.touch()
    context = RunContext("TC1", root, root / "logs", model, False, 2)
    monkeypatch.setenv("FAKE_SILVER_HANG", "run")
    runner = SilverRunner()
    monkeypatch.setattr(runner, "prepare_run_sil", lambda _context: model)
    sessions = []
    context.on_start = sessions.append
    try:
        if pooled:
            sessions.append(runner.launch(model, False, root / "console.log"))
        with pytest.raises(RunnerError, match="^timeout$"):
            if pooled:
                runner.configure_and_run(sessions[0], context)
            else:
                runner.run(context)
        assert sessions[0].process.poll() is not None
        assert_stopped(int(marker.read_text()))
    finally:
        for session in sessions:
            session.stop()


@pytest.mark.parametrize("phase", ["open", "property", "run"])
def test_force_stop_unblocks_active_call_as_cancellation(native_api, monkeypatch, phase):
    root, marker = native_api
    monkeypatch.setenv("FAKE_SILVER_HANG", phase)
    session = _SilverSession(str(root), {}, time.monotonic() + 10)
    context = RunContext("TC1", root, root, root / "model.sil", False, 10)
    failures = []
    runner = SilverRunner()
    monkeypatch.setattr(runner, "prepare_run_sil", lambda _context: context.sil_path)

    def execute():
        try:
            if phase == "open":
                runner.configure_and_run(session, context)
            else:
                runner._execute(session, context)
        except BaseException as exc:
            failures.append(exc)

    thread = threading.Thread(target=execute)
    thread.start()
    try:
        deadline = time.monotonic() + 3
        phase_marker = marker.with_suffix(f".pid.{phase}")
        while not phase_marker.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        assert phase_marker.exists()
        context.cancel_event.set()
        SilverRunner.force_stop(session)
        thread.join(timeout=5)
        assert not thread.is_alive()
        assert len(failures) == 1 and isinstance(failures[0], RunnerCancelled)
        assert_stopped(int(marker.read_text()))
    finally:
        session.stop()
        thread.join(timeout=5)


@pytest.mark.parametrize("phase", ["add", "save"])
def test_generate_sil_timeout_cleans_native_process(native_api, monkeypatch, phase):
    root, marker = native_api
    monkeypatch.setenv("FAKE_SILVER_HANG", phase)
    with pytest.raises(RunnerError, match="^timeout$"):
        SilverRunner().generate_sil(root / "generated.sil", "host.dll", "host.sbs")
    assert_stopped(int(marker.read_text()))


@pytest.mark.parametrize("streaming", [False, True])
@pytest.mark.parametrize("parent_exits", [False, True])
def test_bridge_timeout_kills_inherited_pipe_descendant(tmp_path, streaming, parent_exits):
    marker = tmp_path / "descendant.pid"
    source = textwrap.dedent(f"""
        import subprocess, sys, time
        from pathlib import Path
        child = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(120)"])
        Path({str(marker)!r}).write_text(str(child.pid))
        {"sys.exit(0)" if parent_exits else "time.sleep(120)"}
    """)
    command = [sys.executable, "-c", source]
    bridge = UtAgentBridge(BridgeSettings("", "", "", 2, 2, 2))
    started = time.monotonic()
    with pytest.raises(subprocess.TimeoutExpired):
        if streaming:
            bridge._stream_units(
                command, str(tmp_path), os.environ.copy(), 2,
                tmp_path / "receipts", tmp_path, lambda _receipt: None)
        else:
            child = bridge._popen(command, str(tmp_path), os.environ.copy())
            bridge._wait(child, command, 2)
    assert time.monotonic() - started < 8
    assert marker.is_file()
    assert_stopped(int(marker.read_text()))


def test_bridge_success_closes_tree_ownership(tmp_path):
    command = [sys.executable, "-c", "print('complete')"]
    bridge = UtAgentBridge(BridgeSettings("", "", "", 2, 2, 2))
    child = bridge._popen(command, str(tmp_path), os.environ.copy())
    result = bridge._wait(child, command, 2)
    assert result.returncode == 0
    assert result.stdout.strip() == b"complete"


def test_dispose_busy_session_interrupts_rpc(native_api, monkeypatch):
    root, marker = native_api
    monkeypatch.setenv("FAKE_SILVER_HANG", "clear")
    session = _SilverSession(str(root), {}, time.monotonic() + 60)
    failures = []

    def execute():
        try:
            session.clear()
        except RunnerError as exc:
            failures.append(exc)

    thread = threading.Thread(target=execute)
    thread.start()
    try:
        deadline = time.monotonic() + 3
        while not Path(str(marker) + ".clear").exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert Path(str(marker) + ".clear").exists()
        started = time.monotonic()
        SilverRunner._safe_dispose(session)
        assert time.monotonic() - started < 4
        thread.join(timeout=2)
        assert not thread.is_alive()
        assert failures
        assert_stopped(int(marker.read_text()))
    finally:
        session.stop()
        thread.join(timeout=2)


@pytest.mark.parametrize("phase", ["send", "partial_reply", "pipe_send"])
def test_transport_deadline_covers_blocked_io(native_api, phase):
    import multiprocessing

    root, marker = native_api
    session = _SilverSession(str(root), {}, time.monotonic() + 5)
    original = session._connection
    blocked = threading.Event()
    released = threading.Event()
    local, remote = multiprocessing.Pipe()

    class StalledConnection:
        def send(self, payload):
            if phase == "pipe_send":
                blocked.set()
                local.send_bytes(b"x" * (16 * 1024 * 1024))
            elif phase == "send":
                blocked.set()
                released.wait(10)
            else:
                original.send(payload)

        def poll(self, timeout):
            return True

        def recv(self):
            blocked.set()
            released.wait(10)
            raise EOFError()

        def close(self):
            released.set()
            remote.close()
            local.close()
            original.close()

    session._connection = StalledConnection()
    session._deadline = time.monotonic() + 0.2
    started = time.monotonic()
    try:
        with pytest.raises(RunnerError, match="^timeout$"):
            session.clear()
        assert blocked.is_set()
        assert time.monotonic() - started < 4
        assert_stopped(int(marker.read_text()))
    finally:
        released.set()
        session.stop()


def test_session_cleanup_failure_still_reaps_and_retains_job(native_api, monkeypatch):
    from app.runners import process_guard, silver_runner

    root, marker = native_api
    session = _SilverSession(str(root), {}, time.monotonic() + 10)
    original = silver_runner.kill_process_tree
    calls = []

    def fail_once(pid, job):
        calls.append(pid)
        if len(calls) == 1:
            raise OSError("injected job query failure")
        return original(pid, job)

    monkeypatch.setattr(silver_runner, "kill_process_tree", fail_once)
    try:
        with pytest.raises(RunnerError, match="ownership retained"):
            session.stop()
        assert not session._worker.is_alive()
        assert id(session) in process_guard._pending_cleanup
        process_guard.retry_pending_cleanup()
        assert id(session) not in process_guard._pending_cleanup
        assert len(calls) >= 2
        assert_stopped(int(marker.read_text()))
    finally:
        session.stop()
