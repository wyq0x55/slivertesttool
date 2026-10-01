from __future__ import annotations

import json
from datetime import datetime
from unittest.mock import Mock

import pytest


@pytest.fixture
def recovery_context(app_ctx, tmp_path, monkeypatch):
    from app.extensions import db
    from app.jobqueue import tasks as jobs
    from app.runners import silver_pool, test_runner

    backend = Mock(side_effect=AssertionError("Recovery must not invoke a backend"))
    monkeypatch.setattr(test_runner, "build_runner", backend)
    monkeypatch.setattr(test_runner, "execute", backend)
    monkeypatch.setattr(silver_pool, "build_driver", backend)
    monkeypatch.setattr(jobs, "get_pool", backend)
    monkeypatch.setattr(jobs, "run_task", backend)
    with app_ctx.app_context():
        assert db.engine.dialect.name == "postgresql"
        yield tmp_path, backend
        backend.assert_not_called()


def make_attempt(recovery_context, number, *, status="queued", kind="generated", count=1):
    from app.extensions import db
    from app.models import Task
    from app.services import run_evidence_service as evidence

    root, _backend = recovery_context
    workspace = root / "workspace"
    source = workspace / ".pending" / str(number) / "CASE"
    source.mkdir(parents=True)
    model = root / "plant.sil"
    model.write_text("approved model", encoding="utf-8")
    if kind == "legacy":
        (source / "judge.py").write_text("raise RuntimeError('must never execute')", encoding="utf-8")
    else:
        (source / "constants.json").write_text('{"constants": {}}', encoding="utf-8")
        (source / "lib.json").write_text('{"subroutines": {}}', encoding="utf-8")
        steps = [] if kind == "invalid" else [{"no": 1}]
        (source / "testcase_CASE.json").write_text(json.dumps({"steps": steps}), encoding="utf-8")
    task = Task(task_key=f"R{number:06}", test_id="CASE", status=status,
                workspace=str(workspace), sil_relpath=str(model), run_count=count)
    db.session.add(task)
    db.session.flush()
    if kind != "unpinned":
        evidence.pin_attempt(task, source, approved_inputs={"review": "human"})
    db.session.commit()
    return task


def queued(enqueue, *, is_pending=lambda *identity: False, limit=100):
    from app.services.run_evidence_service import recover_queued_attempts

    return recover_queued_attempts(enqueue, is_pending=is_pending, limit=limit)


def interrupted(finalise, *, startup=False, limit=100):
    from app.services import run_recovery_service

    return run_recovery_service.recover_interrupted_attempts(finalise, startup=startup, limit=limit)


def seal(task, *, status="passed", verdict="PASS", message="trusted completion"):
    from app.extensions import db
    from app.services import run_evidence_service as evidence

    evidence.seal_attempt(task, status=status, verdict=verdict, message=message)
    db.session.commit()
    return evidence.attempt_dir(task)


@pytest.mark.parametrize("predecessor", ["pending", "invalid", "unpinned"])
def test_queued_cursor_survives_restart_and_reaches_task_101(recovery_context, predecessor):
    from app.extensions import db

    for number in range(1, 101):
        make_attempt(recovery_context, number, kind="generated" if predecessor == "pending" else predecessor)
    last = make_attempt(recovery_context, 101)
    identity = (last.id, last.run_count)
    pending_checks = Mock(side_effect=lambda task_id, count: task_id != identity[0])
    enqueue = Mock()
    first = queued(enqueue, is_pending=pending_checks)
    assert first["recovered"] == []
    assert len(first["skipped"]) + len(first["errors"]) == 100
    assert pending_checks.call_count <= 100
    db.session.remove()
    second = queued(enqueue, is_pending=pending_checks)
    assert identity in second["recovered"]
    assert len(second["recovered"]) + len(second["skipped"]) + len(second["errors"]) <= 100
    enqueue.assert_called_once_with(*identity)


def test_queued_cursor_wraps_and_revisits_previously_pending_attempt(recovery_context):
    first = make_attempt(recovery_context, 1)
    second = make_attempt(recovery_context, 2)
    identities = [(first.id, 1), (second.id, 1)]
    pending = {identities[0]}
    enqueue = Mock()
    assert queued(enqueue, is_pending=lambda *identity: identity in pending, limit=1)["skipped"] == [identities[0]]
    assert queued(enqueue, is_pending=lambda *identity: identity in pending, limit=1)["recovered"] == [identities[1]]
    pending.clear()
    assert queued(enqueue, limit=1)["recovered"] == [identities[0]]


def test_queued_enqueue_failure_is_retried_after_wrap(recovery_context):
    task = make_attempt(recovery_context, 1)
    identity = (task.id, 1)
    first = queued(Mock(side_effect=RuntimeError("queue unavailable")))
    assert first["errors"] == [{"task_id": identity[0], "run_count": 1, "error": "queue unavailable"}]
    assert queued(Mock())["recovered"] == [identity]
    assert task.status == "queued"


@pytest.mark.parametrize("status", ["running", "passed", "failed", "cancelled"])
def test_queued_never_replays_nonqueued_attempt(recovery_context, status):
    make_attempt(recovery_context, 1, status=status)
    enqueue = Mock()
    assert queued(enqueue)["recovered"] == []
    enqueue.assert_not_called()


@pytest.mark.parametrize("change", ["cancelled", "deleted", "new_attempt", "sealed"])
def test_queued_never_replays_ineligible_attempt(recovery_context, change):
    from app.extensions import db

    task = make_attempt(recovery_context, 1)
    if change == "cancelled":
        task.cancel_requested = True
    elif change == "deleted":
        task.deleted_at = datetime.now()
    elif change == "new_attempt":
        task.run_count = 2
    else:
        seal(task)
    db.session.commit()
    enqueue = Mock()
    assert queued(enqueue)["recovered"] == []
    enqueue.assert_not_called()


def test_queued_legacy_judge_does_not_require_generated_documents(recovery_context):
    task = make_attempt(recovery_context, 1, kind="legacy", count=2)
    enqueue = Mock()
    assert queued(enqueue)["recovered"] == [(task.id, 2)]
    enqueue.assert_called_once_with(task.id, 2)


def test_startup_crash_after_claim_requires_manual_retry_without_execution(recovery_context):
    from app.extensions import db
    from app.jobqueue.tasks import _claim_run
    from app.models import Task

    task = make_attempt(recovery_context, 1)
    identity = (task.id, 1)
    assert _claim_run(db, *identity).status == "running"
    db.session.remove()
    finalise = Mock(return_value=True)
    assert interrupted(finalise, startup=True)["recovered"] == [identity]
    arguments, options = finalise.call_args
    assert arguments[:4] == (*identity, "failed", "ERROR")
    assert "manual" in arguments[4].lower() and "retry" in arguments[4].lower()
    assert options == {"trusted_outcome": False}
    assert db.session.get(Task, identity[0]).status == "running"


def test_periodic_recovery_preserves_live_unsealed_running_attempt(recovery_context):
    task = make_attempt(recovery_context, 1, status="running")
    finalise = Mock(return_value=True)
    result = interrupted(finalise)
    assert result["recovered"] == []
    assert result["skipped"] == [(task.id, 1)]
    assert task.status == "running"
    finalise.assert_not_called()


@pytest.mark.parametrize("startup", [False, True])
@pytest.mark.parametrize("status,verdict", [("passed", "PASS"), ("failed", "FAIL"), ("failed", "ERROR"),
                                            ("failed", "UNKNOWN"), ("cancelled", "CANCELLED")])
def test_trusted_sealed_crash_reconciles_via_callback(recovery_context, startup, status, verdict):
    from app.extensions import db
    from app.models import Task

    task = make_attempt(recovery_context, 1, status="running", count=2)
    identity = (task.id, 2)
    seal(task, status=status, verdict=verdict)
    db.session.remove()
    finalise = Mock(return_value=True)
    assert interrupted(finalise, startup=startup)["recovered"] == [identity]
    finalise.assert_called_once_with(*identity, status, verdict, "trusted completion")
    assert db.session.get(Task, identity[0]).status == "running"


@pytest.mark.parametrize("mutation", ["outcome", "results", "manifest", "unbound"])
@pytest.mark.parametrize("startup", [False, True])
def test_untrusted_outcome_never_promotes_success(recovery_context, mutation, startup):
    from app.extensions import db
    from app.models import RunEvidence

    task = make_attempt(recovery_context, 1, status="running")
    root = seal(task)
    if mutation == "outcome":
        outcome = root / "outcome.json"
        data = json.loads(outcome.read_text(encoding="utf-8"))
        data["message"] = "forged"
        outcome.write_text(json.dumps(data), encoding="utf-8")
    elif mutation == "results":
        (root / "results" / "forged.log").write_text("forged", encoding="utf-8")
    elif mutation == "manifest":
        (root / "manifest.json").write_text("{}", encoding="utf-8")
    else:
        db.session.get(RunEvidence, (task.id, 1)).outcome_sha256 = ""
        db.session.commit()
    finalise = Mock(return_value=True)
    result = interrupted(finalise, startup=startup)
    assert result["errors"]
    if startup and mutation == "unbound":
        arguments, options = finalise.call_args
        assert arguments[:4] == (task.id, 1, "failed", "ERROR")
        assert options == {"trusted_outcome": False}
    else:
        assert result["recovered"] == []
        finalise.assert_not_called()
    assert task.status == "running"


@pytest.mark.parametrize("status", ["queued", "passed", "failed", "cancelled"])
def test_interrupted_never_finalises_nonrunning_attempt(recovery_context, status):
    task = make_attempt(recovery_context, 1, status=status)
    seal(task)
    finalise = Mock(return_value=True)
    assert interrupted(finalise, startup=True)["recovered"] == []
    finalise.assert_not_called()


@pytest.mark.parametrize("change", ["deleted", "new_attempt", "unpinned"])
def test_interrupted_never_finalises_ineligible_attempt(recovery_context, change):
    from app.extensions import db

    task = make_attempt(recovery_context, 1, status="running", kind="unpinned" if change == "unpinned" else "generated")
    if change == "deleted":
        task.deleted_at = datetime.now()
    elif change == "new_attempt":
        task.run_count = 2
    db.session.commit()
    finalise = Mock(return_value=True)
    assert interrupted(finalise, startup=True)["recovered"] == []
    finalise.assert_not_called()


@pytest.mark.parametrize("startup", [False, True])
def test_interrupted_bounded_cursor_progresses_after_skipped_or_failed_callbacks(recovery_context, startup):
    from app.extensions import db

    for number in range(1, 101):
        task = make_attempt(recovery_context, number, status="running")
        if startup:
            seal(task)
    last = make_attempt(recovery_context, 101, status="running")
    seal(last)
    identity = (last.id, 1)
    finalise = Mock(side_effect=lambda task_id, *arguments, **options: task_id == identity[0])
    first = interrupted(finalise, startup=startup)
    assert first["recovered"] == []
    assert len(first["skipped"]) == 100
    assert finalise.call_count <= 100
    db.session.remove()
    assert identity in interrupted(finalise, startup=startup)["recovered"]


def test_callback_failure_keeps_sealed_outcome_for_retry(recovery_context):
    from app.services.run_evidence_service import read_evidence

    task = make_attempt(recovery_context, 1, status="running")
    seal(task)
    before = read_evidence(task)
    finalise = Mock(side_effect=RuntimeError("transaction aborted"))
    result = interrupted(finalise)
    assert result["errors"] == [{"task_id": task.id, "run_count": 1, "error": "transaction aborted"}]
    assert read_evidence(task) == before
    finalise = Mock(return_value=True)
    assert interrupted(finalise)["recovered"] == [(task.id, 1)]


@pytest.mark.parametrize("limit", [True, False, 0, -1, 1001, 1.5, "100", None])
def test_recovery_rejects_invalid_budgets_before_callbacks(recovery_context, limit):
    callback = Mock()
    with pytest.raises(ValueError, match="limit"):
        queued(callback, limit=limit)
    with pytest.raises(ValueError, match="limit"):
        interrupted(callback, limit=limit)
    callback.assert_not_called()


def test_bound_but_missing_outcome_never_becomes_unsealed_success(recovery_context):
    task = make_attempt(recovery_context, 1, status="running")
    root = seal(task)
    (root / "outcome.json").unlink()
    finalise = Mock(return_value=True)
    assert interrupted(finalise, startup=True)["errors"]
    finalise.assert_not_called()


def test_startup_requires_explicit_boolean(recovery_context):
    finalise = Mock()
    with pytest.raises(ValueError, match="startup"):
        interrupted(finalise, startup="False")
    finalise.assert_not_called()


def test_queued_bound_but_missing_outcome_is_never_dispatched(recovery_context):
    task = make_attempt(recovery_context, 1)
    root = seal(task)
    (root / "outcome.json").unlink()
    enqueue = Mock()
    assert queued(enqueue)["recovered"] == []
    enqueue.assert_not_called()


def test_scan_cursor_survives_process_death_during_publication(recovery_context):
    from app.extensions import db

    first = make_attempt(recovery_context, 1)
    second = make_attempt(recovery_context, 2)
    identities = [(first.id, 1), (second.id, 1)]
    with pytest.raises(SystemExit):
        queued(Mock(side_effect=SystemExit("process death")), limit=1)
    db.session.remove()
    assert queued(Mock(), limit=1)["recovered"] == [identities[1]]
    assert queued(Mock(), limit=1)["recovered"] == [identities[0]]


def test_locked_predecessor_is_skipped_and_revisited_without_starvation(recovery_context):
    from sqlalchemy import select
    from sqlalchemy.orm import Session
    from app.extensions import db
    from app.models import Task

    first = make_attempt(recovery_context, 1)
    second = make_attempt(recovery_context, 2)
    identities = [(first.id, 1), (second.id, 1)]
    with Session(db.engine) as locker, locker.begin():
        locker.execute(select(Task).where(Task.id == identities[0][0]).with_for_update()).scalar_one()
        assert queued(Mock(), limit=1)["skipped"] == [identities[0]]
        assert queued(Mock(), limit=1)["recovered"] == [identities[1]]
    assert queued(Mock(), limit=1)["recovered"] == [identities[0]]


@pytest.mark.parametrize("mutation", ["cancelled", "deleted", "new_attempt"])
def test_candidates_are_rechecked_after_page_selection(recovery_context, mutation):
    from app.extensions import db
    from app.models import Task

    first = make_attempt(recovery_context, 1)
    second = make_attempt(recovery_context, 2)
    identities = [(first.id, 1), (second.id, 1)]

    def enqueue(task_id, run_count):
        current = db.session.get(Task, identities[1][0])
        if mutation == "cancelled":
            current.cancel_requested = True
        elif mutation == "deleted":
            current.deleted_at = datetime.now()
        else:
            current.run_count = 2
        db.session.commit()

    result = queued(enqueue)
    assert result["recovered"] == [identities[0]]
    assert result["skipped"] == [identities[1]]


def test_finaliser_owns_commit_and_terminal_attempt_is_not_reconciled_twice(recovery_context):
    from app.extensions import db
    from app.models import Task

    task = make_attempt(recovery_context, 1, status="running")
    seal(task)
    identity = (task.id, 1)

    def commit_finalisation(task_id, run_count, status, verdict, message, **options):
        current = db.session.get(Task, task_id)
        assert current.status == "running"
        current.status, current.result, current.message = status, verdict, message
        db.session.commit()
        return True

    finalise = Mock(side_effect=commit_finalisation)
    assert interrupted(finalise)["recovered"] == [identity]
    assert interrupted(finalise)["recovered"] == []
    finalise.assert_called_once_with(*identity, "passed", "PASS", "trusted completion")
    assert db.session.get(Task, identity[0]).status == "passed"


@pytest.mark.parametrize("value", ["broken", "-1", "999999999"])
def test_cursor_recovers_from_invalid_value_or_removed_tail(recovery_context, value):
    from app.extensions import db
    from app.models import Setting

    task = make_attempt(recovery_context, 1)
    identity = (task.id, 1)
    assert queued(Mock())["recovered"] == [identity]
    cursor = Setting.query.filter(Setting.key.like("run_recovery_cursor_%")).one()
    cursor.value = value
    db.session.commit()
    assert queued(Mock(), limit=1)["recovered"] == [identity]


@pytest.mark.parametrize("status,verdict,message", [
    ("running", "PASS", "nonterminal"),
    ("passed", "FAIL", "mismatch"),
    ("failed", "PASS", "mismatch"),
    ("cancelled", "FAIL", "mismatch"),
    ("passed", None, "invalid verdict"),
    ("passed", "PASS", None),
])
def test_invalid_but_hash_bound_outcome_is_not_finalised(recovery_context, status, verdict, message):
    task = make_attempt(recovery_context, 1, status="running")
    seal(task, status=status, verdict=verdict, message=message)
    finalise = Mock(return_value=True)
    assert interrupted(finalise, startup=True)["errors"]
    finalise.assert_not_called()
