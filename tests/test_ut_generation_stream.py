"""Authoritative completed-unit transport, independent of PostgreSQL."""

import hashlib
import json
import os
import signal
import subprocess
import sys
import time

import pytest

from app.services.ut_pipeline import bridge, generated_unit


def _receipt(root):
    unit_dir = root / "f"
    unit_dir.mkdir(parents=True)
    unit = {"function": "f", "row": 1, "status": "VALIDATED", "output_dir": str(unit_dir)}
    artifacts = {}
    for key, name in (("testcsv", "f.csv"), ("ir_json", "function-ir.json"),
                      ("intent_manifest", "test-intents.json"), ("intent_summary", "test-intents-summary.json")):
        path = unit_dir / name
        path.write_bytes(b"artifact")
        artifacts[key] = {"path": str(path), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
        if key != "intent_summary":
            unit[key] = str(path)
    return {"contract": "ut-agent.generated-unit/1", "provenance": {"generator_version": "test"},
            "unit": unit, "artifacts": artifacts}


@pytest.mark.parametrize("damage", ["hash", "missing_summary", "outside", "descriptor", "contract", "untyped"])
def test_invalid_completed_artifacts_cannot_authorize_dispatch(tmp_path, damage):
    root = tmp_path / "generation"
    record = _receipt(root)
    if damage == "hash":
        record["artifacts"]["testcsv"]["sha256"] = "0" * 64
    elif damage == "missing_summary":
        del record["artifacts"]["intent_summary"]
    elif damage == "outside":
        record["unit"]["output_dir"] = str(tmp_path)
    elif damage == "descriptor":
        record["unit"]["testcsv"] = str(root / "other.csv")
    elif damage == "contract":
        record["contract"] = "progress/1"
    else:
        record["artifacts"] = {}
    path = root / "ready.json"
    path.write_text(json.dumps(record), encoding="utf-8")
    with pytest.raises(ValueError):
        generated_unit.read_receipt(path, root)


def test_finalized_receipt_preserves_the_exact_unit_and_provenance(tmp_path):
    record = _receipt(tmp_path)
    path = tmp_path / "ready.json"
    path.write_text(json.dumps(record), encoding="utf-8")
    assert generated_unit.read_receipt(path, tmp_path) == record


def test_completed_callback_runs_before_child_finishes_and_drains_large_pipes(tmp_path):
    root = tmp_path / "generation"
    record = _receipt(root)
    directory = root / "unit-ready"
    directory.mkdir()
    source = tmp_path / "record.json"
    source.write_text(json.dumps(record), encoding="utf-8")
    script = """
import json,os,sys,time
from pathlib import Path
root,source=map(Path,sys.argv[1:])
sys.stderr.write('x'*1024*1024);sys.stderr.flush()
sys.stdout.write('log '*20000+'\\n');sys.stdout.flush()
directory=root/'unit-ready'
pending=directory/'1.tmp'
pending.write_bytes(source.read_bytes())
os.replace(pending,directory/'1.json')
deadline=time.monotonic()+5
while not (root/'consumed').exists():
    if time.monotonic()>deadline: raise RuntimeError('callback waited for batch exit')
    time.sleep(.01)
(root/'finished').touch()
print(json.dumps({'contract':'ut-agent.bridge/1','ok':True,'units':[json.loads(source.read_text())['unit']]}))
"""
    observed = []

    def completed(value):
        assert not (root / "finished").exists()
        observed.append(value)
        (root / "consumed").touch()

    result = bridge.UtAgentBridge._stream_units(
        [sys.executable, "-c", script, str(root), str(source)], str(tmp_path), os.environ.copy(),
        10, directory, root, completed)
    assert result.returncode == 0
    assert observed == [record]
    assert (root / "finished").exists()
    assert len(result.stderr) == 1024 * 1024
    assert json.loads(result.stdout.splitlines()[-1])["units"] == [record["unit"]]


def test_stream_timeout_terminates_its_child(tmp_path, monkeypatch):
    children, spawn = [], subprocess.Popen

    def capture(*args, **kwargs):
        child = spawn(*args, **kwargs)
        if args[0][0] == sys.executable:
            children.append(child)
        return child

    monkeypatch.setattr(bridge.subprocess, "Popen", capture)
    with pytest.raises(subprocess.TimeoutExpired):
        bridge.UtAgentBridge._stream_units(
            [sys.executable, "-c", "import time;time.sleep(20)"], str(tmp_path), os.environ.copy(),
            0.3, tmp_path / "ready", tmp_path, lambda _: pytest.fail("No completed unit exists"))
    assert len(children) == 1 and children[0].poll() is not None


def test_timeout_terminates_descendants_that_inherit_generation_output_pipes(tmp_path):
    script = """
import subprocess,sys,time
subprocess.Popen([sys.executable,'-c','import time;time.sleep(20)'])
print('descendant started',flush=True)
time.sleep(20)
"""
    started = time.monotonic()
    with pytest.raises(subprocess.TimeoutExpired):
        bridge.UtAgentBridge._stream_units(
            [sys.executable, "-c", script], str(tmp_path), os.environ.copy(),
            0.5, tmp_path / "ready", tmp_path, lambda _: pytest.fail("No completed unit exists"))
    assert time.monotonic() - started < 6


def test_failed_tree_cleanup_reports_an_error_without_waiting_for_live_descendants(tmp_path, monkeypatch):
    pid_file = tmp_path / "descendant.pid"
    script = """
import subprocess,sys,time
from pathlib import Path
child=subprocess.Popen([sys.executable,'-c','import time;time.sleep(20)'])
Path(sys.argv[1]).write_text(str(child.pid))
time.sleep(20)
"""
    run = subprocess.run
    kill_group = getattr(os, "killpg", None)
    monkeypatch.setattr(bridge.subprocess, "run", lambda *args, **kwargs: subprocess.CompletedProcess(args, 1))
    if kill_group is not None:
        monkeypatch.setattr(bridge.os, "killpg", lambda *args: None)
    started = time.monotonic()
    try:
        with pytest.raises(bridge.BridgeError, match="cleanup could not drain"):
            bridge.UtAgentBridge._stream_units(
                [sys.executable, "-c", script, str(pid_file)], str(tmp_path), os.environ.copy(),
                1, tmp_path / "ready", tmp_path, lambda _: pytest.fail("No completed unit exists"))
        assert time.monotonic() - started < 7
    finally:
        if pid_file.is_file():
            pid = int(pid_file.read_text())
            if os.name == "nt":
                run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True, check=False, timeout=5)
            else:
                os.kill(pid, signal.SIGKILL)


def test_existing_receipts_are_rejected_before_starting_another_generator(tmp_path, monkeypatch):
    directory = tmp_path / "unit-ready"
    directory.mkdir()
    (directory / "stale.json").write_text("{}", encoding="utf-8")
    client = bridge.UtAgentBridge(bridge.BridgeSettings(str(tmp_path), sys.executable, "", 1, 1, 1))
    monkeypatch.setattr(client, "_run", lambda *a, **k: pytest.fail("Stale receipt launched a generator"))
    with pytest.raises(bridge.BridgeError, match="stale"):
        client.generate("m", "i", "p", tmp_path, ["f"], progress_dir=tmp_path / "progress", on_unit=lambda _: None)
