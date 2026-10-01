from __future__ import annotations

import hashlib
import json
import os
import subprocess
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest


def prepared(tmp_path, *, task_key="T000001", run_count=1):
    workspace = tmp_path / "workspace"
    source = workspace / ".pending" / "TC-1" / "TC-1"
    source.mkdir(parents=True, exist_ok=True)
    (source / "constants.json").write_text('{"constants": {}}', encoding="utf-8")
    (source / "lib.json").write_text('{"subroutines": {}}', encoding="utf-8")
    (source / "testcase_TC-1.json").write_text(
        json.dumps({"test_case_id": "TC-1", "steps": [{"no": 1,
                    "inputs": [{"var": "IN", "value": 1}]}]}), encoding="utf-8")
    model = tmp_path / "saved_model" / "plant.sil"
    model.parent.mkdir(exist_ok=True)
    model.write_text("saved model", encoding="utf-8")
    task = SimpleNamespace(id=1, task_key=task_key, run_count=run_count,
                           test_id="TC-1", project_id=7, workspace=str(workspace),
                           sil_relpath=str(model), sil_model_id=42,
                           sil_name="plant", sil_version="v1", report_path="",
                           status="queued", cancel_requested=False, deleted_at=None)
    return task, source, model


def service():
    from app.services import run_evidence_service
    return run_evidence_service


def test_pin_preserves_inputs_and_saved_model_after_edit(tmp_path):
    evidence = service()
    task, source, model = prepared(tmp_path)
    original_hash = hashlib.sha256(model.read_bytes()).hexdigest()
    root = evidence.pin_attempt(task, source, approved_inputs={"steps": "approved"})
    (source / "constants.json").write_text("changed", encoding="utf-8")
    model.write_text("edited model", encoding="utf-8")
    task.sil_version = "v2"
    data = evidence.read_evidence(task)
    assert data["model"]["sha256"] == original_hash
    assert data["model"]["version"] == "v1"
    assert data["approved_inputs"] == {"steps": "approved"}
    assert data["documents"]["constants.json"] == {"constants": {}}
    assert Path(data["model"]["execution_path"]).read_text(encoding="utf-8") == "saved model"
    assert task.report_path == str(root)
    with pytest.raises(FileExistsError):
        evidence.pin_attempt(task, source)


def test_saved_companions_are_pinned_and_references_rewritten(tmp_path):
    evidence = service()
    task, source, model = prepared(tmp_path)
    dll = model.with_suffix(".dll")
    sbs = model.with_suffix(".sbs")
    dll.write_bytes(b"dll-v1")
    sbs.write_bytes(b"sbs-v1")
    model.write_text(f'{dll.as_posix()} -S {sbs.as_posix()}\n', encoding="utf-8")
    evidence.pin_attempt(task, source)
    data = evidence.read_evidence(task)
    pinned = Path(data["model"]["execution_path"])
    assert str(model.parent).replace("\\", "/") not in pinned.read_text(encoding="utf-8")
    dll.write_bytes(b"dll-v2")
    sbs.write_bytes(b"sbs-v2")
    assert (pinned.parent / dll.name).read_bytes() == b"dll-v1"
    assert (pinned.parent / sbs.name).read_bytes() == b"sbs-v1"


def test_reports_remain_attempt_specific_for_retest_and_same_case_tasks(tmp_path):
    from app.services import report_service
    evidence = service()
    task, source, model = prepared(tmp_path)
    roots = []
    for key, attempt, verdict in [("T000001", 1, "first"),
                                  ("T000001", 2, "second"),
                                  ("T000002", 1, "parallel")]:
        task.task_key, task.run_count = key, attempt
        task.sil_relpath = str(model)
        root = evidence.pin_attempt(task, source)
        result = root / "results"
        result.mkdir()
        (result / "jdgrslt.log").write_text(verdict, encoding="utf-8")
        evidence.seal_attempt(task, status="failed", verdict="FAIL", message=verdict)
        roots.append(root)
    task.task_key, task.run_count = "T000001", 2
    task.report_path = str(roots[1])
    assert len(set(roots)) == 3
    assert report_service.jdgrslt_path(task, run_count=1).read_text(encoding="utf-8") == "first"
    with zipfile.ZipFile(report_service.build_report_stream(task, run_count=1)) as archive:
        assert archive.read("TC-1/jdgrslt.log") == b"first"
    assert evidence.read_evidence(task, run_count=1)["outcome"]["message"] == "first"


def test_changed_snapshot_is_rejected_and_not_used_as_report_fallback(tmp_path):
    from app.services import report_service
    evidence = service()
    task, source, _model = prepared(tmp_path)
    root = evidence.pin_attempt(task, source)
    (root / "inputs" / "TC-1" / "constants.json").write_text("tampered", encoding="utf-8")
    with pytest.raises(ValueError, match="hash|integrity"):
        evidence.read_evidence(task)
    old = Path(task.workspace) / "log" / task.test_id
    old.mkdir(parents=True)
    (old / "jdgrslt.log").write_text("unrelated", encoding="utf-8")
    assert report_service.result_dir(task) is None


@pytest.mark.parametrize("attribute,value", [("task_key", ".."), ("test_id", "../outside")])
def test_unsafe_identity_is_rejected(tmp_path, attribute, value):
    task, source, _model = prepared(tmp_path)
    setattr(task, attribute, value)
    with pytest.raises(ValueError):
        service().pin_attempt(task, source)


def test_junction_escape_is_rejected(tmp_path):
    task, source, _model = prepared(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    link = Path(task.workspace) / ".evidence"
    if os.name == "nt":
        result = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(outside)],
                                capture_output=True)
        assert result.returncode == 0
    else:
        link.symlink_to(outside, target_is_directory=True)
    try:
        with pytest.raises(ValueError):
            service().pin_attempt(task, source)
        assert list(outside.iterdir()) == []
    finally:
        if os.name == "nt":
            os.rmdir(link)
        else:
            link.unlink()


@pytest.fixture
def submitted(app_ctx, tmp_path):
    from app.extensions import db
    from app.models import Task
    with app_ctx.app_context():
        prototype, source, model = prepared(tmp_path)
        task = Task(**{key: value for key, value in vars(prototype).items() if key != "id"})
        db.session.add(task)
        db.session.commit()
        service().pin_attempt(task, source, approved_inputs={"review": "human"})
        db.session.commit()
        yield app_ctx, task, source, model


@pytest.mark.parametrize("failure", [None, "error", "cancelled"])
def test_runner_uses_snapshot_archives_all_outcomes_and_writes_back(submitted, monkeypatch, failure):
    from app.runners import test_runner
    from app.runners.silver_runner import RunnerCancelled, RunnerError
    from app.services import report_service
    application, task, source, model = submitted
    calls = []
    writes = []
    model.write_text("edited", encoding="utf-8")
    (source / "constants.json").write_text("edited", encoding="utf-8")

    def run(context):
        calls.append(context)
        assert context.sil_path.read_text(encoding="utf-8") == "saved model"
        assert json.loads((context.run_dir / "TC-1" / "constants.json").read_text()) == {"constants": {}}
        (context.log_dir / "Console.log").write_text("attempt console", encoding="utf-8")
        (context.log_dir / "jdgrslt.log").write_text("Test is Passed.", encoding="utf-8")
        if failure == "error":
            raise RunnerError("mock failure")
        if failure == "cancelled":
            raise RunnerCancelled()

    monkeypatch.setattr(test_runner, "build_runner", lambda backend: SimpleNamespace(run=run))
    monkeypatch.setattr(test_runner, "_write_row_result", lambda current, verdict: writes.append(verdict))
    test_runner.execute(application, application.config_obj, task)
    assert len(calls) == 1
    assert task.result == {None: "PASS", "error": "ERROR", "cancelled": "CANCELLED"}[failure]
    assert writes == [task.result]
    data = service().read_evidence(task)
    assert data["outcome"]["verdict"] == task.result
    assert "attempt console" in data["logs"]["Console.log"]
    assert report_service.jdgrslt_path(task).read_text() == "Test is Passed."


def test_runner_blocks_tampered_input_before_backend(submitted, monkeypatch):
    from app.runners import test_runner
    application, task, _source, _model = submitted
    root = service().attempt_dir(task)
    (root / "inputs" / "TC-1" / "constants.json").write_text("tampered", encoding="utf-8")
    calls = []
    monkeypatch.setattr(test_runner, "build_runner", lambda backend: calls.append(backend))
    test_runner.execute(application, application.config_obj, task)
    assert calls == []
    assert task.status == "failed"
    assert task.result == "ERROR"


def test_recovery_only_dispatches_pinned_current_queued_attempt(submitted):
    from app.extensions import db
    from app.models import Task
    application, task, _source, _model = submitted
    dispatched = []
    pending = set()

    def enqueue(task_id, run_count):
        dispatched.append((task_id, run_count))
        pending.add((task_id, run_count))

    recover = lambda: service().recover_queued_attempts(
        enqueue, is_pending=lambda task_id, attempt: (task_id, attempt) in pending, limit=10)
    original = service().read_evidence(task)
    summary = recover()
    assert summary["recovered"] == [(task.id, 1)]
    assert recover()["recovered"] == []
    assert service().read_evidence(task) == original
    pending.clear()
    for changes in [{"status": "running"}, {"status": "passed"},
                    {"status": "queued", "cancel_requested": True},
                    {"cancel_requested": False, "deleted_at": __import__("datetime").datetime.now()},
                    {"deleted_at": None, "run_count": 2}]:
        for name, value in changes.items():
            setattr(task, name, value)
        db.session.commit()
        assert recover()["recovered"] == []
    unpinned = Task(task_key="T000099", workspace=task.workspace, test_id="other")
    db.session.add(unpinned)
    db.session.commit()
    assert recover()["recovered"] == []
    assert dispatched == [(task.id, 1)]
