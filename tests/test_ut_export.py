"""Archive of approved unit-test CSVs with their provenance."""

from __future__ import annotations

import csv
import hashlib
import io
import json
import zipfile
from pathlib import Path

from test_ut_pipeline import GOOD, _configure, _enqueue, env  # noqa: F401


def _approve(env, job_id, note=""):
    status, body = env.call("post", f"/jobs/{job_id}/review", json={"decision": "approve", "note": note})
    assert status == 200, body


def _export(env, **query):
    resp = env.client.get(env.base + "/approved/export", query_string=query, headers=env.headers)
    return resp


def test_approved_csvs_ship_with_a_manifest(env):
    targets = _configure(env)
    [first] = _enqueue(env, targets["f"])
    _approve(env, first, "looks right")
    [second] = _enqueue(env, targets["f"])   # re-queued, approved again: the newer one ships
    _approve(env, second)
    [pending] = _enqueue(env, targets["g"])  # never approved: not exported

    resp = _export(env)
    assert resp.status_code == 200 and resp.mimetype == "application/zip"
    assert "attachment" in resp.headers["Content-Disposition"]
    archive = zipfile.ZipFile(io.BytesIO(resp.data))
    assert sorted(archive.namelist()) == ["manifest.csv", "manifest.json", "src/a.c/f.csv"]
    assert archive.read("src/a.c/f.csv") == GOOD

    manifest = json.loads(archive.read("manifest.json"))
    [entry] = manifest["files"]
    assert entry["job_id"] == second and entry["origin"] == "GENERATOR"
    assert entry["csv_sha256"] == hashlib.sha256(GOOD).hexdigest()
    assert entry["machine_verdict"] == "PASS" and entry["test_rules_ref"] == "universal-test-rules@1"
    assert entry["approved_by"] and entry["approved_at"]
    rows = list(csv.DictReader(io.StringIO(archive.read("manifest.csv").decode("utf-8-sig"))))
    assert [row["function"] for row in rows] == ["f"]
    assert pending not in [entry["job_id"] for entry in manifest["files"]]


def test_nothing_approved_and_batch_filter(env):
    targets = _configure(env)
    [job_id] = _enqueue(env, targets["f"])
    resp = _export(env)
    assert resp.status_code == 404 and resp.get_json()["error"]["code"] == "NOT_FOUND"
    _approve(env, job_id)
    _, batches = env.call("get", "/batches")
    batch_id = batches["data"][0]["id"]
    assert _export(env, batch_id=batch_id).status_code == 200
    assert _export(env, batch_id=batch_id + 999).status_code == 404


def test_a_csv_changed_after_review_aborts_the_export(env, app_ctx):
    from app.models import UtAttempt, UtJob

    targets = _configure(env)
    [job_id] = _enqueue(env, targets["f"])
    _approve(env, job_id)
    with app_ctx.app_context():
        from app.extensions import db
        path = Path(db.session.get(UtAttempt, db.session.get(UtJob, job_id).current_attempt_id).csv_path)
    path.write_bytes(GOOD.replace(b",0,1", b",0,9"))
    resp = _export(env)
    assert resp.status_code == 400
    error = resp.get_json()["error"]
    assert error["code"] == "INTEGRITY_ERROR" and "审核后被改动" in error["message"]


def test_a_proven_presented_form_is_archived_and_a_changed_one_aborts(env, app_ctx, tmp_path):
    from app.models import UtAttempt, UtJob

    presented = GOOD.replace(b"#COMMENT,x,ret\r\n", b"#COMMENT,x,ret\r\n;$L$,TRUE\r\n")
    sandbox = tmp_path / "exec"
    sandbox.mkdir()
    shown = sandbox / "presented-candidate.csv"
    shown.write_bytes(presented)
    targets = _configure(env)
    [job_id] = _enqueue(env, targets["f"])
    _approve(env, job_id)
    with app_ctx.app_context():
        from app.extensions import db
        attempt = db.session.get(UtAttempt, db.session.get(UtJob, job_id).current_attempt_id)
        attempt.execution = {"status": "HOST_PASS", "sandbox": str(sandbox), "presented_candidate": {
            "contract": "ut-agent.host-presented-candidate/1", "path": str(shown),
            "sha256": hashlib.sha256(presented).hexdigest(),
            "source_candidate_sha256": hashlib.sha256(GOOD).hexdigest()}}
        db.session.commit()

    archive = zipfile.ZipFile(io.BytesIO(_export(env).data))
    assert archive.read("src/a.c/f.csv") == presented
    [entry] = json.loads(archive.read("manifest.json"))["files"]
    assert entry["csv_sha256"] == hashlib.sha256(GOOD).hexdigest()
    assert entry["file_sha256"] == hashlib.sha256(presented).hexdigest()

    shown.write_bytes(presented.replace(b",0,1", b",0,9"))
    resp = _export(env)
    assert resp.status_code == 400 and "展示版" in resp.get_json()["error"]["message"]
