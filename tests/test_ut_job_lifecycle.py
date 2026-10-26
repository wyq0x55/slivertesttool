"""Unit-test job lifecycle: stopping, resuming and superseded jobs.

Reuses the scripted bridge of test_ut_pipeline. Huey runs in immediate mode,
so a stop that has to land while a worker is inside an external call is
requested from inside the fake bridge call itself.
"""

from __future__ import annotations

from test_ut_pipeline import (  # noqa: F401
    GAP,
    _configure,
    _enqueue,
    _job,
    _llm_reply,
    _ut_reviews,
    env,
)

from app.services.ai import provider
from app.services.ut_pipeline import pipeline


def _queued_batch(env, app_ctx, *target_ids):
    """Create a batch without dispatching its worker, so its jobs stay queued."""
    with app_ctx.app_context():
        from app.models import LMUser

        admin = LMUser.query.filter_by(is_system_admin=True).first()
        batch = pipeline.create_batch(env.pid, list(target_ids), admin.id)
        return batch.id


def test_a_queued_job_stops_at_once_and_its_batch_never_generates(env, app_ctx):
    targets = _configure(env)
    batch_id = _queued_batch(env, app_ctx, targets["f"])
    _, jobs = env.call("get", "/jobs", query_string={"batch_id": batch_id})
    [job_id] = [job["id"] for job in jobs["data"]]

    status, body = env.call("post", f"/jobs/{job_id}/stop")
    assert status == 202 and body["data"]["state"] == "stopped"
    assert body["data"]["stop_requested"] is False

    env.bridge.generate_error = "generation must not run for a fully stopped batch"
    with app_ctx.app_context():
        assert pipeline.run_generation(batch_id) == []
    _, batches = env.call("get", "/batches")
    assert batches["data"][0]["status"] == "done"
    assert batches["data"][0]["states"] == {"stopped": 1}

    # Nothing was generated, so there is no candidate to resume from.
    status, body = env.call("post", f"/jobs/{job_id}/retry")
    assert status == 400 and "重新加入队列" in body["error"]["message"]
    # The function is free to be queued again.
    env.bridge.generate_error = None
    [again] = _enqueue(env, targets["f"])
    assert _job(env, again)["state"] == "awaiting_review"


def test_a_running_job_finishes_its_call_then_stops_before_the_model(env, monkeypatch):
    targets = _configure(env)
    env.bridge.gaps = [GAP]
    calls = []
    monkeypatch.setattr(provider, "chat", lambda *a, **k: calls.append(1) or
                        _llm_reply(declined=("b1:T",)))
    execute = env.bridge.execute

    def execute_then_stop(**kwargs):
        evidence = execute(**kwargs)
        if env.bridge.executions == 1:
            # The request a user makes while LLVM is running.
            from app.models import LMUser, UtJob
            job = UtJob.query.order_by(UtJob.id.desc()).first()
            admin = LMUser.query.filter_by(is_system_admin=True).first()
            stopped = pipeline.stop_job(env.pid, job.id, admin.id)
            assert stopped.state == "executing" and stopped.to_dict()["stop_requested"]
        return evidence

    env.bridge.execute = execute_then_stop
    [job_id] = _enqueue(env, targets["f"])
    job = _job(env, job_id)
    # The LLVM verdict is retained; the missing-case repair never started.
    assert job["state"] == "stopped" and calls == []
    assert [(a["origin"], a["verdict"]) for a in job["attempts"]] == [("GENERATOR", "PASS")]
    assert job["last_verdict"] == "PASS" and "已按请求停止" in job["reason"]

    status, body = env.call("post", f"/jobs/{job_id}/stop")
    assert status == 400 and body["error"]["code"] == "INVALID_STATE"

    # Resuming continues where the job stopped: the repair round runs now.
    status, body = env.call("post", f"/jobs/{job_id}/retry")
    assert status == 202
    job = _job(env, job_id)
    assert job["state"] == "awaiting_review" and calls == [1]
    assert job["stop_requested"] is False


def test_stopping_a_batch_stops_every_active_job(env, app_ctx):
    targets = _configure(env)
    batch_id = _queued_batch(env, app_ctx, targets["f"], targets["g"])

    status, body = env.call("post", f"/batches/{batch_id}/stop")
    assert status == 202 and body["data"] == {"requested": 2, "stopped": 2, "stopping": 0}
    _, jobs = env.call("get", "/jobs", query_string={"batch_id": batch_id})
    assert {job["state"] for job in jobs["data"]} == {"stopped"}

    status, body = env.call("post", f"/batches/{batch_id}/stop")
    assert status == 400 and body["error"]["code"] == "INVALID_STATE"
    status, body = env.call("post", "/batches/999999/stop")
    assert status == 404

    with app_ctx.app_context():
        from app.models import AuditLog

        stops = AuditLog.query.filter_by(action="ut.job.stop").all()
        assert len(stops) == 2 and all(row.new_value["batch_wide"] for row in stops)


def test_queueing_a_function_again_supersedes_its_older_job(env):
    targets = _configure(env)
    [old] = _enqueue(env, targets["f"])
    assert _ut_reviews(env)[0][0]["id"] == old

    [new] = _enqueue(env, targets["f"])
    assert _job(env, old)["superseded_by"] == new
    assert _job(env, new)["superseded_by"] is None

    # Only the latest job is work: the older one leaves every pending queue.
    rows, counts = _ut_reviews(env)
    assert [row["id"] for row in rows] == [new]
    rows, _ = _ut_reviews(env, role="requester")
    assert [row["id"] for row in rows] == [new]

    # ...and is read-only, so nobody signs off the replaced candidate.
    for path, payload in (("review", {"decision": "approve", "note": "x"}),
                          ("revision", {"csv": "mod\r\n"}), ("retry", None)):
        kwargs = {"json": payload} if payload is not None else {}
        status, body = env.call("post", f"/jobs/{old}/{path}", **kwargs)
        assert status == 400 and body["error"]["code"] == "SUPERSEDED", path

    status, body = env.call("post", f"/jobs/{new}/review", json={"decision": "approve"})
    assert status == 200
    # A decided job no longer carries the reason it was waiting for.
    assert body["data"]["state"] == "approved" and body["data"]["reason"] is None
