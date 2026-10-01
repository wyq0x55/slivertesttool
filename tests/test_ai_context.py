import hashlib
import json

import pytest

from test_ai_api import _admin, _configure_ai, _login, project_env


def _row(app_ctx, project_id, *, sheet="test", case_id="C1"):
    with app_ctx.app_context():
        from app.extensions import db
        from app.models import TestItemRow

        row = TestItemRow(project_id=project_id, sheet=sheet, case_id=case_id,
                          title="Stored viewpoint", version=4,
                          expected_result="Stored expected",
                          custom_values={"purpose": "Stored purpose"})
        db.session.add(row)
        db.session.commit()
        return row.id


def test_context_resolves_live_rows_and_discards_client_truth(app_ctx, project_env):
    from app.services.ai import context

    item_id = _row(app_ctx, project_env)
    with app_ctx.app_context():
        payload = context.build_payload(project_env, "procedure", {
            "viewpoints": [{"item_id": item_id, "version": 999,
                            "title": "forged", "ref": "forged"}],
            "signal_dict": [["forged", "private.signal"]],
            "_context": {"project_id": 999},
        })
    assert payload["viewpoints"][0]["title"] == "Stored viewpoint"
    assert payload["viewpoints"][0]["version"] == 4
    assert payload["viewpoints"][0]["ref"] == str(item_id)
    assert payload["signal_dict"] == []
    assert payload["_context"]["items"] == [{"id": item_id, "version": 4}]
    assert payload["_context"]["project_id"] == project_env


@pytest.mark.parametrize("selection", [[], [True], [999999], [1, 1]])
def test_context_rejects_invalid_selection(app_ctx, project_env, selection):
    from app.services.ai import context

    with app_ctx.app_context(), pytest.raises(ValueError):
        context.build_payload(project_env, "procedure", {"item_ids": selection})


def test_context_rejects_other_sheet_and_project(app_ctx, project_env):
    from app.services.ai import context

    item_id = _row(app_ctx, project_env, sheet="lib")
    with app_ctx.app_context(), pytest.raises(ValueError):
        context.build_payload(project_env, "procedure", {"item_ids": [item_id]})


def test_document_context_retains_digest_and_review_boundary(app_ctx, project_env):
    from app.services.ai import context

    with app_ctx.app_context():
        payload = context.build_payload(project_env, "viewpoint", {
            "doc_text": "Design requirement", "source_name": "design.md",
            "source_revision": "v2", "_context": {"approved": True},
        })
    source = payload["_context"]["provenance"][0]
    assert source["kind"] == "submitted_document"
    assert source["name"] == "design.md"
    assert source["revision"] == "v2"
    assert source["sha256"] == hashlib.sha256(b"Design requirement").hexdigest()
    assert "approved" not in payload["_context"]


def test_create_procedure_draft_uses_snapshot(client, app_ctx, project_env,
                                            monkeypatch):
    from app.services.ai.base import GenerationResult
    from app.services.ai import scenarios

    item_id = _row(app_ctx, project_env)
    _configure_ai(app_ctx)
    captured = []

    def generate(scenario, payload, **kwargs):
        captured.append(payload)
        return GenerationResult(output={"procedures": [], "failed_refs": [str(item_id)]})

    monkeypatch.setattr(scenarios, "run_scenario", generate)
    response = client.post("/api/v1/ai/drafts", headers=_login(client, _admin(client)),
                           json={"scenario": "procedure", "project_id": project_env,
                                 "payload": {"item_ids": [item_id]}})
    assert response.status_code == 201
    saved = response.get_json()["data"]["input"]
    assert saved["viewpoints"][0]["version"] == 4
    assert captured[0]["_context"] == saved["_context"]


def test_context_snapshot_schema(app_ctx, project_env):
    from pathlib import Path
    from app.services.ai import context

    item_id = _row(app_ctx, project_env)
    schema = json.loads((Path(__file__).parents[1] / "docs/contracts/ai-context.schema.json").read_text())
    with app_ctx.app_context():
        snapshot = context.build_payload(project_env, "procedure", {"item_ids": [item_id]})
    stored = snapshot["_context"]
    assert set(schema["required"]).issubset(stored)
    assert stored["schema_version"] == schema["properties"]["schema_version"]["const"]
    assert type(stored["project_id"]) is int and stored["project_id"] >= 1
    for entry in stored["items"]:
        assert set(schema["properties"]["items"]["items"]["required"]).issubset(entry)
        assert all(type(entry[key]) is int and entry[key] >= 1 for key in ("id", "version"))
    assert isinstance(stored["provenance"], list)


def test_context_records_project_asset_dependencies(app_ctx, project_env):
    from app.services.ai import context

    item_id = _row(app_ctx, project_env)
    lib_id = _row(app_ctx, project_env, sheet="lib", case_id="L1")
    with app_ctx.app_context():
        payload = context.build_payload(project_env, "procedure", {"item_ids": [item_id]})
    assert {"id": lib_id, "version": 4} in payload["_context"]["dependencies"]


def test_source_context_is_bounded_and_provenanced(app_ctx, project_env):
    from app.services.ai import context

    with app_ctx.app_context():
        payload = context.build_payload(project_env, "viewpoint", {
            "doc_text": "requirement", "source_context": "submitted context",
        })
    assert any(source["kind"] == "submitted_context" for source in payload["_context"]["provenance"])
    with app_ctx.app_context(), pytest.raises(ValueError):
        context.build_payload(project_env, "viewpoint", {
            "doc_text": "requirement", "source_context": "x" * (context.MAX_SOURCE_CHARS + 1),
        })


def test_create_rejects_non_object_json(client):
    headers = _login(client, _admin(client))
    response = client.post("/api/v1/ai/drafts", json=[{"scenario": "viewpoint"}], headers=headers)
    assert response.status_code == 400


def test_library_proposal_snapshots_stored_steps(app_ctx, project_env):
    from app.extensions import db
    from app.models import TestItemRow
    from app.services.ai import context

    item_id = _row(app_ctx, project_env)
    with app_ctx.app_context():
        row = db.session.get(TestItemRow, item_id)
        row.custom_values = {"steps": json.dumps({"steps": [{"no": 1}]})}
        db.session.commit()
        payload = context.build_payload(project_env, "lib", {
            "item_ids": [item_id], "proposal": "Reuse setup",
            "procedures": [{"item_id": item_id, "version": 999, "steps_doc": {"forged": True}}],
        })
    assert payload["procedures"] == [{"item_id": item_id, "version": 4,
                                      "steps_doc": {"steps": [{"no": 1}]}}]


def test_sbs_proposal_reads_saved_base_without_mutation(app_ctx, project_env, tmp_path):
    from app.extensions import db
    from app.models import ProjectModel
    from app.services.ai import context

    sbs_path = tmp_path / "model.sbs"
    sbs_path.write_text("int speed;", encoding="utf-8")
    with app_ctx.app_context():
        model = ProjectModel(project_id=project_env, name="saved", version="v1",
                             kind="bundle", bundle_dir=str(tmp_path))
        db.session.add(model)
        db.session.commit()
        payload = context.build_payload(project_env, "sbs", {
            "model_id": model.id, "current_sbs": "forged base",
            "source_files": {"engine.c": "int speed;"},
        })
        assert payload["_context"]["model"]["id"] == model.id
        assert payload["_context"]["model"]["sbs_sha256"] == hashlib.sha256(b"int speed;").hexdigest()
    assert payload["current_sbs"] == "int speed;"
    assert sbs_path.read_text(encoding="utf-8") == "int speed;"


@pytest.mark.parametrize("fields", [
    {"model_id": True}, {"model_id": 999999}, {"source_files": {"file.c": 1}},
    {"doc_text": 1}, {"doc_text": "x" * 256001},
])
def test_context_rejects_invalid_sources_and_models(app_ctx, project_env, fields):
    from app.services.ai import context

    with app_ctx.app_context(), pytest.raises(ValueError):
        context.build_payload(project_env, "viewpoint", fields)


def test_history_and_input_pool_are_server_owned(app_ctx, project_env):
    from app.extensions import db
    from app.models import TestItemRow
    from app.services.ai import context

    item_id = _row(app_ctx, project_env)
    history_id = _row(app_ctx, project_env, case_id="H1")
    with app_ctx.app_context():
        history = db.session.get(TestItemRow, history_id)
        history.workflow_status = "Ready"
        history.custom_values = {"steps": json.dumps({"input_signals": [["Speed", "speed"]]})}
        db.session.add(TestItemRow(project_id=project_env, sheet="io",
                                  custom_values={"io_name": "Flag", "io_path": "flag"}))
        db.session.add(TestItemRow(project_id=project_env, sheet="const",
                                  custom_values={"const_name": "LIMIT"}))
        db.session.commit()
        payload = context.build_payload(project_env, "procedure", {"item_ids": [item_id]})
    assert payload["sbs_variables"] == [["Flag", "flag"]]
    assert payload["historical_pairs"] == [["Speed", "speed"]]
    assert payload["constant_names"] == ["LIMIT"]


@pytest.mark.parametrize("arguments", [
    ["-Xclang", "-load", "plugin.dll"], ["@private.rsp"],
    ["-I", "C:/private"], "-DDEBUG", [True],
])
def test_http_context_rejects_native_plugin_and_filesystem_compiler_options(app_ctx, project_env, arguments):
    from app.services.ai import context

    with app_ctx.app_context(), pytest.raises(ValueError):
        context.build_payload(project_env, "viewpoint", {
            "doc_text": "Requirement", "compile_args": arguments,
        })


def test_http_context_accepts_bounded_preprocessor_definitions(app_ctx, project_env):
    from app.services.ai import context

    with app_ctx.app_context():
        payload = context.build_payload(project_env, "viewpoint", {
            "doc_text": "Requirement", "compile_args": ["-DFEATURE=1", "-UDEBUG", "-std=c11"],
        })
    assert payload["compile_args"] == ["-DFEATURE=1", "-UDEBUG", "-std=c11"]
