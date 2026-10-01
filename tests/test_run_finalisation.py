from __future__ import annotations

from sqlalchemy.orm import Session

import pytest

from test_run_evidence import submitted


def matching_row(task):
    from app.extensions import db
    from app.models import LMUser, Project, TestItemRow
    from app.services.lanmatrix import fields, fields_service
    admin = LMUser.query.filter_by(is_system_admin=True).first()
    project = Project(id=task.project_id, code="FINALISE", name="Finalise", owner_id=admin.id)
    db.session.add(project)
    db.session.commit()
    fields_service.ensure_fields(admin, project, fields.TEST_FIELDS)
    row = TestItemRow(project_id=task.project_id, case_id=task.test_id, title="Approved test")
    db.session.add(row)
    task.status = "running"
    db.session.commit()
    return row


def test_finalisation_commits_task_and_history_together(submitted, monkeypatch):
    from app.extensions import db
    from app.models import Task, TaskStatus, TestRunRecord
    from app.runners import test_runner
    from app.services.lanmatrix import run_writeback_service
    _app, task, _source, _model = submitted
    row = matching_row(task)
    original = run_writeback_service.record_run
    observed = []

    def record(current, verdict, **kwargs):
        with Session(db.engine) as observer:
            observed.append(observer.get(Task, task.id).status)
        assert observed == ["running"]
        return original(current, verdict, **kwargs)

    monkeypatch.setattr(run_writeback_service, "record_run", record)
    test_runner._finalise(task, TaskStatus.PASSED, "Passed", "PASS")
    history = TestRunRecord.query.filter_by(task_key=task.task_key).one()
    assert history.run_count == 1 and history.model_version == "v1"
    assert row.result == "PASS" and task.status == "passed"


def test_writeback_failure_keeps_durable_outcome_for_idempotent_recovery(submitted, monkeypatch):
    from app.extensions import db
    from app.models import Task, TaskStatus, TestRunRecord
    from app.runners import test_runner
    from app.services import run_evidence_service as evidence
    from app.services.lanmatrix import run_writeback_service
    _app, task, _source, _model = submitted
    row = matching_row(task)
    original = run_writeback_service.record_run

    def broken(*args, **kwargs):
        raise RuntimeError("writeback interrupted")

    monkeypatch.setattr(run_writeback_service, "record_run", broken)
    with pytest.raises(RuntimeError, match="writeback interrupted"):
        test_runner._finalise(task, TaskStatus.PASSED, "Passed", "PASS")
    db.session.rollback()
    with Session(db.engine) as observer:
        assert observer.get(Task, task.id).status == "running"
        assert observer.query(TestRunRecord).count() == 0
    assert evidence.read_evidence(task)["outcome"]["verdict"] == "PASS"
    monkeypatch.setattr(run_writeback_service, "record_run", original)
    from app.services.run_finalisation_service import finalise_attempt
    assert finalise_attempt(task.id, 1, "passed", "PASS", "Passed")
    assert not finalise_attempt(task.id, 1, "passed", "PASS", "Passed")
    assert row.result == "PASS"
    assert TestRunRecord.query.count() == 1


def test_old_finalisation_cannot_mutate_a_retest_or_forge_outcome(submitted):
    from app.extensions import db
    from app.models import TaskStatus, TestRunRecord
    from app.runners import test_runner
    _app, task, _source, _model = submitted
    matching_row(task)
    test_runner._finalise(task, TaskStatus.FAILED, "Failure", "FAIL")
    task.run_count = 2
    task.status = "queued"
    db.session.commit()
    from app.services.run_finalisation_service import finalise_attempt
    assert not finalise_attempt(task.id, 1, "passed", "PASS", "forged")
    assert task.run_count == 2 and task.status == "queued"
    assert TestRunRecord.query.one().verdict == "FAIL"
