"""Persisted timing across PostgreSQL workflow calls and human actions."""

from copy import deepcopy

from test_ut_pipeline import GAP, _configure, _enqueue, _job
from test_ut_pipeline import env as workflow_env

from app.services.ai import provider
from app.services.ut_pipeline import pipeline, timing

env = workflow_env


def test_completed_calls_survive_metadata_handoff_and_read_refresh(env, monkeypatch, app_ctx):
    from sqlalchemy.orm import Session

    from app.extensions import db
    from app.models import UtJob

    targets = _configure(env)
    execute = env.bridge.execute
    seen = []

    def observed(*args, **kwargs):
        # An independent reader sees the start while the external call runs.
        with Session(db.engine) as reader:
            item = reader.query(UtJob).filter_by(state="executing").one()
            rows = item.generation[timing.KEY]["steps"]
            assert rows[-1]["status"] == "running"
            seen.append(rows[-1]["step"])
        return execute(*args, **kwargs)

    monkeypatch.setattr(env.bridge, "execute", observed)
    [ident] = _enqueue(env, targets["f"])
    first = _job(env, ident)["timing"]
    assert seen == ["host_execute"]
    assert first["history_complete"]
    work = [row for row in first["steps"] if row["kind"] == "work"]
    assert {row["step"] for row in work} == {"host_execute"}
    assert all(row["status"] == "completed" and row["duration_ms"] >= 0 for row in work)
    assert all(row["attempt_id"] and row["attempt_seq"] == 1 and row["candidate_sha256"] for row in work)
    with app_ctx.app_context():
        item = db.session.get(UtJob, ident)
        before = deepcopy(item.generation)
        pipeline.job_detail(env.pid, ident)
        db.session.expire_all()
        assert db.session.get(UtJob, ident).generation == before
    second = _job(env, ident)["timing"]
    assert [(r["id"], r["duration_ms"]) for r in first["steps"]] == [
        (r["id"], r["duration_ms"]) for r in second["steps"]]


def test_failed_model_request_is_timed_again_on_retry_without_consuming_round(env, monkeypatch):
    targets = _configure(env)
    env.bridge.gaps = [GAP]

    def offline(*args, **kwargs):
        raise provider.ProviderError("offline")

    monkeypatch.setattr(provider, "chat", offline)
    [ident] = _enqueue(env, targets["f"])
    first = _job(env, ident)
    assert first["gate_code"] == "MODEL_UNAVAILABLE" and first["repair_rounds"] == 0
    assert env.call("post", f"/jobs/{ident}/retry")[0] == 202
    latest = _job(env, ident)
    calls = [row for row in latest["timing"]["steps"] if row["step"] == "input_model"]
    assert len(calls) == 2 and calls[0]["id"] != calls[1]["id"]
    assert all(row["status"] == "failed" and row["round"] == 1 and row["duration_ms"] >= 0 for row in calls)
    assert latest["repair_rounds"] == 0


def test_review_and_supersede_close_human_wait_in_history(env):
    targets = _configure(env)
    [reviewed] = _enqueue(env, targets["f"])
    assert env.call("post", f"/jobs/{reviewed}/review", json={"decision": "approve"})[0] == 200
    rows = _job(env, reviewed)["timing"]["steps"]
    assert rows[-1]["step"] == "human_wait" and rows[-1]["status"] == "completed"
    [older] = _enqueue(env, targets["g"])
    _enqueue(env, targets["g"])
    rows = _job(env, older)["timing"]["steps"]
    assert rows[-1]["outcome"] == "superseded" and rows[-1]["status"] == "completed"


def test_recovery_marks_unfinished_calls_unknown_instead_of_counting_downtime(env, app_ctx):
    from app.extensions import db
    from app.models import LMUser, UtJob

    targets = _configure(env)
    with app_ctx.app_context():
        uid = LMUser.query.filter_by(is_system_admin=True).first().id
        batch = pipeline.create_batch(env.pid, [targets["f"]], uid)
        item = UtJob.query.filter_by(batch_id=batch.id).one()
        pipeline._set_state(item, UtJob.GENERATING)
        db.session.commit()
        ident = item.id
        pipeline.recover_interrupted(set(), set())
    data = _job(env, ident)
    assert data["state"] == "error" and not data["timing"]["history_complete"]
    rows = data["timing"]["steps"]
    abandoned = next(row for row in rows if row["step"] == "generation")
    assert abandoned["status"] == "interrupted"
    assert abandoned["ended_at"] is None and abandoned["duration_ms"] is None
    assert rows[-1]["step"] == "human_wait" and rows[-1]["status"] == "running"


def test_pending_duplicate_cannot_keep_a_dead_call_running(env, app_ctx):
    from app.extensions import db
    from app.models import UtJob

    targets = _configure(env)
    [ident] = _enqueue(env, targets["f"])
    with app_ctx.app_context():
        item = db.session.get(UtJob, ident)
        pipeline._set_state(item, UtJob.EXECUTING)
        # This durable start is what a process killed inside an engine leaves.
        span = timing.stage(item, "host_execute")
        span.__enter__()
        call_id = item.generation[timing.KEY]["steps"][-1]["id"]
        recovered = pipeline.recover_interrupted({ident}, set())
        assert recovered["pending"] == [ident]
        rows = item.generation[timing.KEY]["steps"]
        call = next(row for row in rows if row["id"] == call_id)
        assert call["status"] == "interrupted" and call["duration_ms"] is None
        span.__exit__(None, None, None)
        pipeline.advance_job(ident)
    data = _job(env, ident)
    assert data["state"] == "awaiting_review"
    assert not any(row["status"] == "running" and row["kind"] != "wait" for row in data["timing"]["steps"])


def test_delayed_generation_duplicate_marks_crashed_interval_unknown(env, app_ctx):
    from app.extensions import db
    from app.models import LMUser, UtJob

    targets = _configure(env)
    with app_ctx.app_context():
        uid = LMUser.query.filter_by(is_system_admin=True).first().id
        batch = pipeline.create_batch(env.pid, [targets["f"]], uid)
        item = UtJob.query.filter_by(batch_id=batch.id).one()
        pipeline._set_state(item, UtJob.GENERATING)
        db.session.commit()
        ident = item.id
        pipeline.run_generation(batch.id)
    data = _job(env, ident)
    row = next(row for row in data["timing"]["steps"] if row["step"] == "generation")
    assert row["status"] == "interrupted" and row["duration_ms"] is None
