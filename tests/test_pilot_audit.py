from __future__ import annotations

import hashlib
import importlib
import json
import os
from datetime import datetime
from pathlib import Path

import pytest
from flask import Flask
from sqlalchemy import event, text
from sqlalchemy.exc import DBAPIError

from app.extensions import db
from app.models import AiDraft, LMUser, Project, ProjectMember, ProjectModel, RunEvidence, Task, TestItemRow
from app.services import run_evidence_service as evidence
from app.services.pilot_contract import PilotInput


TEST_URL = "postgresql+psycopg2://postgres@127.0.0.1:55435/stp_pilot_audit"


def audit_module():
    return importlib.import_module("app.services.pilot_audit")


def steps_document(value="1"):
    return {"input_signals": [["Input", "IN"]], "expected_signals": [["Output", "OUT"]],
            "steps": [{"no": 1, "inputs": [value], "expecteds": ["1"], "timing": "即時"}]}


@pytest.fixture()
def audit_env(tmp_path):
    assert all(os.environ.get(name) == TEST_URL for name in (
        "TEST_DATABASE_URL", "DATABASE_URL", "HUEY_DATABASE_URL"))
    assert os.environ.get("RUNNER_BACKEND") == "mock"
    assert os.environ.get("HUEY_IMMEDIATE") == "1"
    application = Flask(__name__)
    application.config.update(SQLALCHEMY_DATABASE_URI=TEST_URL, SQLALCHEMY_TRACK_MODIFICATIONS=False)
    db.init_app(application)
    with application.app_context():
        db.drop_all()
        db.create_all()
        reader = LMUser(username="pilot-reader", password_hash="unused", status="active")
        outsider = LMUser(username="pilot-outsider", password_hash="unused", status="active")
        admin = LMUser(username="pilot-admin", password_hash="unused", status="active", is_system_admin=True)
        project = Project(code="PILOT", name="Pilot", status="active")
        foreign = Project(code="FOREIGN", name="Foreign", status="active")
        db.session.add_all([reader, outsider, admin, project, foreign])
        db.session.flush()
        db.session.add(ProjectMember(project_id=project.id, user_id=reader.id, role="reader"))
        model_path = tmp_path / "saved" / "pilot.sil"
        model_path.parent.mkdir()
        model_path.write_text("pilot configuration\n", encoding="utf-8")
        model = ProjectModel(project_id=project.id, name="pilot-model", version="v1", sil_path=str(model_path))
        rows = [TestItemRow(project_id=project.id, sheet="test", case_id=f"C{number}",
                            uuid=f"pilot-row-{number}", version=3, module="A" if number <= 10 else "B",
                            review_status="approved", workflow_status="Ready", custom_values={"steps": steps_document()})
                for number in range(1, 21)]
        db.session.add_all([model, *rows])
        db.session.flush()
        payload = {"schema_version": 1, "project_id": project.id, "modules": ["A", "B"],
                   "model": {"model_id": model.id, "version": "v1",
                             "sha256": hashlib.sha256(model_path.read_bytes()).hexdigest()},
                   "viewpoints": [{"item_id": row.id, "version": 3, "module": row.module,
                                   "document_revision": "declared-doc-v1", "approval_reference": "declared-owner-review"}
                                  for row in rows]}
        result = {"app": application, "reader": reader.id, "outsider": outsider.id, "admin": admin.id,
                  "project": project.id, "foreign": foreign.id, "model": model.id, "path": model_path,
                  "rows": [row.id for row in rows], "payload": payload, "root": tmp_path}
        db.session.commit()
        db.session.rollback()
        try:
            yield result
        finally:
            db.session.remove()
            db.engine.dispose()


def pilot(env, **updates):
    return PilotInput.model_validate({**env["payload"], **updates})


def collect(env, **updates):
    return audit_module().collect_snapshot(pilot(env, **updates), actor_id=env["reader"])


def make_draft(env, *, status="pending", meta=None, accepted=None, legacy=False):
    identities = env["rows"][:2]
    viewpoints = [{"ref": str(identity), "item_id": identity, "version": 3} for identity in identities]
    source = {"_context": {"schema_version": 1, "project_id": env["project"],
                            "items": [{"id": identity, "version": 3} for identity in identities],
                            "model": {"id": env["model"], "name": "pilot-model", "version": "v1"},
                            "provenance": []},
              "viewpoints": viewpoints, "runtime_inputs": {"constants": [], "libraries": []}}
    output = {"procedures": [{"ref": str(identity), "steps_doc": steps_document()} for identity in identities]}
    applied = {"applied": [{"ref": str(identity), "item_id": identity} for identity in (accepted or [])],
               "skipped": [], "failed_refs": []}
    if legacy:
        source.pop("viewpoints")
        source["item_id"] = identities[0]
        source["_context"]["items"] = source["_context"]["items"][:1]
        output = {"steps_doc": steps_document()}
        applied = {"item_id": identities[0], "missing_variables": [], "note": ""}
    draft = AiDraft(project_id=env["project"], scenario="procedure", status=status,
                    input_json=json.dumps(source), output_json=json.dumps(output),
                    meta_json=json.dumps(meta or {"model": "configured-only", "usage": {"input_tokens": 10}}),
                    applied_result_json=json.dumps(applied),
                    reviewed_by=env["reader"] if status == "approved" else None,
                    reviewed_at=datetime.utcnow() if status == "approved" else None)
    db.session.add(draft)
    db.session.flush()
    identity = draft.id
    db.session.commit()
    return identity


def make_attempt(env, *, count=1, task=None, kind="silver_runtime", backend="silver", sealed=True, finalised=True):
    row = db.session.get(TestItemRow, env["rows"][0])
    if task is None:
        task = Task(task_key="T000001", project_id=env["project"], test_id=row.case_id,
                    sil_relpath=str(env["path"]), sil_name="pilot-model", sil_version="v1",
                    sil_model_id=env["model"], run_count=count, workspace=str(env["root"] / "workspace"))
        db.session.add(task)
        db.session.flush()
    else:
        task.run_count = count
    source = env["root"] / "workspace" / f"source-{count}"
    source.mkdir(parents=True)
    (source / "constants.json").write_text('{"constants": {}}', encoding="utf-8")
    (source / "lib.json").write_text('{"subroutines": {}}', encoding="utf-8")
    (source / f"testcase_{row.case_id}.json").write_text('{"steps": []}', encoding="utf-8")
    approved = {"row": {"id": row.id, "uuid": row.uuid, "version": 3}, "test": steps_document()}
    root = evidence.pin_attempt(task, source, approved_inputs=approved)
    db.session.commit()
    if sealed:
        results = root / "results"
        results.mkdir()
        for name in ("Console.log", "jdgrslt.log", "output.csv"):
            (results / name).write_text("retained synthetic fixture content", encoding="utf-8")
        evidence.seal_attempt(task, status="passed", verdict="PASS", message="fixture",
                              evidence_kind=kind, runner_backend=backend)
    record = db.session.get(RunEvidence, (task.id, count))
    record.finalised = finalised
    request = {"task_key": task.task_key, "run_count": count, "item_id": row.id}
    task_id = task.id
    db.session.commit()
    return request, root, task_id


def rewrite_manifest(task_id, root, mutate):
    path = root / "manifest.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    mutate(data)
    path.write_text(json.dumps(data), encoding="utf-8")
    record = db.session.get(RunEvidence, (task_id, data["run_count"]))
    record.manifest_sha256 = hashlib.sha256(path.read_bytes()).hexdigest()
    db.session.commit()


def test_collect_snapshot_binds_selection_and_never_infers_human_approval(audit_env):
    selection = pilot(audit_env)
    snapshot = collect(audit_env)
    assert snapshot.selection_sha256 == selection.selection_digest()
    assert snapshot.source == "postgresql_readonly"
    assert snapshot.preflight_issues == []
    assert snapshot.drafts == [] and snapshot.runs == []
    assert "declared-owner-review" not in snapshot.model_dump_json()
    assert not db.session().in_transaction()


def test_audit_preserves_all_database_counts_and_versions(audit_env):
    def state():
        counts = tuple(db.session.query(model).count() for model in (TestItemRow, AiDraft, Task, RunEvidence, Project))
        versions = tuple(db.session.query(TestItemRow.id, TestItemRow.version, TestItemRow.review_status).all())
        db.session.rollback()
        return counts, versions
    draft_id = make_draft(audit_env)
    attempt, _root, _task_id = make_attempt(audit_env)
    before = state()
    collect(audit_env, draft_ids=[draft_id], run_attempts=[attempt])
    assert state() == before


def test_transaction_is_repeatable_read_and_rejects_database_write(audit_env):
    observed = []
    engine = db.engine
    def inspect_transaction(connection, cursor, statement, parameters, context, executemany):
        if observed or not statement.lstrip().upper().startswith("SELECT"):
            return
        observed.append(True)
        assert connection.exec_driver_sql("SHOW transaction_isolation").scalar() == "repeatable read"
        assert connection.exec_driver_sql("SHOW transaction_read_only").scalar() == "on"
        with pytest.raises(DBAPIError) as failure:
            connection.exec_driver_sql("UPDATE lm_test_items SET version = version + 1")
        assert failure.value.orig.pgcode == "25006"
    event.listen(engine, "before_cursor_execute", inspect_transaction)
    try:
        with pytest.raises(audit_module().PilotAuditError):
            collect(audit_env)
    finally:
        event.remove(engine, "before_cursor_execute", inspect_transaction)
    assert observed == [True]
    assert not db.session().in_transaction()
    assert {row.version for row in TestItemRow.query.all()} == {3}


@pytest.mark.parametrize("authority", ["outsider", "disabled", "missing", "deleted_project"])
def test_denies_access_with_safe_typed_error(audit_env, authority):
    actor = audit_env["reader"]
    if authority == "outsider":
        actor = audit_env["outsider"]
    elif authority == "disabled":
        db.session.get(LMUser, actor).status = "disabled"
    elif authority == "missing":
        actor = 999999
    else:
        db.session.get(Project, audit_env["project"]).deleted_at = datetime.utcnow()
    db.session.commit()
    with pytest.raises(audit_module().PilotAuditError) as failure:
        audit_module().collect_snapshot(pilot(audit_env), actor_id=actor)
    assert failure.value.code == "PROJECT_ACCESS_DENIED"
    assert str(audit_env["root"]) not in str(failure.value)
    assert not db.session().in_transaction()


def test_system_admin_authority_does_not_require_membership(audit_env):
    assert audit_module().collect_snapshot(pilot(audit_env), actor_id=audit_env["admin"]).preflight_issues == []


@pytest.mark.parametrize("condition", ["dirty", "active"])
def test_rejects_and_preserves_unrelated_session_work(audit_env, condition):
    session = db.session()
    if condition == "dirty":
        pending = LMUser(username="unrelated", password_hash="unused")
        session.add(pending)
    else:
        session.execute(text("SELECT 1"))
    transaction = session.get_transaction()
    with pytest.raises(audit_module().PilotAuditError) as failure:
        collect(audit_env)
    assert failure.value.code == "SESSION_NOT_CLEAN"
    assert session.get_transaction() is transaction
    if condition == "dirty":
        assert pending in session.new and pending.id is None


@pytest.mark.parametrize("mutation,issue", [
    ("version", "row_version_mismatch"), ("module", "row_module_mismatch"),
    ("foreign", "row_unavailable"), ("deleted", "row_unavailable"), ("sheet", "row_unavailable"),
    ("project", "project_not_active"), ("model_version", "model_version_mismatch"),
    ("model_foreign", "model_unavailable"), ("model_deprecated", "model_unavailable"),
    ("model_digest", "model_digest_mismatch"), ("model_missing", "model_file_unavailable"),
])
def test_current_preflight_is_scoped_and_explicit(audit_env, mutation, issue):
    row = db.session.get(TestItemRow, audit_env["rows"][0])
    model = db.session.get(ProjectModel, audit_env["model"])
    if mutation == "version":
        row.version += 1
    elif mutation == "module":
        row.module = "foreign-module"
    elif mutation == "foreign":
        row.project_id = audit_env["foreign"]
    elif mutation == "deleted":
        row.deleted_at = datetime.utcnow()
    elif mutation == "sheet":
        row.sheet = "lib"
    elif mutation == "project":
        db.session.get(Project, audit_env["project"]).status = "frozen"
    elif mutation == "model_version":
        model.version = "v2"
    elif mutation == "model_foreign":
        model.project_id = audit_env["foreign"]
    elif mutation == "model_deprecated":
        model.deprecated_at = datetime.utcnow()
    elif mutation == "model_digest":
        audit_env["path"].write_text("changed", encoding="utf-8")
    else:
        audit_env["path"].unlink()
    db.session.commit()
    assert issue in collect(audit_env).preflight_issues


def test_foreign_and_missing_requests_have_identical_generic_observations(audit_env):
    draft_id = make_draft(audit_env)
    db.session.get(AiDraft, draft_id).project_id = audit_env["foreign"]
    attempt, _root, task_id = make_attempt(audit_env)
    db.session.get(Task, task_id).project_id = audit_env["foreign"]
    db.session.commit()
    missing = {**attempt, "task_key": "T999999"}
    snapshot = collect(audit_env, draft_ids=[draft_id, 999999], run_attempts=[attempt, missing])
    assert len(snapshot.drafts) == 2 and len(snapshot.runs) == 2
    assert [entry.issues for entry in snapshot.drafts] == [["draft_unavailable"]] * 2
    assert [entry.issues for entry in snapshot.runs] == [["run_unavailable"]] * 2


@pytest.mark.parametrize("receipt,expected", [
    ({"schema_version": 1, "attempted_calls": 3, "successful_calls": 2, "api_response_calls": 2}, "live"),
    ({"schema_version": 1, "attempted_calls": 0, "successful_calls": 0, "api_response_calls": 0}, "unknown"),
    ({"schema_version": 1, "attempted_calls": 2, "successful_calls": 2, "api_response_calls": 1}, "unknown"),
    ({"schema_version": 1, "attempted_calls": 1, "successful_calls": 2, "api_response_calls": 2}, "unknown"),
    ({"schema_version": True, "attempted_calls": 1, "successful_calls": 1, "api_response_calls": 1}, "unknown"),
    ({"schema_version": 1, "attempted_calls": True, "successful_calls": 1, "api_response_calls": 1}, "unknown"),
    ({"schema_version": 1, "attempted_calls": 1, "successful_calls": 1.0, "api_response_calls": 1}, "unknown"),
    ({"schema_version": 1, "attempted_calls": 1, "successful_calls": 1, "api_response_calls": "1"}, "unknown"),
    ({"schema_version": 1, "attempted_calls": -1, "successful_calls": -1, "api_response_calls": -1}, "unknown"),
    ({"schema_version": 2, "attempted_calls": 1, "successful_calls": 1, "api_response_calls": 1}, "unknown"),
    ({"schema_version": 1, "attempted_calls": 1}, "unknown"), (None, "unknown"), ("live", "unknown"),
])
def test_provider_provenance_requires_actual_strict_production_receipt(audit_env, receipt, expected):
    identity = make_draft(audit_env, meta={"model": "live-looking", "provider_kind": "live",
                                         "provider_provenance": receipt})
    assert collect(audit_env, draft_ids=[identity]).drafts[0].provider_kind == expected


def test_partial_approval_counts_actual_applied_selected_refs_only(audit_env):
    identity = make_draft(audit_env, status="approved", accepted=audit_env["rows"][:1])
    db.session.get(TestItemRow, audit_env["rows"][0]).version = 4
    db.session.commit()
    observation = collect(audit_env, draft_ids=[identity]).drafts[0]
    assert observation.generated_item_ids == audit_env["rows"][:2]
    assert observation.accepted_item_ids == audit_env["rows"][:1]
    assert observation.conversion_item_ids == audit_env["rows"][:2]
    assert observation.provider_kind == "unknown"


def test_legacy_single_procedure_and_conversion_failure(audit_env):
    identity = make_draft(audit_env, status="approved", legacy=True)
    assert collect(audit_env, draft_ids=[identity]).drafts[0].accepted_item_ids == audit_env["rows"][:1]
    draft = db.session.get(AiDraft, identity)
    draft.output_json = json.dumps({"steps_doc": steps_document("UNKNOWN+1")})
    db.session.commit()
    observation = collect(audit_env, draft_ids=[identity]).drafts[0]
    assert observation.conversion_item_ids == []
    assert "draft_conversion_failed" in observation.issues
    assert "UNKNOWN+1" not in observation.model_dump_json()


@pytest.mark.parametrize("mutation", ["base_version", "context_project", "model", "unselected", "malformed", "applied"])
def test_draft_source_identity_cannot_be_forged_or_widened(audit_env, mutation):
    identity = make_draft(audit_env, status="approved", accepted=audit_env["rows"][:1])
    draft = db.session.get(AiDraft, identity)
    source = json.loads(draft.input_json)
    if mutation == "base_version":
        source["_context"]["items"][0]["version"] = 2
    elif mutation == "context_project":
        source["_context"]["project_id"] = audit_env["foreign"]
    elif mutation == "model":
        source["_context"]["model"]["id"] = 999999
    elif mutation == "unselected":
        source["viewpoints"][0]["item_id"] = 999999
        source["_context"]["items"][0]["id"] = 999999
    elif mutation == "malformed":
        draft.input_json = "not-json PRIVATE-DOCUMENT"
    else:
        draft.applied_result_json = json.dumps({"applied": [{"ref": str(audit_env["rows"][0]), "item_id": 999999}]})
    if mutation not in ("malformed", "applied"):
        draft.input_json = json.dumps(source)
    db.session.commit()
    observation = collect(audit_env, draft_ids=[identity]).drafts[0]
    assert 999999 not in observation.generated_item_ids + observation.accepted_item_ids
    assert observation.issues
    if mutation in ("base_version", "context_project", "model", "malformed"):
        assert observation.accepted_item_ids == []
    assert "PRIVATE-DOCUMENT" not in observation.model_dump_json()


def test_exact_prior_attempt_survives_retest_and_current_row_model_changes(audit_env):
    request, _root, task_id = make_attempt(audit_env)
    task = db.session.get(Task, task_id)
    second, _second_root, _task_id = make_attempt(audit_env, count=2, task=task, kind="synthetic", backend="mock")
    db.session.get(TestItemRow, request["item_id"]).version = 5
    db.session.get(ProjectModel, audit_env["model"]).version = "v2"
    audit_env["path"].write_text("current saved model changed", encoding="utf-8")
    db.session.commit()
    snapshot = collect(audit_env, run_attempts=[request, second])
    assert "row_version_mismatch" in snapshot.preflight_issues
    assert "model_version_mismatch" in snapshot.preflight_issues
    assert snapshot.runs[0].verified and snapshot.runs[0].finalised
    assert snapshot.runs[0].evidence_kind == "silver_runtime"
    assert snapshot.runs[0].runner_backend == "silver"
    assert snapshot.runs[1].evidence_kind == "synthetic"
    assert snapshot.runs[1].runner_backend == "mock"


@pytest.mark.parametrize("kind,backend,expected_kind,expected_backend", [
    ("synthetic", "mock", "synthetic", "mock"), ("unclassified", "", "unclassified", "unknown"),
    ("silver_runtime", "mock", "synthetic", "mock"), ("synthetic", "silver", "synthetic", "silver"),
    ("invented", "invented", "unclassified", "unknown"),
])
def test_simulation_and_unclassified_evidence_never_become_real(audit_env, kind, backend, expected_kind, expected_backend):
    request, _root, _task_id = make_attempt(audit_env, kind=kind, backend=backend)
    observation = collect(audit_env, run_attempts=[request]).runs[0]
    assert observation.evidence_kind == expected_kind
    assert observation.runner_backend == expected_backend
    assert observation.verified


@pytest.mark.parametrize("mutation,issue", [
    ("manifest", "run_evidence_invalid"), ("results", "run_evidence_invalid"),
    ("model", "run_evidence_invalid"), ("outcome", "run_evidence_invalid"),
    ("missing_record", "run_evidence_missing"), ("missing_file", "run_evidence_missing"),
    ("row", "run_row_identity_mismatch"), ("row_uuid", "run_row_identity_mismatch"),
    ("model_id", "run_model_mismatch"), ("model_version", "run_model_mismatch"),
    ("original_digest", "run_model_mismatch"), ("artifacts", "run_artifacts_missing"),
    ("unsealed", "run_not_sealed"), ("unfinalised", "run_not_finalised"),
])
def test_missing_tampered_or_misbound_archive_is_not_verified(audit_env, mutation, issue):
    request, root, task_id = make_attempt(audit_env, sealed=mutation != "unsealed", finalised=mutation != "unfinalised")
    if mutation == "manifest":
        (root / "manifest.json").write_text("{}", encoding="utf-8")
    elif mutation == "results":
        (root / "results" / "output.csv").write_text("tampered", encoding="utf-8")
    elif mutation == "model":
        (root / "model" / "approved.sil").write_text("tampered", encoding="utf-8")
    elif mutation == "outcome":
        (root / "outcome.json").write_text("{}", encoding="utf-8")
    elif mutation == "missing_record":
        db.session.delete(db.session.get(RunEvidence, (task_id, 1)))
    elif mutation == "missing_file":
        (root / "manifest.json").unlink()
    elif mutation in ("row", "row_uuid"):
        key = "id" if mutation == "row" else "uuid"
        rewrite_manifest(task_id, root, lambda data: data["approved_inputs"]["row"].update({key: "wrong"}))
    elif mutation in ("model_id", "model_version", "original_digest"):
        key = {"model_id": "id", "model_version": "version", "original_digest": "sha256"}[mutation]
        rewrite_manifest(task_id, root, lambda data: data["model"].update({key: "wrong"}))
    elif mutation == "artifacts":
        (root / "results" / "Console.log").unlink()
        path = root / "outcome.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        data["result_files"] = evidence._hashes(root / "results")
        path.write_text(json.dumps(data), encoding="utf-8")
        db.session.get(RunEvidence, (task_id, 1)).outcome_sha256 = hashlib.sha256(path.read_bytes()).hexdigest()
    db.session.commit()
    observation = collect(audit_env, run_attempts=[request]).runs[0]
    assert not observation.verified
    assert issue in observation.issues
    assert str(audit_env["root"]) not in observation.model_dump_json()
    assert not db.session().in_transaction()


def test_archive_reader_receives_explicit_attempt_and_zero_log_budget(audit_env, monkeypatch):
    request, _root, _task_id = make_attempt(audit_env)
    original = evidence.read_evidence
    calls = []
    def checked_reader(task, run_count=None, *, max_log_bytes=262144):
        calls.append((isinstance(task, Task), run_count, max_log_bytes))
        return original(task, run_count, max_log_bytes=max_log_bytes)
    monkeypatch.setattr(evidence, "read_evidence", checked_reader)
    assert collect(audit_env, run_attempts=[request]).runs[0].verified
    assert calls == [(True, 1, 0)]
