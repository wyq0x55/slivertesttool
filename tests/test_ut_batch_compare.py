"""The batch comparison reads two frozen records and writes neither of them."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.extensions import db
from app.models import UtJob
from test_ut_pipeline import _configure, _enqueue, env as workflow_env  # noqa: F401
from test_ut_batch_derive import _batch_id, _confirm
from test_ut_witness_handoff import _record, _report


def _derived(workflow_env, source_id, limit=None):
    body = {"function_step_limit": limit} if limit is not None else {}
    status, response = workflow_env.call("post", f"/batches/{source_id}/derive", json=body)
    assert status == 202, response
    return response["data"]["id"]


def test_compare_lines_up_a_function_across_both_batches(workflow_env, app_ctx):
    targets = _configure(workflow_env)
    workflow_env.bridge.report = _report(("f", _record()), ("g", _record()))
    job_ids = _enqueue(workflow_env, targets["f"], targets["g"])
    source_id = _batch_id(workflow_env, job_ids[0])
    _confirm(workflow_env, "f")
    derived_id = _derived(workflow_env, source_id, limit=500_000)

    status, body = workflow_env.call("get", f"/batches/{derived_id}/compare",
                            query_string={"against": source_id})
    assert status == 200, body
    data = body["data"]
    assert data["derived_from_batch_id"] == source_id
    assert data["base"]["function_step_limit"] is None
    assert data["derived"]["function_step_limit"] == 500_000

    rows = {row["function"]: row for row in data["functions"]}
    assert set(rows) == {"f", "g"}
    assert rows["f"]["base"]["handoff_state"] == "HANDOFF_READY"
    assert rows["f"]["base"]["obligations_total"] == 12
    assert rows["f"]["base"]["obligations_completed"] == 210
    assert rows["f"]["base"]["replay"]["confirm_status"] == "CONFIRMED"
    assert rows["f"]["derived"]["step_limit"] == 500_000
    # g was never confirmed, so the derived batch did not regenerate it.
    assert rows["g"]["derived"] is None


def test_a_missing_figure_stays_null_instead_of_becoming_zero(workflow_env, app_ctx):
    targets = _configure(workflow_env)
    workflow_env.bridge.report = _report(("f", _record()))
    job_ids = _enqueue(workflow_env, targets["f"])
    source_id = _batch_id(workflow_env, job_ids[0])
    with workflow_env.client.application.app_context():
        job = UtJob.query.join(UtJob.target).filter_by(function="f").one()
        job.generation = {**(job.generation or {}), "intent_summary": {"cases": 4}}
        job.witness = {"contract": "silver.ut-witness/1", "handoff": _record(),
                       "confirm": {"status": "CONFIRMED", "confirmed": 1}}
        db.session.commit()
    derived_id = _derived(workflow_env, source_id)

    status, body = workflow_env.call("get", f"/batches/{derived_id}/compare",
                            query_string={"against": source_id})
    assert status == 200, body
    side = next(row for row in body["data"]["functions"] if row["function"] == "f")["base"]
    assert side["resolved"] is None
    assert side["stopped_at"] is None


def test_compare_needs_a_batch_to_compare_against(workflow_env, app_ctx):
    status, body = workflow_env.call("get", "/batches/1/compare")
    assert status == 400 and body["error"]["code"] == "VALIDATION_ERROR"
    assert "against" in body["error"]["message"]


def test_compare_refuses_a_batch_from_another_project(workflow_env, app_ctx):
    targets = _configure(workflow_env)
    workflow_env.bridge.report = _report(("f", _record()))
    job_ids = _enqueue(workflow_env, targets["f"])
    source_id = _batch_id(workflow_env, job_ids[0])

    status, body = workflow_env.call("get", f"/batches/{source_id}/compare",
                            query_string={"against": 999})
    assert status == 404 and body["error"]["code"] == "NOT_FOUND"
