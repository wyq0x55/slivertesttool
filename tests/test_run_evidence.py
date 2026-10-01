from __future__ import annotations

import hashlib
import json
import os
import subprocess
import zipfile
from datetime import datetime
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


def test_evidence_log_budget_is_shared_across_all_files(tmp_path):
    task, source, _model = prepared(tmp_path)
    evidence = service()
    root = evidence.pin_attempt(task, source)
    results = root / "results"
    results.mkdir()
    for number in range(3):
        (results / f"case-{number}.log").write_text("0123456789", encoding="utf-8")
    evidence.seal_attempt(task, status="failed", verdict="FAIL", message="failure")
    data = evidence.read_evidence(task, max_log_bytes=12)
    assert sum(len(value.encode("utf-8")) for value in data["logs"].values()) <= 12
    assert len(data["artifacts"]) == 3


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


def test_retest_from_pinned_model_keeps_original_identity_hash(tmp_path):
    task, source, model = prepared(tmp_path)
    dll, sbs = model.with_suffix(".dll"), model.with_suffix(".sbs")
    dll.write_bytes(b"dll-v1")
    sbs.write_bytes(b"sbs-v1")
    model.write_text(f'{dll.as_posix()} -S {sbs.as_posix()}\n', encoding="utf-8")
    evidence = service()
    evidence.pin_attempt(task, source)
    first = evidence.read_evidence(task)
    task.run_count = 2
    evidence.pin_attempt(task, source)
    second = evidence.read_evidence(task)
    assert second["model"]["sha256"] == first["model"]["sha256"]
    assert second["model"]["source_path"] == first["model"]["source_path"]
    assert Path(second["model"]["execution_path"]).parent != Path(first["model"]["execution_path"]).parent


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


def test_missing_new_attempt_never_uses_another_cases_legacy_logs(tmp_path):
    from app.services import report_service
    task, source, _model = prepared(tmp_path)
    service().pin_attempt(task, source)
    task.run_count = 2
    task.report_path = ""
    legacy = Path(task.workspace) / "log" / task.test_id
    legacy.mkdir(parents=True)
    (legacy / "jdgrslt.log").write_text("unrelated", encoding="utf-8")
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
def submitted(app_ctx, tmp_path, monkeypatch):
    from app.extensions import db
    from app.models import Task
    monkeypatch.setattr(app_ctx.config_obj, "POOL_DIR", tmp_path / "pool")
    with app_ctx.app_context():
        prototype, source, model = prepared(tmp_path)
        task = Task(**{key: value for key, value in vars(prototype).items() if key != "id"})
        db.session.add(task)
        db.session.commit()
        service().pin_attempt(task, source, approved_inputs={"review": "human"})
        db.session.commit()
        yield app_ctx, task, source, model


@pytest.mark.parametrize("mutation", ["manifest", "rewritten_input_hash", "outcome"])
def test_database_attempt_identity_binds_evidence_metadata(submitted, mutation):
    from app.extensions import db
    _app, task, _source, _model = submitted
    evidence = service()
    root = evidence.attempt_dir(task)
    if mutation == "outcome":
        evidence.seal_attempt(task, status="failed", verdict="FAIL", message="failure")
        db.session.commit()
        path = root / "outcome.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        data["verdict"] = "PASS"
    else:
        path = root / "manifest.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        if mutation == "manifest":
            data["approved_inputs"] = {"review": "rewritten"}
        else:
            (root / "inputs" / task.test_id / "constants.json").write_text('{"constants":{"FORGED":1}}', encoding="utf-8")
            data["input_files"] = evidence._hashes(root / "inputs")
    path.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(ValueError, match="integrity|hash"):
        evidence.read_evidence(task)


def test_submission_rollback_does_not_block_a_fresh_approved_attempt(submitted):
    from app.extensions import db
    _app, task, source, model = submitted
    evidence = service()
    original = evidence.read_evidence(task)
    task.run_count = 2
    task.sil_relpath = str(model)
    evidence.pin_attempt(task, source, approved_inputs={"review": "aborted"})
    db.session.flush()
    db.session.rollback()
    assert task.run_count == 1
    task.run_count = 2
    task.sil_relpath = str(model)
    evidence.pin_attempt(task, source, approved_inputs={"review": "fresh human decision"})
    db.session.commit()
    assert evidence.read_evidence(task)["approved_inputs"] == {"review": "fresh human decision"}
    assert evidence.read_evidence(task, run_count=1) == original


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
    monkeypatch.setattr(test_runner, "_write_row_result", lambda current, verdict, **kwargs: writes.append(verdict))
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
                    {"cancel_requested": False, "deleted_at": datetime.now()},
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


def test_submission_export_pins_before_commit_and_history_resolves_after_retest(app_ctx, tmp_path, monkeypatch):
    from app.extensions import db
    from app.models import Project, Task, TestItemRow, TestRunRecord
    from app.runners import test_runner
    from app.services.lanmatrix import silver_json_export as exporter
    from app.services import task_service
    with app_ctx.app_context():
        project = Project(code="EVIDENCE", name="Evidence", owner_id=None)
        db.session.add(project)
        db.session.commit()
        matrix_row = TestItemRow(project_id=project.id, case_id="TC-1", custom_values={
            "steps": {"input_signals": [["input", "IN"]], "steps": [{"no": 1, "inputs": ["1"]}]}})
        db.session.add(matrix_row)
        db.session.commit()
        prototype, source, model = prepared(tmp_path)
        task = task_service.create_task(task_name="case", file_name="json", submitter="tester",
                                        test_id="TC-1", sil_relpath=str(model), sil_name="plant",
                                        sil_version="v1", project_id=project.id, workspace="", commit=False)
        exporter.materialise_run_dir(source, matrix_row, [], [], task=task)
        assert service().read_evidence(task)["approved_inputs"]["row"]["id"] == matrix_row.id
        assert service().read_evidence(task)["documents"]["testcase_TC-1.json"]["steps"][0]["inputs"][0]["value"] == 1
        db.session.commit()

        def run(context):
            (context.log_dir / "jdgrslt.log").write_text("Test is Passed.", encoding="utf-8")

        monkeypatch.setattr(test_runner, "build_runner", lambda backend: SimpleNamespace(run=run))
        test_runner.execute(app_ctx, app_ctx.config_obj, task)
        record = TestRunRecord.query.filter_by(task_key=task.task_key).one()
        assert record.run_count == 1
        assert record.to_dict()["run_count"] == 1
        assert matrix_row.result == "PASS"
        old = service().read_record_evidence(record)
        task.run_count = 2
        task.status = "queued"
        task.report_path = ""
        matrix_row.custom_values = {"steps": {"input_signals": [["input", "IN"]],
                                              "steps": [{"no": 1, "inputs": ["2"]}]}}
        exporter.materialise_run_dir(source, matrix_row, [], [], task=task)
        db.session.commit()
        assert service().read_record_evidence(record) == old
        assert service().read_evidence(task.task_key)["documents"]["testcase_TC-1.json"]["steps"][0]["inputs"][0]["value"] == 2


def test_explicit_schema_upgrade_does_not_invent_legacy_attempts(app_ctx):
    from sqlalchemy import text, inspect
    from app.bootstrap import _migrate_schema
    from app.extensions import db
    with app_ctx.app_context():
        with db.engine.begin() as connection:
            connection.execute(text("ALTER TABLE lm_test_run_records DROP COLUMN run_count"))
        _migrate_schema()
        _migrate_schema()
        column = next(item for item in inspect(db.engine).get_columns("lm_test_run_records")
                      if item["name"] == "run_count")
        assert column["nullable"] is True


def test_recovery_returns_enqueue_failure_without_changing_attempt(submitted):
    _app, task, _source, _model = submitted
    before = service().read_evidence(task)

    def fail(task_id, attempt):
        raise RuntimeError("queue unavailable")

    result = service().recover_queued_attempts(fail, is_pending=lambda *args: False)
    assert result["recovered"] == []
    assert result["errors"][0]["error"] == "queue unavailable"
    assert service().read_evidence(task) == before


def test_sealed_results_reject_late_mutation(tmp_path):
    task, source, _model = prepared(tmp_path)
    evidence = service()
    root = evidence.pin_attempt(task, source)
    result = root / "results"
    result.mkdir()
    (result / "Console.log").write_text("original", encoding="utf-8")
    evidence.seal_attempt(task, status="failed", verdict="ERROR", message="failure")
    (result / "Console.log").write_text("changed", encoding="utf-8")
    with pytest.raises(ValueError, match="integrity"):
        evidence.read_evidence(task)


def test_http_submission_and_retest_enqueue_the_pinned_attempt(app_ctx, tmp_path, monkeypatch):
    from app.extensions import db
    from app.models import LMUser, Project, ProjectMember, ProjectModel, Task, TestItemRow
    from app.jobqueue import tasks as jobs
    with app_ctx.app_context():
        user = LMUser(username="evidence-runner", display_name="Runner", password_hash="x")
        db.session.add(user)
        db.session.commit()
        project = Project(code="HTTP-EVIDENCE", name="HTTP evidence", owner_id=user.id)
        db.session.add(project)
        db.session.commit()
        _prototype, _source, model = prepared(tmp_path)
        db.session.add(ProjectMember(project_id=project.id, user_id=user.id, role="project_admin"))
        db.session.add(ProjectModel(project_id=project.id, name="plant", version="v1",
                                   sil_path=str(model), is_current=True, kind="path"))
        db.session.add(TestItemRow(project_id=project.id, case_id="TC-1", sheet="test",
                                   custom_values={"steps": {"steps": [{"no": 1}]}}))
        db.session.commit()
        project_id, user_id = project.id, user.id
    calls = []

    def enqueue(task_id, expected_run_count=None):
        task = db.session.get(Task, task_id)
        snapshot = service().read_evidence(task, expected_run_count)
        calls.append((task_id, expected_run_count, snapshot))

    monkeypatch.setattr(jobs, "run_task", enqueue)
    client = app_ctx.test_client()
    with client.session_transaction() as session:
        session["lm_user_id"] = user_id
        session["csrf_token"] = "evidence-csrf"
    headers = {"X-CSRF-Token": "evidence-csrf"}
    response = client.post(f"/api/v1/projects/{project_id}/tasks/run-selected",
                           json={"test_ids": ["TC-1"], "model": "plant@v1"}, headers=headers)
    assert response.status_code == 201
    assert len(response.json["data"]["created"]) == 1, response.json
    assert calls[0][1] == 1
    with app_ctx.app_context():
        task = db.session.get(Task, calls[0][0])
        task.status = "passed"
        task_key = task.task_key
        db.session.commit()
    response = client.post(f"/api/v1/projects/{project_id}/tasks/rerun-selected",
                           json={"task_keys": [task_key]}, headers=headers)
    assert len(response.json["data"]["created"]) == 1, response.json
    assert calls[1][1] == 2
    assert calls[0][2]["archive_path"] != calls[1][2]["archive_path"]
    with app_ctx.app_context():
        assert service().read_evidence(task_key, 1) == calls[0][2]


def test_recovery_preserves_pinned_legacy_upload(submitted):
    _application, task, source, model = submitted
    task.run_count = 2
    task.sil_relpath = str(model)
    for name in ("testcase_TC-1.json", "constants.json", "lib.json"):
        (source / name).unlink()
    (source / "judge.py").write_text("print('approved legacy script')", encoding="utf-8")
    service().pin_attempt(task, source, approved_inputs={"source": "human submitted bundle"})
    from app.extensions import db
    db.session.commit()
    calls = []
    result = service().recover_queued_attempts(
        lambda *identity: calls.append(identity), is_pending=lambda *identity: False)
    assert result["recovered"] == [(task.id, 2)]
    assert calls == [(task.id, 2)]


@pytest.mark.parametrize("failure", [False, True])
def test_pooled_console_is_sliced_before_sealing_all_outcomes(submitted, monkeypatch, failure):
    from app.runners import test_runner
    from app.runners.silver_runner import RunnerError
    application, task, _source, _model = submitted
    console = Path(application.config_obj.POOL_DIR) / "inst_1" / "Console.log"
    console.parent.mkdir(parents=True)
    console.write_bytes(b"previous case\n")
    instance = SimpleNamespace(uid=1, handle=object(), console_log=console)

    def run(current, context):
        with console.open("ab") as stream:
            stream.write(b"this attempt only\n")
        (context.log_dir / "jdgrslt.log").write_text("Test is failed.", encoding="utf-8")
        if failure:
            raise RunnerError("synthetic pooled failure")

    pool = SimpleNamespace(is_mock=True, configure_and_run=run, force_stop=lambda current: None)
    test_runner.execute(application, application.config_obj, task, pool=pool, instance=instance)
    data = service().read_evidence(task)
    assert data["logs"]["Console.log"] == "this attempt only\n"
    assert data["outcome"]["evidence_kind"] == "synthetic"
    assert data["outcome"]["runner_backend"] == "mock"
    assert task.result == ("ERROR" if failure else "FAIL")
    console.write_bytes(b"subsequent case\n")
    assert service().read_evidence(task)["logs"] == data["logs"]


def test_purge_removes_only_its_task_archive_and_soft_delete_preserves_it(tmp_path):
    from app.services import task_service
    task, source, model = prepared(tmp_path)
    first = service().pin_attempt(task, source)
    other = SimpleNamespace(**{**vars(task), "task_key": "T000002", "sil_relpath": str(model)})
    second = service().pin_attempt(other, source)
    task.deleted_at = datetime.now()
    assert first.is_dir()
    with pytest.raises(ValueError, match="deleted"):
        service().read_evidence(task)
    task_service.remove_task_artifacts(task)
    assert not first.exists()
    assert second.is_dir()
