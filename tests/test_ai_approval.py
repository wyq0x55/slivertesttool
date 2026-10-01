from __future__ import annotations

import copy
import datetime
import hashlib
import json
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.extensions import db
from app.models import AiDraft, AiSignalDict, CellComment, LMUser, Project, ProjectModel, SbsRevision, TestItemRow as ItemRow
from app.services.ai import apply as ai_apply, validators
from app.services.lanmatrix import fields, fields_service, items_service, projects_service
from app.services.lanmatrix.errors import ServiceError
from app.services.lanmatrix.silver_json_export import build_lib


def steps_doc():
    return {
        "input_signals": [["Speed", "speed"]],
        "expected_signals": [["Warning", "warning"]],
        "steps": [{"no": 1, "inputs": ["120"], "expecteds": ["1"]}],
    }


def viewpoint_output():
    return {"module_id": "M1", "viewpoints": [
        {"case_id": "M1-1", "title": "Warning", "kind": "normal", "expected": "On"},
    ]}


def failure_output():
    return {"classification": "test_error", "analysis": "Mismatch", "likely_cause": "Input",
            "suggested_action": "Review input"}


def procedure_payload():
    return {"viewpoints": [{"ref": "R1", "item_id": 1, "version": 1},
                           {"ref": "R2", "item_id": 2, "version": 1}],
            "sbs_variables": [["Speed", "speed"], ["Warning", "warning"]],
            "lib_functions": [],
            "_context": {"schema_version": 1, "project_id": 1,
                         "items": [{"id": 1, "version": 1}, {"id": 2, "version": 1}],
                         "model": None, "provenance": []}}


def procedure_output():
    return {"procedures": [{"ref": "R1", "steps_doc": steps_doc()},
                           {"ref": "R2", "steps_doc": steps_doc()}], "failed_refs": []}


@pytest.mark.parametrize("scenario,output", [
    ("viewpoint", viewpoint_output()),
    ("procedure", procedure_output()),
    ("sbs", {"sbs_additions": "variable speed;", "needed_variables": [{"name": "speed"}]}),
    ("lib", {"lib_name": "set_speed", "lib_para": [{"name": "value", "default": 0}],
             "lib_stb": steps_doc(), "rewritten": [{"item_id": 1, "steps_doc": steps_doc()}]}),
    ("failure", failure_output()),
])
def test_shared_validator_accepts_five_scenarios(scenario, output):
    payload = procedure_payload()
    payload.update(source_files={"engine.c": "int speed; int warning;"},
                   procedures=[{"item_id": 1, "version": 1}], existing_lib_names=[])
    assert validators.validate_output(scenario, payload, output) == []


@pytest.mark.parametrize("scenario", AiDraft.SCENARIOS)
@pytest.mark.parametrize("output", [None, [], "bad", {}])
def test_shared_validator_reports_malformed_output(scenario, output):
    assert validators.validate_output(scenario, procedure_payload(), output)


@pytest.mark.parametrize("refs", [[], ["R1", "R1"], ["unknown"], [1], "R1"])
def test_selection_must_be_nonempty_unique_existing_strings(refs):
    assert validators.validate_output("procedure", procedure_payload(), procedure_output(),
                                      refs=refs, for_apply=True)


def test_partial_validation_ignores_only_unselected_bad_entries():
    output = procedure_output()
    output["procedures"][1]["steps_doc"] = {"steps": "invalid"}
    output["procedures"][1]["missing_variables"] = [{"name": "ghost"}]
    assert validators.validate_output("procedure", procedure_payload(), output, refs=["R1"], for_apply=True) == []
    assert validators.validate_output("procedure", procedure_payload(), output, refs=["R2"], for_apply=True)


def test_generation_can_report_missing_but_approval_cannot():
    output = procedure_output()
    output["procedures"][0]["missing_variables"] = [{"name": "ghost"}]
    assert validators.validate_output("procedure", procedure_payload(), output) == []
    assert validators.validate_output("procedure", procedure_payload(), output, for_apply=True)


@pytest.mark.parametrize("mutation", ["signal", "subroutine", "alignment", "duplicate_no", "boolean_no", "cell"])
def test_shared_validation_rejects_invalid_selected_execution_inputs(mutation):
    output = procedure_output()
    doc = output["procedures"][0]["steps_doc"]
    if mutation == "signal":
        doc["input_signals"][0][1] = "ghost"
    elif mutation == "subroutine":
        doc["steps"][0]["subroutine"] = "ghost"
    elif mutation == "alignment":
        doc["steps"][0]["inputs"] = ["120", "999"]
    elif mutation == "duplicate_no":
        doc["steps"].append(copy.deepcopy(doc["steps"][0]))
    elif mutation == "boolean_no":
        doc["steps"][0]["no"] = True
    else:
        doc["steps"][0]["inputs"] = [{"value": "120"}]
    assert validators.validate_output("procedure", procedure_payload(), output, refs=["R1"], for_apply=True)


@pytest.fixture()
def approval_env(app_ctx, monkeypatch, tmp_path):
    from app.services.ai import provider
    monkeypatch.setattr(provider, "chat", lambda *args, **kwargs: pytest.fail("Approval must not call a provider"))
    with app_ctx.app_context():
        admin = LMUser.query.filter_by(is_system_admin=True).first()
        project = projects_service.create_project(admin, code="APPROVAL", name="Approval")
        fields_service.ensure_fields(admin, project, fields.TEST_FIELDS + fields.LIB_FIELDS)
        rows = [items_service.create_item(admin, project, {"test_id": f"T{index}", "test_name": f"Case {index}"},
                                          sheet="test", draft=True) for index in (1, 2)]
        bundle = tmp_path / "bundle"
        bundle.mkdir()
        sbs = bundle / "engine.sbs"
        sbs.write_text("variable speed;\n", encoding="utf-8")
        model = ProjectModel(project_id=project.id, name="Engine", version="v1", kind="bundle",
                             sil_path=str(bundle / "engine.sil"), bundle_dir=str(bundle))
        db.session.add(model)
        db.session.commit()
        yield {"app": app_ctx, "user": admin, "project": project, "rows": rows, "model": model, "sbs": sbs}


def make_draft(env, scenario="procedure", payload=None, output=None):
    from app.services.lanmatrix import sbs_service
    rows = env["rows"]
    model = env["model"]
    canonical = procedure_payload()
    canonical["viewpoints"] = [{"ref": f"R{index}", "item_id": row.id, "version": row.version}
                                for index, row in enumerate(rows, 1)]
    canonical["procedures"] = [{"item_id": row.id, "version": row.version} for row in rows]
    canonical["item_id"] = rows[0].id
    canonical["_context"].update(project_id=env["project"].id,
                                 items=[{"id": row.id, "version": row.version} for row in rows])
    canonical.update(source_files={"engine.c": "int speed; int warning;"}, existing_lib_names=[])
    if scenario == "sbs":
        saved = sbs_service.read_sbs(env["project"].id, model.name, model_id=model.id)
        canonical.update(model_id=model.id, current_sbs=saved["content"])
        canonical["_context"]["model"] = {"id": model.id, "name": model.name, "version": model.version,
                                          "sbs_sha256": saved["version"]}
    if payload is not None:
        canonical = payload
    defaults = {"viewpoint": viewpoint_output(), "procedure": procedure_output(),
                "sbs": {"sbs_additions": "variable warning;", "needed_variables": [{"name": "warning"}]},
                "lib": {"lib_name": "set_speed", "description": "Set speed", "lib_para": [{"name": "value", "default": 0}],
                        "lib_stb": steps_doc(), "rewritten": [{"item_id": rows[0].id, "steps_doc": steps_doc()}]},
                "failure": failure_output()}
    draft = AiDraft(project_id=env["project"].id, scenario=scenario, input_json=json.dumps(canonical),
                    output_json=json.dumps(defaults[scenario] if output is None else output), created_by=env["user"].id)
    db.session.add(draft)
    db.session.commit()
    return draft


def login(env):
    client = env["app"].test_client()
    with client.session_transaction() as session:
        session["lm_user_id"] = env["user"].id
        session["csrf_token"] = "approval-csrf"
    return client, {"X-CSRF-Token": "approval-csrf"}


@pytest.mark.parametrize("mutation", ["missing_context", "missing_version", "stale", "deleted", "foreign", "wrong_sheet", "context_mismatch"])
def test_approval_rejects_unsafe_generation_snapshots(approval_env, mutation):
    env = approval_env
    draft = make_draft(env)
    row = env["rows"][0]
    payload = json.loads(draft.input_json)
    if mutation == "missing_context":
        payload.pop("_context")
    elif mutation == "missing_version":
        payload["viewpoints"][0].pop("version")
    elif mutation == "stale":
        row.version += 1
    elif mutation == "deleted":
        row.deleted_at = datetime.datetime.utcnow()
    elif mutation == "foreign":
        other = projects_service.create_project(env["user"], code="OTHER", name="Other")
        row.project_id = other.id
    elif mutation == "wrong_sheet":
        row.sheet = "lib"
    else:
        payload["_context"]["items"][0]["version"] += 1
    draft.input_json = json.dumps(payload)
    db.session.commit()
    with pytest.raises(ai_apply.ApplyError):
        ai_apply.apply_draft(draft, env["user"], refs=["R1"])
    db.session.expire_all()
    assert draft.status == "pending"
    assert not row.get_field("steps")


def test_partial_approval_applies_valid_row_and_records_terminal_decision(approval_env):
    env = approval_env
    output = procedure_output()
    output["procedures"][1].update(steps_doc={}, missing_variables=[{"name": "ghost"}])
    draft = make_draft(env, output=output)
    env["rows"][1].deleted_at = datetime.datetime.utcnow()
    db.session.commit()
    result = ai_apply.apply_draft(draft, env["user"], refs=["R1"])
    assert [entry["ref"] for entry in result["applied"]] == ["R1"]
    assert [entry["ref"] for entry in result["skipped"]] == ["R2"]
    assert draft.status == "approved" and draft.reviewed_by == env["user"].id
    assert json.loads(env["rows"][0].get_field("steps")) == steps_doc()
    assert not env["rows"][1].get_field("steps")
    with pytest.raises(ai_apply.ApplyError):
        ai_apply.apply_draft(draft, env["user"], refs=["R2"])


@pytest.mark.parametrize("scenario", AiDraft.SCENARIOS)
def test_approval_commits_once_only_after_review_metadata(approval_env, monkeypatch, scenario):
    env = approval_env
    draft = make_draft(env, scenario)
    commits = []
    original_commit = db.session.commit
    def checked_commit():
        commits.append(draft.status)
        assert draft.status == "approved"
        assert draft.reviewed_by == env["user"].id and draft.reviewed_at is not None
        assert json.loads(draft.applied_result_json)
        original_commit()
    monkeypatch.setattr(db.session, "commit", checked_commit)
    ai_apply.apply_draft(draft, env["user"])
    assert commits == ["approved"]


@pytest.mark.parametrize("scenario", AiDraft.SCENARIOS)
def test_commit_failure_rolls_back_assets_and_review(approval_env, monkeypatch, scenario):
    env = approval_env
    draft = make_draft(env, scenario)
    def broken_commit():
        raise RuntimeError("commit failed")
    monkeypatch.setattr(db.session, "commit", broken_commit)
    with pytest.raises((ai_apply.ApplyError, RuntimeError)):
        ai_apply.apply_draft(draft, env["user"])
    with Session(db.engine) as observer:
        assert observer.get(AiDraft, draft.id).status == "pending"
        assert observer.query(ItemRow).count() == 2
        assert observer.query(SbsRevision).count() == 0
        assert observer.query(CellComment).count() == 0
    assert not env["rows"][0].get_field("steps")
    assert draft.status == "pending" and draft.reviewed_by is None


def test_later_asset_failure_rolls_back_earlier_writes(approval_env, monkeypatch):
    env = approval_env
    draft = make_draft(env, "viewpoint", output={"module_id": "M1", "viewpoints": [
        viewpoint_output()["viewpoints"][0], dict(viewpoint_output()["viewpoints"][0], case_id="M1-2")]})
    original_create = items_service.create_item
    calls = []
    def create_or_fail(*args, **kwargs):
        calls.append(1)
        if len(calls) == 2:
            raise ServiceError("Second asset rejected")
        return original_create(*args, **kwargs)
    monkeypatch.setattr(items_service, "create_item", create_or_fail)
    with pytest.raises(ai_apply.ApplyError):
        ai_apply.apply_draft(draft, env["user"])
    assert ItemRow.query.count() == 2
    assert draft.status == "pending"


def test_library_parameters_round_trip_through_actual_exporter(approval_env):
    env = approval_env
    body = steps_doc()
    body["steps"][0]["inputs"] = ["value"]
    draft = make_draft(env, "lib", output={"lib_name": "set_speed", "lib_para": [
        {"name": "value", "default": 0}, {"name": "limit", "default": "0x10"}, {"name": "bare"}],
        "lib_stb": body, "rewritten": [{"item_id": env["rows"][0].id, "steps_doc": steps_doc()}]})
    result = ai_apply.apply_draft(draft, env["user"])
    row = db.session.get(ItemRow, result["lib_item_id"])
    exported = build_lib([row])["subroutines"]["set_speed"]
    assert row.get_field("lib_para") == "value=0\nlimit=0x10\nbare"
    assert exported["params"] == [{"name": "value", "default": 0}, {"name": "limit", "default": 16}, {"name": "bare"}]
    assert exported["steps"][0]["inputs"] == [{"var": "speed", "value": 0, "param": "value"}]


@pytest.mark.parametrize("mutation", ["missing_version", "foreign_rewrite", "deleted", "stale"])
def test_library_rewrites_require_generation_snapshot(approval_env, mutation):
    env = approval_env
    draft = make_draft(env, "lib")
    payload = json.loads(draft.input_json)
    if mutation == "missing_version":
        payload["procedures"][0].pop("version")
    elif mutation == "foreign_rewrite":
        output = json.loads(draft.output_json)
        output["rewritten"][0]["item_id"] = 9999
        draft.output_json = json.dumps(output)
    elif mutation == "deleted":
        env["rows"][0].deleted_at = datetime.datetime.utcnow()
    else:
        env["rows"][0].version += 1
    draft.input_json = json.dumps(payload)
    db.session.commit()
    with pytest.raises(ai_apply.ApplyError):
        ai_apply.apply_draft(draft, env["user"])
    assert ItemRow.query.count() == 2 and draft.status == "pending"


@pytest.mark.parametrize("mutation", ["hash", "identity", "missing_hash", "missing_model", "deprecated"])
def test_sbs_rejects_changed_or_missing_saved_model(approval_env, mutation):
    env = approval_env
    draft = make_draft(env, "sbs")
    if mutation == "hash":
        env["sbs"].write_text("changed", encoding="utf-8")
    elif mutation == "identity":
        env["model"].version = "v2"
    elif mutation == "missing_hash":
        payload = json.loads(draft.input_json)
        payload["_context"]["model"].pop("sbs_sha256")
        draft.input_json = json.dumps(payload)
    elif mutation == "missing_model":
        db.session.delete(env["model"])
    else:
        env["model"].deprecated_at = datetime.datetime.utcnow()
    db.session.commit()
    with pytest.raises(ai_apply.ApplyError):
        ai_apply.apply_draft(draft, env["user"])
    assert SbsRevision.query.count() == 0 and draft.status == "pending"


def test_sbs_candidate_does_not_activate_or_write_file(approval_env):
    env = approval_env
    draft = make_draft(env, "sbs")
    original = env["sbs"].read_bytes()
    result = ai_apply.apply_draft(draft, env["user"])
    revision = db.session.get(SbsRevision, result["sbs_revision_id"])
    assert revision.content == "variable speed;\n\nvariable warning;"
    assert env["sbs"].read_bytes() == original
    assert not env["model"].is_current


def test_failure_approval_creates_comment_only(approval_env):
    env = approval_env
    draft = make_draft(env, "failure")
    before = env["rows"][0].to_dict()
    result = ai_apply.apply_draft(draft, env["user"])
    assert db.session.get(CellComment, result["comment_id"]).test_item_id == env["rows"][0].id
    assert env["rows"][0].to_dict() == before
    assert ItemRow.query.count() == 2 and SbsRevision.query.count() == 0


def test_apply_refreshes_identity_mapped_draft_and_row(approval_env):
    env = approval_env
    draft = make_draft(env)
    with Session(db.engine) as other:
        other.get(ItemRow, env["rows"][0].id).version += 1
        other.commit()
    with pytest.raises(ai_apply.ApplyError):
        ai_apply.apply_draft(draft, env["user"], refs=["R1"])
    with Session(db.engine) as other:
        other.get(AiDraft, draft.id).status = "rejected"
        other.commit()
    with pytest.raises(ai_apply.ApplyError):
        ai_apply.apply_draft(draft, env["user"], refs=["R1"])
    assert not env["rows"][0].get_field("steps")


def test_pg_approval_holds_draft_and_row_locks_until_single_commit(approval_env, monkeypatch):
    env = approval_env
    draft = make_draft(env)
    draft_id, row_id, user_id = draft.id, env["rows"][0].id, env["user"].id
    engine = db.engine
    reached = threading.Event()
    release = threading.Event()
    original_update = items_service.update_item
    def paused_update(*args, **kwargs):
        reached.set()
        assert release.wait(10)
        return original_update(*args, **kwargs)
    monkeypatch.setattr(items_service, "update_item", paused_update)
    db.session.remove()
    def approve():
        with env["app"].app_context():
            return ai_apply.apply_draft(db.session.get(AiDraft, draft_id), db.session.get(LMUser, user_id), refs=["R1"])
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(approve)
        try:
            assert reached.wait(10)
            with Session(engine) as observer:
                assert observer.get(AiDraft, draft_id).status == "pending"
                for model_type, identity in ((AiDraft, draft_id), (ItemRow, row_id)):
                    with pytest.raises(Exception) as conflict:
                        observer.execute(text("SET LOCAL lock_timeout = '200ms'"))
                        observer.query(model_type).filter(model_type.id == identity).with_for_update().one()
                    assert "lock timeout" in str(conflict.value)
                    observer.rollback()
        finally:
            release.set()
        assert future.result(timeout=10)["applied"][0]["item_id"] == row_id


@pytest.mark.parametrize("scenario,invalid", [
    ("viewpoint", {"module_id": "M1", "viewpoints": []}),
    ("procedure", {"procedures": [{"ref": "R1", "steps_doc": {}}]}),
    ("sbs", {"sbs_additions": "{"}),
    ("lib", {"lib_name": "bad", "lib_stb": {}}),
    ("failure", {"classification": "code_bug"}),
])
def test_manual_edit_reuses_shared_validation_and_preserves_output(approval_env, scenario, invalid):
    env = approval_env
    draft = make_draft(env, scenario)
    before = draft.output_json
    client, headers = login(env)
    response = client.put(f"/api/v1/ai/drafts/{draft.id}", headers=headers, json={"output": invalid})
    assert response.status_code == 400
    db.session.refresh(draft)
    assert draft.output_json == before


def test_route_partial_selection_and_collab_guard(approval_env, monkeypatch):
    env = approval_env
    draft = make_draft(env)
    client, headers = login(env)
    response = client.post(f"/api/v1/ai/drafts/{draft.id}/approve", headers=headers, json={"refs": []})
    assert response.status_code == 400
    from app.collab import presence
    monkeypatch.setattr(presence, "is_collab_active", lambda project_id: True)
    response = client.post(f"/api/v1/ai/drafts/{draft.id}/approve", headers=headers, json={"refs": ["R1"]})
    assert response.status_code == 409
    db.session.refresh(draft)
    assert draft.status == "pending" and not env["rows"][0].get_field("steps")


@pytest.mark.parametrize("method,suffix,body", [("put", "", {"output": viewpoint_output()}),
                                               ("post", "/reject", {"note": "No"})])
def test_edit_and_reject_reload_locked_terminal_status(approval_env, monkeypatch, method, suffix, body):
    env = approval_env
    draft = make_draft(env, "viewpoint")
    client, headers = login(env)
    from app.routes.lanmatrix import ai as ai_routes
    original_require = ai_routes._require_edit
    def concurrent_approval(project_id):
        original_require(project_id)
        with Session(db.engine) as other:
            other.get(AiDraft, draft.id).status = "approved"
            other.commit()
    monkeypatch.setattr(ai_routes, "_require_edit", concurrent_approval)
    response = getattr(client, method)(f"/api/v1/ai/drafts/{draft.id}{suffix}", headers=headers, json=body)
    assert response.status_code == 409
    db.session.refresh(draft)
    assert draft.status == "approved"


@pytest.mark.parametrize("scenario", ["viewpoint", "lib", "failure", "sbs"])
def test_applied_assets_keep_draft_and_source_provenance(approval_env, scenario):
    env = approval_env
    draft = make_draft(env, scenario)
    payload = json.loads(draft.input_json)
    sources = [{"kind": "submitted_source", "name": "engine.c", "revision": "abc123", "sha256": "a" * 64}]
    payload["_context"]["provenance"] = sources
    draft.input_json = json.dumps(payload)
    db.session.commit()
    result = ai_apply.apply_draft(draft, env["user"])
    assert result["draft_id"] == draft.id
    assert result["provenance"]["sources"] == sources
    assert len(result["provenance"]["sha256"]) == 64
    if scenario == "viewpoint":
        evidence = db.session.get(ItemRow, result["created_item_ids"][0]).get_field("remark")
    elif scenario == "lib":
        evidence = db.session.get(ItemRow, result["lib_item_id"]).get_field("lib_note")
    elif scenario == "failure":
        evidence = db.session.get(CellComment, result["comment_id"]).content
    else:
        assert result["model_snapshot"] == payload["_context"]["model"]
        assert result["sbs_base_sha256"] == payload["_context"]["model"]["sbs_sha256"]
        return
    assert f'"draft_id": {draft.id}' in evidence
    assert "engine.c" in evidence and "abc123" in evidence and "a" * 64 in evidence


@pytest.mark.parametrize("scenario", ["procedure", "lib"])
@pytest.mark.parametrize("mutation", ["stale", "deleted", "foreign", "missing"])
def test_approval_rejects_changed_source_dependencies(approval_env, scenario, mutation):
    env = approval_env
    dependency = items_service.create_item(env["user"], env["project"], {"lib_func": "existing", "lib_stb": json.dumps(steps_doc())},
                                           draft=True, sheet="lib")
    draft = make_draft(env, scenario)
    payload = json.loads(draft.input_json)
    payload["_context"]["dependencies"] = [{"id": dependency.id, "version": dependency.version}]
    draft.input_json = json.dumps(payload)
    if mutation == "stale":
        dependency.version += 1
    elif mutation == "deleted":
        dependency.deleted_at = datetime.datetime.utcnow()
    elif mutation == "foreign":
        other = projects_service.create_project(env["user"], code="OTHER", name="Other")
        dependency.project_id = other.id
    else:
        db.session.delete(dependency)
    db.session.commit()
    with pytest.raises(ai_apply.ApplyError):
        ai_apply.apply_draft(draft, env["user"], refs=["R1"] if scenario == "procedure" else None)
    assert draft.status == "pending" and not env["rows"][0].get_field("steps")


@pytest.mark.parametrize("scenario", ["procedure", "lib"])
def test_approval_rejects_changed_signal_dictionary(approval_env, scenario):
    env = approval_env
    from app.services.ai import signal_dict
    draft = make_draft(env, scenario)
    payload = json.loads(draft.input_json)
    encoded = json.dumps(signal_dict.entries_for(env["project"].id), ensure_ascii=False, sort_keys=True)
    payload["_context"]["signal_dict_sha256"] = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
    draft.input_json = json.dumps(payload)
    db.session.add(AiSignalDict(project_id=env["project"].id, path="new_path", display="New", type="int"))
    db.session.commit()
    with pytest.raises(ai_apply.ApplyError):
        ai_apply.apply_draft(draft, env["user"])
    assert draft.status == "pending" and not env["rows"][0].get_field("steps")


@pytest.mark.parametrize("scenario", ["viewpoint", "procedure", "lib"])
@pytest.mark.parametrize("state", ["active", "unavailable"])
def test_direct_apply_preserves_fail_closed_crdt_guard(approval_env, monkeypatch, scenario, state):
    env = approval_env
    draft = make_draft(env, scenario)
    from app.collab import presence
    def collab_state(project_id):
        if state == "unavailable":
            raise RuntimeError("Presence unavailable")
        return True
    monkeypatch.setattr(presence, "is_collab_active", collab_state)
    with pytest.raises(ai_apply.ApplyError):
        ai_apply.apply_draft(draft, env["user"])
    assert draft.status == "pending" and ItemRow.query.count() == 2
