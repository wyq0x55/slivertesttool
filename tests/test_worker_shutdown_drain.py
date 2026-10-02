from unittest.mock import Mock

import pytest


@pytest.fixture
def queued_run(app_ctx, monkeypatch):
    from app.extensions import db
    from app.jobqueue import tasks
    from app.models import Task
    from app.runners import test_runner

    with app_ctx.app_context():
        task = Task(task_key="DRAIN0001", status="queued", run_count=1)
        db.session.add(task)
        db.session.commit()
        task_id = task.id
    instance = object()
    pool = Mock()
    pool.acquire.return_value = instance
    backend = Mock()
    monkeypatch.setattr(tasks, "get_pool", lambda *_args: pool)
    monkeypatch.setattr(test_runner, "execute", backend)
    return task_id, pool, instance, backend


def test_get_pool_cannot_restore_target_while_draining(app_ctx, monkeypatch):
    from app.jobqueue import tasks
    from app.runners import silver_pool
    from app.services import license_service

    pool = Mock()
    monkeypatch.setattr(silver_pool, "get_pool", lambda *_args: pool)
    with app_ctx.app_context():
        license_service.begin_drain()
        assert tasks.get_pool(app_ctx, app_ctx.config_obj) is pool
        pool.set_target.assert_called_once_with(0)
        assert license_service.get_limit() == 2


def test_pooled_submission_cannot_execute_during_drain(app_ctx, queued_run):
    from app.extensions import db
    from app.jobqueue import tasks
    from app.models import Task
    from app.services import license_service

    task_id, pool, instance, backend = queued_run
    with app_ctx.app_context():
        license_service.begin_drain()
        tasks._run_task_pooled(app_ctx, app_ctx.config_obj, task_id, 1)
        backend.assert_not_called()
        assert db.session.get(Task, task_id).status == "queued"
        assert license_service.get_limit() == 2
        assert license_service.get_in_use() == 0
        if pool.acquire.called:
            pool.release.assert_called_once_with(instance)


def test_drain_between_pool_borrow_and_claim_leaves_attempt_queued(app_ctx, queued_run):
    from app.extensions import db
    from app.jobqueue import tasks
    from app.models import Task
    from app.services import license_service

    task_id, pool, instance, backend = queued_run

    def begin_drain(*_args, **_kwargs):
        license_service.begin_drain()
        return instance

    pool.acquire.side_effect = begin_drain
    with app_ctx.app_context():
        tasks._run_task_pooled(app_ctx, app_ctx.config_obj, task_id, 1)
        backend.assert_not_called()
        pool.release.assert_called_once_with(instance)
        assert db.session.get(Task, task_id).status == "queued"
        assert license_service.get_in_use() == 0


def test_pooled_waiter_stops_when_drain_begins(app_ctx, queued_run):
    from app.jobqueue import tasks
    from app.services import license_service

    task_id, pool, _instance, backend = queued_run

    def waiting_acquire(*_args, should_cancel, **_kwargs):
        license_service.begin_drain()
        assert should_cancel() is True
        return None

    pool.acquire.side_effect = waiting_acquire
    with app_ctx.app_context():
        tasks._run_task_pooled(app_ctx, app_ctx.config_obj, task_id, 1)
        backend.assert_not_called()
        assert license_service.get_in_use() == 0


def test_pooled_borrow_does_not_steal_an_existing_license_count(app_ctx, queued_run):
    from app.jobqueue import tasks
    from app.services import license_service

    task_id, pool, instance, backend = queued_run
    with app_ctx.app_context():
        license_service.set_limit(1)
        assert license_service.try_acquire()
        tasks._run_task_pooled(app_ctx, app_ctx.config_obj, task_id, 1)
        backend.assert_not_called()
        pool.release.assert_called_once_with(instance)
        assert license_service.get_in_use() == 1
        license_service.release()


def test_dedicated_waiter_stops_without_claiming_during_drain(app_ctx, queued_run, monkeypatch):
    from app.extensions import db
    from app.jobqueue import tasks
    from app.models import Task
    from app.services import license_service

    task_id, _pool, _instance, backend = queued_run
    sleep = Mock(side_effect=AssertionError("A draining worker must not keep waiting"))
    monkeypatch.setattr(tasks.time, "sleep", sleep)
    with app_ctx.app_context():
        license_service.begin_drain()
        tasks._run_task_dedicated(app_ctx, app_ctx.config_obj, task_id, 1)
        backend.assert_not_called()
        sleep.assert_not_called()
        assert db.session.get(Task, task_id).status == "queued"


def test_running_pooled_attempt_releases_its_slot_during_drain(app_ctx, queued_run):
    from app.jobqueue import tasks
    from app.services import license_service

    task_id, pool, instance, backend = queued_run

    def finish_running(*_args, **_kwargs):
        assert license_service.get_in_use() == 1
        license_service.begin_drain()

    backend.side_effect = finish_running
    with app_ctx.app_context():
        tasks._run_task_pooled(app_ctx, app_ctx.config_obj, task_id, 1)
        backend.assert_called_once()
        pool.release.assert_called_once_with(instance)
        assert license_service.get_in_use() == 0
        assert license_service.get_limit() == 2
        assert license_service.is_draining()


@pytest.mark.parametrize("delivery", ["stale", "already-running", "current"])
def test_busy_ai_slots_reschedule_only_current_queued_attempts(app_ctx, monkeypatch, delivery):
    from app.extensions import db
    from app.jobqueue import tasks
    from app.models import AiDraft, Project
    from app.services.ai import jobs

    with app_ctx.app_context():
        project = Project(code="DELIVERY", name="Delivery fence")
        db.session.add(project)
        db.session.flush()
        draft = AiDraft(project_id=project.id, scenario="viewpoint")
        attempt = jobs.prepare(draft)
        db.session.add(draft)
        db.session.commit()
        draft_id = draft.id
        if delivery == "already-running":
            assert jobs.claim(draft_id, attempt)
    monkeypatch.setattr(tasks, "_get_app", lambda: app_ctx)
    monkeypatch.setattr(tasks.huey, "immediate", False)
    acquire = Mock(return_value=False)
    schedule = Mock()
    monkeypatch.setattr(jobs, "acquire_slot", acquire)
    monkeypatch.setattr(tasks.run_ai_generation, "schedule", schedule)
    tasks.run_ai_generation.call_local(draft_id, "old-attempt" if delivery == "stale" else attempt)
    if delivery == "current":
        schedule.assert_called_once_with(args=(draft_id, attempt), delay=2)
        acquire.assert_called_once()
    else:
        schedule.assert_not_called()
        acquire.assert_not_called()
