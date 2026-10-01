import pytest


def _task(app_ctx, *, count=2):
    from app.extensions import db
    from app.models import Task

    task = Task(task_key="T900001", project_id=1, test_id="C1", status="queued", run_count=count)
    db.session.add(task)
    db.session.commit()
    return task.id


def test_worker_claim_fences_stale_and_duplicate_delivery(app_ctx):
    from app.extensions import db
    from app.jobqueue import tasks

    with app_ctx.app_context():
        task_id = _task(app_ctx)
        assert tasks._claim_run(db, task_id, 1) is None
        claimed = tasks._claim_run(db, task_id, 2)
        assert claimed.status == "running"
        assert tasks._claim_run(db, task_id, 2) is None


@pytest.mark.parametrize("pooled", [False, True])
def test_untyped_legacy_delivery_cannot_adopt_a_retest(app_ctx, monkeypatch, pooled):
    from unittest.mock import Mock
    from app.extensions import db
    from app.models import Task
    from app.jobqueue import tasks
    from app.runners import test_runner
    from app.services import license_service

    with app_ctx.app_context():
        task_id = _task(app_ctx, count=2)
        backend = Mock()
        pool = Mock()
        pool.acquire.return_value = object()
        monkeypatch.setattr(tasks, "get_pool", lambda *args: pool)
        monkeypatch.setattr(test_runner, "execute", backend)
        monkeypatch.setattr(license_service, "try_acquire", lambda: True)
        monkeypatch.setattr(license_service, "release", lambda: None)
        monkeypatch.setattr(license_service, "mark_busy", lambda: None)
        monkeypatch.setattr(license_service, "mark_idle", lambda: None)
        run = tasks._run_task_pooled if pooled else tasks._run_task_dedicated
        run(app_ctx, app_ctx.config_obj, task_id)
        backend.assert_not_called()
        pool.acquire.assert_not_called()
        assert db.session.get(Task, task_id).status == "queued"


@pytest.mark.parametrize("changed", ["cancelled", "deleted"])
def test_worker_claim_cannot_start_cancelled_or_deleted_attempt(app_ctx, changed):
    import datetime
    from app.extensions import db
    from app.models import Task
    from app.jobqueue import tasks

    with app_ctx.app_context():
        task_id = _task(app_ctx)
        task = db.session.get(Task, task_id)
        if changed == "cancelled":
            task.cancel_requested = True
        else:
            task.deleted_at = datetime.datetime.utcnow()
        db.session.commit()
        assert tasks._claim_run(db, task_id, 2) is None
