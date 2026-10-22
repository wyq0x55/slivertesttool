"""Manifest import/form persistence, validation ownership and immutable revisions."""

import copy
import hashlib
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_ut_pipeline import _as, _configure, _enqueue, _member
from test_ut_pipeline import env as _shared_env

from app.services.ut_pipeline import pipeline
from app.services.ut_pipeline.binding import read_manifest
from app.services.ut_pipeline.bridge import BridgeError, BridgeSettings, UtAgentBridge

env = _shared_env

DOCUMENT = {"project": {"id": "manifest-ui-fixture"}, "baseline": {"id": "psd-rebuild", "version": "1.1"},
            "build": {"profile": "rh850-ghs", "exclude_directories": []},
            "winams": {"profile": "standard"}, "profile": {"mcdc_enabled": True}}


def test_read_validation_and_exposed_document_use_the_same_snapshot(tmp_path):
    source = tmp_path / "mutable.json"
    original = json.dumps(DOCUMENT).encode("utf-8")
    source.write_bytes(original)

    def validate(path):
        assert Path(path) != source
        assert Path(path).read_bytes() == original
        source.write_text('{"unrelated_private_data": "must not be exposed"}', encoding="utf-8")
        return {"test_policy_sha256": "validated-original"}

    result = read_manifest(str(source), bridge=SimpleNamespace(constraints=validate))
    assert result["manifest_document"] == DOCUMENT
    assert result["manifest_sha256"] == hashlib.sha256(original).hexdigest()
    assert result["test_policy_sha256"] == "validated-original"


def test_read_refuses_values_the_editor_would_round(tmp_path):
    source = tmp_path / "large-number.json"
    source.write_text(json.dumps({**DOCUMENT, "targets": {"value": 9007199254740993}}), encoding="utf-8")
    with pytest.raises(ValueError, match="精确保留"):
        read_manifest(str(source), bridge=SimpleNamespace(constraints=lambda path: {}))


def _save(case, document):
    status, body = case.call("put", "/config", json={"manifest_document": document})
    assert status == 200, body
    return body["data"]


def test_form_document_roundtrips_all_fields_and_has_content_addressed_storage(env, tmp_path):
    document = copy.deepcopy(DOCUMENT)
    document["targets"] = {"approved_custom_binding": {"enabled": True}}
    document["build"]["custom_option"] = {"nested": [1, 2]}
    document["winams"]["stub_memory_limit_bytes"] = 32768
    saved = _save(env, document)
    path = Path(saved["manifest_path"])
    assert path.parent.name == "manifests" and path.parent.parent.name == f"p{env.pid}"
    assert path.name == hashlib.sha256(path.read_bytes()).hexdigest() + ".json"
    assert saved["manifest_document"] == document
    assert saved["manifest_error"] is None
    _, loaded = env.call("get", "/config")
    assert loaded["data"]["manifest_document"] == document
    assert loaded["data"]["manifest_sha256"] == saved["manifest_sha256"]
    assert _save(env, document)["manifest_path"] == str(path)


def test_new_revision_keeps_queued_batch_manifest_bytes(env, app_ctx):
    targets = _configure(env)
    first = _save(env, DOCUMENT)
    first_path = Path(first["manifest_path"])
    first_bytes = first_path.read_bytes()
    [job_id] = _enqueue(env, targets["f"])
    updated = copy.deepcopy(DOCUMENT)
    updated["project"]["id"] = "second-revision"
    second = _save(env, updated)
    assert second["manifest_path"] != first["manifest_path"]
    assert first_path.read_bytes() == first_bytes
    with app_ctx.app_context():
        from app.extensions import db
        from app.models import UtBatch, UtJob

        job = db.session.get(UtJob, job_id)
        batch = db.session.get(UtBatch, job.batch_id)
        assert batch.config_snapshot["manifest_path"] == first["manifest_path"]


def test_validate_does_not_save_binding_or_publish_a_revision(env, app_ctx):
    status, body = env.call("post", "/manifest/validate", json={"manifest_document": DOCUMENT})
    assert status == 200 and len(body["data"]["sha256"]) == 64, body
    assert env.call("get", "/config")[1]["data"] is None
    root = Path(app_ctx.config["WORKSPACE_DIR"]) / "ut" / f"p{env.pid}" / "manifests"
    assert not list(root.iterdir())


def test_external_validation_failure_preserves_existing_binding(env, app_ctx, monkeypatch):
    saved = _save(env, DOCUMENT)

    def rejected(manifest):
        assert json.loads(Path(manifest).read_text(encoding="utf-8"))["baseline"]["version"] == "missing"
        raise BridgeError("locked baseline version is unavailable")

    monkeypatch.setattr(env.bridge, "constraints", rejected)
    invalid = copy.deepcopy(DOCUMENT)
    invalid["baseline"]["version"] = "missing"
    status, body = env.call("put", "/config", json={"manifest_document": invalid})
    assert status == 400 and body["error"]["code"] == "UT_MANIFEST_INVALID", body
    with app_ctx.app_context():
        assert pipeline.get_config(env.pid).manifest_path == saved["manifest_path"]


@pytest.mark.parametrize("document", [None, [], "{}", {"large": "x" * (256 * 1024)}])
def test_invalid_payload_never_calls_the_bridge_or_changes_config(env, monkeypatch, document):
    monkeypatch.setattr(env.bridge, "constraints", lambda path: pytest.fail("Invalid payload reached bridge"))
    status, body = env.call("put", "/config", json={"manifest_document": document})
    assert status == 400 and body["error"]["code"] == "VALIDATION_ERROR", body
    assert env.call("get", "/config")[1]["data"] is None


def test_content_and_server_path_cannot_be_submitted_together(env):
    status, body = env.call("put", "/config", json={"manifest_document": DOCUMENT, "manifest_path": "x.json"})
    assert status == 400 and body["error"]["code"] == "VALIDATION_ERROR", body


def test_existing_revision_is_never_overwritten_if_its_bytes_changed(env):
    saved = _save(env, DOCUMENT)
    path = Path(saved["manifest_path"])
    path.write_bytes(b"externally changed content")
    status, body = env.call("put", "/config", json={"manifest_document": DOCUMENT})
    assert status == 400 and body["error"]["code"] == "UT_MANIFEST_INTEGRITY", body
    assert path.read_bytes() == b"externally changed content"


def test_reader_cannot_validate_or_save_a_manifest(env, app_ctx):
    saved = _save(env, DOCUMENT)
    _as(env, _member(app_ctx, env.pid, "reader"))
    assert env.call("get", "/config")[1]["data"]["manifest_document"] == saved["manifest_document"]
    assert env.call("put", "/config", json={"manifest_document": DOCUMENT})[0] == 403
    assert env.call("post", "/manifest/validate", json={"manifest_document": DOCUMENT})[0] == 403


def test_manifest_validation_requires_csrf(env):
    response = env.client.post(env.base + "/manifest/validate", json={"manifest_document": DOCUMENT})
    assert response.status_code == 403


@pytest.fixture()
def real_bridge(monkeypatch):
    root = os.environ.get("UT_HOST_TEST_AGENT_ROOT")
    python = os.environ.get("UT_HOST_TEST_PYTHON")
    if not root or not python:
        pytest.skip("External ut-agent checkout and interpreter are required")
    bridge = UtAgentBridge(BridgeSettings(root, python, "", "", 120, 120, 120))
    monkeypatch.setattr(pipeline, "get_bridge", lambda: bridge)
    return bridge


def test_real_bridge_accepts_versioned_manifest_and_rejects_unknown_schema_fields(env, real_bridge):
    saved = _save(env, DOCUMENT)
    invalid = copy.deepcopy(DOCUMENT)
    invalid["not_a_manifest_property"] = "invalid"
    status, body = env.call("post", "/manifest/validate", json={"manifest_document": invalid})
    assert status == 400 and "not_a_manifest_property" in body["error"]["message"], body
    assert env.call("get", "/config")[1]["data"]["manifest_path"] == saved["manifest_path"]


def test_legacy_nonmanifest_json_is_not_exposed_as_a_generic_file_reader(env, real_bridge, tmp_path):
    path = tmp_path / "unrelated.json"
    path.write_text('{"secret":"must-not-be-returned"}', encoding="utf-8")
    status, body = env.call("put", "/config", json={"manifest_path": str(path)})
    assert status == 200, body
    assert body["data"]["manifest_document"] is None
    assert body["data"]["manifest_error"]
    assert "must-not-be-returned" not in json.dumps(body)


def test_validated_policy_fields_follow_the_bridge_response(env, app_ctx):
    policy = {"version": "1", "boundary_adjacent": False, "c1": True, "mcdc": True,
              "arithmetic": False, "combination_group": True}
    env.bridge.constraints = lambda manifest: {
        "baseline": {"id": "universal-test-rules", "version": "1"},
        "effective_test_policy": policy, "test_policy_sha256": "c" * 64,
        "test_rules_ref": "universal-test-rules@1", "test_rules": {"version": "1"},
    }
    saved = _save(env, {"project": {"id": "policy-project"}, "test_policy": policy})
    assert saved["effective_test_policy"] == policy
    assert saved["test_policy_sha256"] == "c" * 64
    assert saved["test_rules_ref"] == "universal-test-rules@1"
    loaded = env.call("get", "/config")[1]["data"]
    assert loaded["effective_test_policy"] == policy
    assert loaded["test_rules"] == {"version": "1"}
    targets = _configure(env)
    [job_id] = _enqueue(env, targets["f"])
    with app_ctx.app_context():
        from app.extensions import db
        from app.models import UtBatch, UtJob

        batch = db.session.get(UtBatch, db.session.get(UtJob, job_id).batch_id)
        assert batch.constraints["test_policy_sha256"] == "c" * 64
        assert batch.constraints["test_rules"] == {"version": "1"}
