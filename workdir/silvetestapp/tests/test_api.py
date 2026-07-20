"""End-to-end API tests (require Flask/SQLAlchemy/Huey installed).

Task creation goes through the primary folder-upload endpoint
(``POST /api/tasks/upload_tree``); the legacy single-``.zip`` endpoints were
removed. A registered ``.sil`` model is a precondition of the folder flow, so
each end-to-end test registers one first (admin-token gated).
"""

from __future__ import annotations

import io
import time

JUDGE = "# coding: UTF-8\ndef run():\n    return True\n"


def _register_model(client, tmp_path, name="PlantA"):
    """Register a real server-side ``.sil`` model (upload_tree precondition)."""
    model_file = tmp_path / f"{name}.sil"
    model_file.write_text("SIL MODEL\n", encoding="utf-8")
    resp = client.post(
        "/api/admin/models",
        json={"name": name, "path": str(model_file)},
        headers={"X-Admin-Token": "secret"},
    )
    assert resp.status_code == 200, resp.get_data(as_text=True)
    return name


def _submit_tree(client, test_id="TC_API", submitter="tester",
                 model="PlantA", folder="Proj"):
    """Submit one test-case folder via the primary folder-upload endpoint.

    The browser prepends the selected folder name to every path; the server
    strips that leading segment, so ``<folder>/<test_id>/judge.py`` stages as
    ``<test_id>/judge.py``.
    """
    rel = f"{folder}/{test_id}/judge.py"
    return client.post(
        "/api/tasks/upload_tree",
        data={
            "files": [(io.BytesIO(JUDGE.encode("utf-8")), rel)],
            "paths": [rel],
            "test_ids": test_id,
            "model": model,
            "submitter": submitter,
            "folder_name": folder,
        },
        content_type="multipart/form-data",
    )


def _wait_final(client, key, timeout=10.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        data = client.get(f"/api/tasks/{key}").get_json()
        if data["status"] in ("passed", "failed", "cancelled"):
            return data
        time.sleep(0.1)
    return client.get(f"/api/tasks/{key}").get_json()


def test_upload_tree_and_run(client, tmp_path):
    _register_model(client, tmp_path)
    resp = _submit_tree(client, "TC_API", submitter="tester")
    assert resp.status_code == 201, resp.get_data(as_text=True)
    created = resp.get_json()["created"]
    assert len(created) == 1 and created[0]["test_id"] == "TC_API"
    key = created[0]["task_id"]

    final = _wait_final(client, key)
    assert final["status"] == "passed"

    detail = client.get(f"/api/tasks/{key}/detail").get_json()
    assert any(e["event_type"] == "result" for e in detail["events"])

    dl = client.get(f"/api/tasks/{key}/download")
    assert dl.status_code == 200


def test_upload_tree_requires_model(client):
    # With no registered model the folder endpoint refuses the submission.
    resp = _submit_tree(client, "TC_NOMODEL", model="")
    assert resp.status_code == 409


def test_resubmit_after_completion_allowed(client, tmp_path):
    _register_model(client, tmp_path)
    first = _submit_tree(client, "TC_DUP", submitter="dup")
    assert first.status_code == 201
    _wait_final(client, first.get_json()["created"][0]["task_id"])
    # A second identical submission after completion is allowed (not active).
    second = _submit_tree(client, "TC_DUP", submitter="dup")
    assert second.status_code == 201
    assert len(second.get_json()["created"]) == 1


def test_admin_verify(client):
    # Wrong / missing token is rejected; correct token unlocks.
    assert client.post("/api/admin/verify", json={}).status_code == 401
    ok = client.post("/api/admin/verify", headers={"X-Admin-Token": "secret"})
    assert ok.status_code == 200
    assert ok.get_json()["ok"] is True


def test_register_and_list_models(tmp_path, client):
    model_file = tmp_path / "plant.sil"
    model_file.write_text("SIL\n", encoding="utf-8")

    # Registration requires the admin token.
    assert client.post("/api/admin/models",
                       json={"name": "PlantA", "path": str(model_file)}).status_code == 401

    resp = client.post("/api/admin/models",
                       json={"name": "PlantA", "path": str(model_file)},
                       headers={"X-Admin-Token": "secret"})
    assert resp.status_code == 200, resp.get_data(as_text=True)

    # Public model list exposes the registered name and existence flag.
    listed = client.get("/api/models").get_json()["models"]
    assert any(m["name"] == "PlantA" and m["exists"] for m in listed)

    # A non-.sil path is rejected.
    bad = client.post("/api/admin/models",
                      json={"name": "Bad", "path": "/tmp/not_a_model.txt"},
                      headers={"X-Admin-Token": "secret"})
    assert bad.status_code == 400


def test_cancel_batch_missing(client):
    resp = client.post("/api/tasks/cancel_batch", json={"keys": ["T999999"]})
    assert resp.status_code == 200
    assert resp.get_json()["results"][0]["result"] == "not_found"


def test_download_batch_empty(client):
    # No such tasks -> 404 (nothing to bundle).
    resp = client.get("/api/tasks/download_batch?keys=T999999,T999998")
    assert resp.status_code == 404


def test_admin_license_requires_token(client):
    assert client.post("/api/admin/license", json={"count": 8}).status_code == 401
    ok = client.post(
        "/api/admin/license",
        json={"count": 8},
        headers={"X-Admin-Token": "secret"},
    )
    assert ok.status_code == 200
    assert client.get("/api/licenses").get_json()["total"] == 8


def test_cancel_missing_task(client):
    assert client.post("/api/tasks/T999999/cancel").status_code == 404
