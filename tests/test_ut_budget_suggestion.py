"""The budget suggestion is advice derived from delivered batches, never applied.

P3 reads finished batches back: each one froze its declared ceiling, and each of
its jobs froze the steps it consumed, the obligation count and the exhaustion
flag.  These tests pin the two things that make the number trustworthy - only
non-exhausted functions reach the median, and the same records answer the same
thing - plus the boundary that keeps it advice: nothing is written back.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.extensions import db
from app.models import LMUser, UtBatch, UtJob, UtTarget
from app.services.lanmatrix import projects_service
from app.services.ut_pipeline import budget, handoff
from test_ut_pipeline import _configure, env as workflow_env  # noqa: F401

# ut-agent's own ceiling, used when a batch declares none.
UT_AGENT_DEFAULT = 250_000_000


def _handoff(obligations: int, *, exhausted: bool = False) -> dict:
    """A frozen witness handoff shaped like ut-agent's P1 record."""
    unresolved = obligations if exhausted else 0
    return {
        "state": handoff.HANDOFF_READY,
        "budget": {"budget_exhausted": exhausted, "total": obligations,
                   "completed": obligations - unresolved, "remaining": unresolved,
                   "completed_is_solved": False},
        "obligations": {"total": obligations, "unresolved": unresolved,
                        "by_cause": {"BUDGET_EXHAUSTED": unresolved} if exhausted else {}},
        "admissible": {"count": 0, "obligation_ids": [], "refused": [], "unclassified": 0},
        "mcdc": {"total": 0, "pending_review": 0, "pending_pair_ids": []},
        "ledger_confirmed": False,
        "error": (f"GENERATION_BUDGET_EXHAUSTED: {obligations} obligations"
                  if exhausted else None),
    }


def _target(project_id: int, function: str) -> UtTarget:
    target = UtTarget.query.filter_by(project_id=project_id, function=function).one_or_none()
    if target is None:
        target = UtTarget(project_id=project_id, row_no=1, source_name="a.c",
                          function=function, source_rel=f"src/{function}.c",
                          unit_rel=f"WinAMS/src/{function}.c/{function}")
        db.session.add(target)
        db.session.flush()
    return target


def _batch(rows, *, project_id: int, declared: int | None = None,
           status: str = UtBatch.STATUS_DONE) -> int:
    """One batch whose jobs carry exactly what the sampler reads.

    ``rows`` is ``(function, steps_used, obligations, exhausted)``.
    """
    batch = UtBatch(project_id=project_id, label="calibration", status=status,
                    config_snapshot={"function_step_limit": declared})
    db.session.add(batch)
    db.session.flush()
    for function, steps, obligations, exhausted in rows:
        db.session.add(UtJob(
            project_id=project_id, batch_id=batch.id,
            target_id=_target(project_id, function).id,
            state=UtJob.AWAITING_REVIEW, generation_status="VALIDATED",
            generation={"function": function, "status": "VALIDATED",
                        "semantic_steps": steps,
                        "step_limit": declared or UT_AGENT_DEFAULT},
            witness={"contract": handoff.CONTRACT,
                     "handoff": _handoff(obligations, exhausted=exhausted)}))
    db.session.commit()
    return batch.id


def _estimate(workflow_env, **kwargs):
    with workflow_env.client.application.app_context():
        return budget.estimate(project_id=workflow_env.pid, **kwargs)


def _app(workflow_env):
    return workflow_env.client.application


def test_only_unexhausted_samples_calibrate_the_coefficient(workflow_env):
    _configure(workflow_env)
    with _app(workflow_env).app_context():
        _batch([("f", 10, 1, False), ("g", 10, 1, False)], project_id=workflow_env.pid)
        # The exhausted function spent its whole ceiling to reach part of a huge
        # obligation set; folded in, its 1000 steps/obligation would move the
        # median from 10 to 55.
        _batch([("f", 100, 1, False), ("g", UT_AGENT_DEFAULT, 250_000, True)],
               project_id=workflow_env.pid)
    document = _estimate(workflow_env, target_obligations=20)

    assert document["state"] == budget.OK
    assert document["samples"]["used_count"] == 3
    assert document["samples"]["exhausted_count"] == 1
    assert document["coefficient"]["steps_per_obligation"] == 10.0
    assert document["suggested_step_limit"] == 300  # ceil(10 x 20 x 1.5)
    assert document["exhausted"]["rate"] == 0.25
    assert document["exhausted"]["stop_positions"] == [
        "GENERATION_BUDGET_EXHAUSTED: 250000 obligations"]


def test_a_clean_sample_gives_the_target_times_ratio_times_safety(workflow_env):
    _configure(workflow_env)
    with _app(workflow_env).app_context():
        _batch([("f", 20, 10, False), ("g", 40, 10, False)], project_id=workflow_env.pid)
        _batch([("f", 60, 10, False)], project_id=workflow_env.pid)
    document = _estimate(workflow_env, target_obligations=10)

    assert document["coefficient"]["steps_per_obligation"] == 4.0
    assert document["coefficient"]["safety_factor"] == budget.DEFAULT_SAFETY_FACTOR
    assert document["suggested_step_limit"] == 60  # ceil(4 x 10 x 1.5)


def test_fewer_than_two_batches_or_three_functions_has_no_number(workflow_env):
    _configure(workflow_env)
    with _app(workflow_env).app_context():
        _batch([("f", 20, 10, False), ("g", 40, 10, False)], project_id=workflow_env.pid)
    document = _estimate(workflow_env)

    assert document["state"] == budget.INSUFFICIENT_SAMPLES
    assert document["suggested_step_limit"] is None
    assert document["coefficient"]["steps_per_obligation"] is None
    assert document["missing"] == [
        {"what": "batches", "have": 1, "need": 2},
        {"what": "functions", "have": 2, "need": 3},
    ]


def test_a_batch_that_still_moves_is_not_a_sample(workflow_env):
    _configure(workflow_env)
    with _app(workflow_env).app_context():
        _batch([("f", 20, 10, False), ("g", 40, 10, False)],
               project_id=workflow_env.pid, status=UtBatch.STATUS_RUNNING)
        finished = _batch([("f", 60, 10, False)], project_id=workflow_env.pid)
    document = _estimate(workflow_env)

    assert document["state"] == budget.INSUFFICIENT_SAMPLES
    assert document["samples"]["used_count"] == 1
    assert document["samples"]["batches"] == [finished]


def test_the_same_records_answer_the_same_thing(workflow_env):
    _configure(workflow_env)
    with _app(workflow_env).app_context():
        _batch([("f", 20, 10, False), ("g", 40, 10, False)], project_id=workflow_env.pid)
        _batch([("f", 60, 10, False)], project_id=workflow_env.pid)
    first = _estimate(workflow_env)
    second = _estimate(workflow_env)
    status, body = workflow_env.call("get", "/budget/suggestion")

    assert status == 200, body
    assert first["samples"]["digest"] == second["samples"]["digest"]
    assert first["samples"]["digest"] == body["data"]["samples"]["digest"]
    assert first["suggested_step_limit"] == body["data"]["suggested_step_limit"]


def test_samples_never_cross_projects(workflow_env):
    _configure(workflow_env)
    with _app(workflow_env).app_context():
        _batch([("f", 20, 10, False), ("g", 40, 10, False)], project_id=workflow_env.pid)
        _batch([("f", 60, 10, False)], project_id=workflow_env.pid)
        admin = LMUser.query.filter_by(is_system_admin=True).one()
        other = projects_service.create_project(admin, code="UTC", name="other",
                                                project_type="unit_test")
        other_id = other.id
        _batch([("h", 9000, 1, False)], project_id=other_id)
        db.session.commit()
    ours = _estimate(workflow_env)
    with _app(workflow_env).app_context():
        theirs = budget.estimate(project_id=other_id)

    assert theirs["samples"]["used_count"] == 1
    assert theirs["state"] == budget.INSUFFICIENT_SAMPLES
    assert theirs["samples"]["digest"] != ours["samples"]["digest"]
    assert theirs["coefficient"]["steps_per_obligation"] is None


def test_the_suggestion_never_falls_below_one(workflow_env):
    _configure(workflow_env)
    with _app(workflow_env).app_context():
        _batch([("f", 7, 10, False), ("g", 7, 10, False)], project_id=workflow_env.pid)
        _batch([("f", 7, 10, False)], project_id=workflow_env.pid)
    document = _estimate(workflow_env, target_obligations=1, safety_factor=0.01)

    assert document["coefficient"]["steps_per_obligation"] == 0.7
    assert document["suggested_step_limit"] == 1


def test_a_delivered_batch_keeps_what_it_froze(workflow_env):
    _configure(workflow_env)
    with _app(workflow_env).app_context():
        _batch([("f", 20, 10, False), ("g", 40, 10, False)],
               project_id=workflow_env.pid, declared=1_000)
        _batch([("f", 60, 10, False)], project_id=workflow_env.pid, declared=2_000)
        before = [(batch.to_dict(), batch.config_snapshot)
                  for batch in UtBatch.query.filter_by(project_id=workflow_env.pid).all()]
        jobs_before = [job.witness for job in UtJob.query.filter_by(project_id=workflow_env.pid)]
    status, body = workflow_env.call("get", "/budget/suggestion")
    assert status == 200, body
    assert body["data"]["samples"]["used"][0]["ceiling"] == 1_000

    with _app(workflow_env).app_context():
        after = [(batch.to_dict(), batch.config_snapshot)
                 for batch in UtBatch.query.filter_by(project_id=workflow_env.pid).all()]
        jobs_after = [job.witness for job in UtJob.query.filter_by(project_id=workflow_env.pid)]
    assert after == before
    assert jobs_after == jobs_before


def test_the_endpoint_rejects_an_unusable_target(workflow_env):
    _configure(workflow_env)
    status, body = workflow_env.call("get", "/budget/suggestion",
                                     query_string={"target_obligations": "0"})
    assert status == 400, body
    status, body = workflow_env.call("get", "/budget/suggestion",
                                     query_string={"safety_factor": "abc"})
    assert status == 400, body