"""Non-streaming ut-agent calls must kill the process tree on timeout."""

import subprocess
from types import SimpleNamespace

import pytest

from app.runners import process_guard
from app.services.ut_pipeline.bridge import BridgeError, BridgeSettings, UtAgentBridge


@pytest.fixture(autouse=True)
def isolated_cleanup_registry(monkeypatch):
    monkeypatch.setattr(process_guard, "_pending_cleanup", {})


def test_nonstreaming_timeout_kills_the_process_tree(monkeypatch):
    bridge = UtAgentBridge(BridgeSettings("root", "python", "", 1, 1, 1))
    killed = []

    class Child:
        pid = 4242
        returncode = None

        def poll(self):
            return None

        def communicate(self, timeout=None):
            raise subprocess.TimeoutExpired(["python"], timeout or 0)

        def kill(self):
            killed.append("kill")

    monkeypatch.setattr(bridge, "_popen", lambda *_args: Child())
    monkeypatch.setattr(UtAgentBridge, "_kill_tree", staticmethod(lambda _child: killed.append("tree")))
    try:
        bridge._run(["check"], timeout=1)
    except BridgeError as exc:
        assert "timed out" in str(exc) or "超时" in str(exc)
    else:
        raise AssertionError("timeout was not reported")
    assert killed == ["tree"]


def test_streaming_cleanup_failure_is_bounded(monkeypatch, tmp_path):
    killed = []

    class Child:
        def communicate(self, timeout=None):
            assert timeout is not None and timeout <= 2
            raise subprocess.TimeoutExpired(["python"], timeout)

    monkeypatch.setattr(UtAgentBridge, "_popen", staticmethod(lambda *_args: Child()))
    monkeypatch.setattr(UtAgentBridge, "_kill_tree",
                        staticmethod(lambda _child: killed.append("tree")))
    with pytest.raises(BridgeError, match="cleanup could not drain"):
        UtAgentBridge._stream_units(
            ["python"], str(tmp_path), {}, 0.001,
            tmp_path / "ready", tmp_path, lambda _receipt: None)
    assert killed == ["tree"]


def test_posix_cleanup_signals_group_after_parent_exit(monkeypatch):
    groups = []
    monkeypatch.setattr(process_guard, "IS_WINDOWS", False)
    monkeypatch.setattr(process_guard.signal, "SIGKILL", 9, raising=False)
    monkeypatch.setattr(process_guard.os, "killpg",
                        lambda pid, signum: groups.append((pid, signum)), raising=False)
    child = SimpleNamespace(pid=4242, poll=lambda: 0)
    UtAgentBridge._kill_tree(child)
    assert groups == [(4242, 9)]


@pytest.mark.parametrize("streaming", [False, True])
def test_tree_failure_still_kills_drains_and_retains_for_retry(monkeypatch, tmp_path, streaming):
    calls = []

    class Child:
        returncode = None

        def communicate(self, timeout):
            calls.append("drain")
            if self.returncode is None:
                raise subprocess.TimeoutExpired(["python"], timeout)
            return b"", b""

        def kill(self):
            calls.append("kill")
            self.returncode = 1

        def wait(self, timeout):
            calls.append("wait")
            return self.returncode

    child = Child()
    monkeypatch.setattr(UtAgentBridge, "_popen", staticmethod(lambda *_args: child))

    def fail_tree(_child):
        calls.append("tree")
        raise OSError("injected job termination failure")

    monkeypatch.setattr(UtAgentBridge, "_kill_tree", staticmethod(fail_tree))
    bridge = UtAgentBridge(BridgeSettings("", "", "", 1, 1, 1))
    with pytest.raises(BridgeError, match="ownership retained"):
        if streaming:
            bridge._stream_units(
                ["python"], str(tmp_path), {}, 0.001,
                tmp_path / "ready", tmp_path, lambda _receipt: None)
        else:
            bridge._wait(child, ["python"], 0.001)
    assert calls[-4:] == ["tree", "kill", "wait", "drain"]
    assert id(child) in process_guard._pending_cleanup
    monkeypatch.setattr(UtAgentBridge, "_kill_tree", staticmethod(lambda _child: None))
    process_guard.retry_pending_cleanup()
    assert id(child) not in process_guard._pending_cleanup
