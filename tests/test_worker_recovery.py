from __future__ import annotations

from pathlib import Path

from app.services import run_evidence_service as evidence
from test_run_recovery import recovery_context


def test_worker_recovery_dispatches_asynchronously_and_deduplicates(app_ctx, tmp_path, monkeypatch):
    from app.extensions import db
    from app.models import Task
    from app.jobqueue import tasks
    from test_run_evidence import prepared

    with app_ctx.app_context():
        prototype, source, model = prepared(tmp_path)
        task = Task(**{key: value for key, value in vars(prototype).items() if key != "id"})
        db.session.add(task)
        db.session.commit()
        evidence.pin_attempt(task, source)
        db.session.commit()
        monkeypatch.setattr(tasks, "_get_app", lambda: app_ctx)
        tasks.huey.flush()
        tasks.recover_run_attempts()
        assert len(tasks.huey.pending()) == 1
        pending = tasks.huey.pending()[0]
        assert pending.args == (task.id, 1)
        assert task.status == "queued"
        assert not (evidence.attempt_dir(task) / "execution.json").exists()
        tasks.recover_run_attempts()
        assert len(tasks.huey.pending()) == 1
        task.run_count = 2
        task.sil_relpath = str(model)
        evidence.pin_attempt(task, source)
        db.session.commit()
        tasks.recover_run_attempts()
        assert {entry.args for entry in tasks.huey.pending()} == {(task.id, 1), (task.id, 2)}
        assert tasks._claim_run(db, task.id, 1) is None
        assert tasks._claim_run(db, task.id, 2) is not None
        tasks.huey.flush()


def test_worker_startup_calls_attempt_recovery_without_owning_bootstrap():
    source = (Path(__file__).parents[1] / "run_worker.py").read_text(encoding="utf-8")
    assert "tasks.recover_run_attempts(startup=True)" in source
    assert "bootstrap_app(" not in source


def test_run_recovery_is_registered_as_periodic():
    from app.jobqueue import tasks
    assert hasattr(tasks, "recover_run_attempts_job")
    assert any(isinstance(task, tasks.recover_run_attempts_job.task_class)
               for task in tasks.huey._registry.periodic_tasks)


def test_startup_recovery_visits_more_than_one_bounded_page(recovery_context):
    from app.extensions import db
    from app.models import Setting, Task
    from app.jobqueue import tasks
    from test_run_recovery import make_attempt

    attempts = [make_attempt(recovery_context, number, status="running") for number in range(1, 102)]
    db.session.add(Setting(key="run_recovery_cursor_running", value=str(attempts[49].id)))
    db.session.commit()
    tasks.huey.flush()
    result = tasks.recover_run_attempts(startup=True)
    assert len(result["recovered"]) == 101
    assert Task.query.filter_by(status="running").count() == 0
    assert tasks.huey.pending() == []
