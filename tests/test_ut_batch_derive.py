"""A derived batch regenerates only confirmed functions and never edits its source.

The source batch is a delivered record: derivation reads its witness records and
writes only the new batch, whose config is frozen from whatever is current then.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.extensions import db
from app.models import UtBatch, UtJob, UtPipelineConfig
from test_ut_pipeline import _configure, _enqueue, env as workflow_env  # noqa: F401
from test_ut_witness_handoff import _record, _report


def _confirm(workflow_env, function):
    with workflow_env.client.application.app_context():
        job = UtJob.query.join(UtJob.target).filter_by(function=function).one()
        job.witness = {
            "contract": "silver.ut-witness/1",
            "handoff": _record(),
            "confirm": {"status": "CONFIRMED", "confirmed": 2, "write": True},
        }
        db.session.commit()
        return job.id


def _batch(workflow_env, batch_id):
    status, body = workflow_env.call("get", "/batches")
    assert status == 200
    return next(item for item in body["data"] if item["id"] == batch_id)


def test_derive_queues_only_confirmed_functions_and_keeps_the_source(workflow_env, app_ctx):
    targets = _configure(workflow_env)
    workflow_env.bridge.report = _report(("f", _record()), ("g", _record()))
    job_ids = _enqueue(workflow_env, targets["f"], targets["g"])
    source_id = _batch_id(workflow_env, job_ids[0])
    _confirm(workflow_env, "f")
    with workflow_env.client.application.app_context():
        before = db.session.get(UtBatch, source_id).to_dict()

    status, body = workflow_env.call("post", f"/batches/{source_id}/derive", json={})
    assert status == 202, body
    derived = body["data"]
    assert derived["derived_from_batch_id"] == source_id
    assert derived["id"] != source_id

    _, jobs = workflow_env.call("get", "/jobs", query_string={"batch_id": derived["id"]})
    functions = sorted(job["target"]["function"] for job in jobs["data"])
    assert functions == ["f"]

    with workflow_env.client.application.app_context():
        after = db.session.get(UtBatch, source_id).to_dict()
    assert after == before


def test_derive_refuses_a_batch_with_nothing_confirmed(workflow_env, app_ctx):
    targets = _configure(workflow_env)
    workflow_env.bridge.report = _report(("f", _record()))
    job_ids = _enqueue(workflow_env, targets["f"])
    source_id = _batch_id(workflow_env, job_ids[0])

    status, body = workflow_env.call("post", f"/batches/{source_id}/derive", json={})
    assert status == 400 and body["error"]["code"] == "VALIDATION_ERROR"
    assert "没有可重生成的目标" in body["error"]["message"]


def test_a_new_ceiling_reaches_only_the_derived_batch(workflow_env, app_ctx):
    targets = _configure(workflow_env)
    workflow_env.bridge.report = _report(("f", _record()))
    job_ids = _enqueue(workflow_env, targets["f"])
    source_id = _batch_id(workflow_env, job_ids[0])
    _confirm(workflow_env, "f")

    status, body = workflow_env.call("post", f"/batches/{source_id}/derive",
                                     json={"function_step_limit": 1_000_000})
    assert status == 202, body
    with workflow_env.client.application.app_context():
        derived = db.session.get(UtBatch, body["data"]["id"])
        source = db.session.get(UtBatch, source_id)
        assert source.config_snapshot.get("function_step_limit") is None
        assert derived.config_snapshot["function_step_limit"] == 1_000_000
        assert UtPipelineConfig.query.filter_by(
            project_id=workflow_env.pid).one().function_step_limit == 1_000_000


def test_explicit_targets_override_the_confirmed_set(workflow_env, app_ctx):
    targets = _configure(workflow_env)
    workflow_env.bridge.report = _report(("f", _record()), ("g", _record()))
    job_ids = _enqueue(workflow_env, targets["f"], targets["g"])
    source_id = _batch_id(workflow_env, job_ids[0])
    _confirm(workflow_env, "f")

    status, body = workflow_env.call("post", f"/batches/{source_id}/derive",
                                     json={"targets": [targets["g"]], "label": "只重生成 g"})
    assert status == 202, body
    assert body["data"]["label"] == "只重生成 g"
    _, jobs = workflow_env.call("get", "/jobs", query_string={"batch_id": body["data"]["id"]})
    assert [job["target"]["function"] for job in jobs["data"]] == ["g"]


def test_derive_rejects_a_bad_target_list(workflow_env, app_ctx):
    targets = _configure(workflow_env)
    workflow_env.bridge.report = _report(("f", _record()))
    job_ids = _enqueue(workflow_env, targets["f"])
    source_id = _batch_id(workflow_env, job_ids[0])

    status, body = workflow_env.call("post", f"/batches/{source_id}/derive", json={"targets": ["f"]})
    assert status == 400 and body["error"]["code"] == "VALIDATION_ERROR"


def test_an_unknown_source_batch_is_not_found(workflow_env, app_ctx):
    status, body = workflow_env.call("post", "/batches/999/derive", json={})
    assert status == 404 and body["error"]["code"] == "NOT_FOUND"


def _batch_id(workflow_env, job_id):
    _, body = workflow_env.call("get", f"/jobs/{job_id}")
    return body["data"]["batch"]["id"]
