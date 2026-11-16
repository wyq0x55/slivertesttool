"""The worker runtime lock is exclusive and precedes license recovery."""

import pytest

from test_run_evidence import submitted  # noqa: F401


@pytest.fixture(autouse=True)
def isolated_worker_ownership(monkeypatch):
    from app.services import worker_lock

    monkeypatch.setattr(worker_lock, "_active_owner", None)
    worker_lock._ownership_lost.clear()
    yield
    worker_lock._ownership_lost.clear()


def _set_in_use(app, value: int) -> None:
    from app.extensions import db
    from app.models import Setting

    with app.app_context():
        row = db.session.get(Setting, Setting.LICENSE_INUSE)
        assert row is not None
        row.value = str(value)
        db.session.commit()


def test_second_worker_cannot_reset_a_live_license_counter(app_ctx):
    from app.services import license_service, worker_lock
    import run_worker

    _set_in_use(app_ctx, 5)
    owner = run_worker.begin_worker_ownership(app_ctx)
    try:
        with app_ctx.app_context():
            assert license_service.get_in_use() == 0
        _set_in_use(app_ctx, 5)
        with pytest.raises(worker_lock.WorkerAlreadyRunning):
            run_worker.begin_worker_ownership(app_ctx)
        with app_ctx.app_context():
            assert license_service.get_in_use() == 5
    finally:
        owner.release()

    successor = run_worker.begin_worker_ownership(app_ctx)
    try:
        with app_ctx.app_context():
            assert license_service.get_in_use() == 0
    finally:
        successor.release()


def test_released_lock_can_be_acquired_again(app_ctx):
    from app.services import worker_lock

    with app_ctx.app_context():
        first = worker_lock.acquire()
        from sqlalchemy import text
        from app.extensions import db
        pid = first._connection.execute(text("SELECT pg_backend_pid()")).scalar()
        with db.engine.connect() as other:
            state = other.execute(
                text("SELECT state FROM pg_stat_activity WHERE pid = :pid"),
                {"pid": pid},
            ).scalar()
        assert state == "idle"
        with pytest.raises(worker_lock.WorkerAlreadyRunning):
            worker_lock.acquire()
        first.release()
        second = worker_lock.acquire()
        second.release()


def test_dead_lock_session_lets_the_successor_take_ownership(app_ctx):
    """Killing the holder session must not leave the old worker as owner."""
    import threading

    from sqlalchemy import text

    from app.extensions import db
    from app.services import license_service, worker_lock
    import run_worker

    lost = threading.Event()
    _set_in_use(app_ctx, 5)
    owner = run_worker.begin_worker_ownership(app_ctx)
    try:
        _set_in_use(app_ctx, 4)
        owner.supervise(lost.set, interval=0.05)
        with app_ctx.app_context():
            pid = owner._connection.execute(text("SELECT pg_backend_pid()")).scalar()
            with db.engine.connect() as other:
                terminated = other.execute(
                    text("SELECT pg_terminate_backend(:pid)"), {"pid": pid}
                ).scalar()
                other.commit()
        assert terminated is True
        assert lost.wait(2)
        assert worker_lock.ownership_was_lost()
        worker_lock._ownership_lost.clear()
        successor = run_worker.begin_worker_ownership(app_ctx)
        try:
            with app_ctx.app_context():
                assert license_service.get_in_use() == 0
        finally:
            successor.release()
    finally:
        worker_lock._ownership_lost.clear()
        owner.release()


@pytest.mark.parametrize("error_type", ["runner", "internal"])
def test_lock_loss_during_error_event_does_not_cancel_the_queue(
    submitted, monkeypatch, error_type
):
    from types import SimpleNamespace

    from app.extensions import db
    from app.models import TaskStatus
    from app.runners import test_runner
    from app.runners.silver_runner import RunnerError
    from app.services import worker_lock
    from test_run_evidence import service

    application, task, _source, _model = submitted
    cancelled = []
    exception = RunnerError if error_type == "runner" else ValueError

    def run(_context):
        raise exception("execution failed")

    monkeypatch.setattr(test_runner, "build_runner", lambda backend: SimpleNamespace(run=run))
    monkeypatch.setattr(
        test_runner.event_service, "emit_error",
        lambda *_args, **_kwargs: worker_lock._ownership_lost.set(),
    )
    monkeypatch.setattr(
        test_runner, "_cancel_project_queue_on_error",
        lambda *_args: cancelled.append(True),
    )
    task.status = TaskStatus.RUNNING.value
    db.session.commit()
    try:
        test_runner.execute(application, application.config_obj, task)
        db.session.refresh(task)
        assert task.status == TaskStatus.RUNNING.value
        assert not task.result
        assert service().read_evidence(task)["outcome"] is None
        assert cancelled == []
    finally:
        worker_lock._ownership_lost.clear()


def test_queue_cancellation_is_fenced_after_lock_loss(monkeypatch):
    from types import SimpleNamespace

    from app.runners import test_runner
    from app.services import worker_lock

    cancelled = []
    monkeypatch.setattr(
        test_runner.task_service, "cancel_project_queue",
        lambda *_args, **_kwargs: cancelled.append(True),
    )
    worker_lock._ownership_lost.set()
    try:
        test_runner._cancel_project_queue_on_error(
            SimpleNamespace(task_key="lost-worker"), "execution failed"
        )
    finally:
        worker_lock._ownership_lost.clear()
    assert cancelled == []


def test_finalisation_rejected_by_service_does_not_cancel_queue(submitted, monkeypatch):
    from app.runners import test_runner

    _application, task, _source, _model = submitted
    cancelled = []
    monkeypatch.setattr(
        "app.services.run_finalisation_service.finalise_attempt",
        lambda *_args, **_kwargs: False,
    )
    monkeypatch.setattr(
        test_runner, "_cancel_project_queue_on_error",
        lambda *_args: cancelled.append(True),
    )
    test_runner._fail_internal(task, ValueError("stale attempt"))
    assert cancelled == []


def test_lock_loss_during_evidence_read_does_not_seal(submitted, monkeypatch):
    from app.extensions import db
    from app.models import TaskStatus
    from app.runners import test_runner
    from app.services import worker_lock
    from test_run_evidence import service

    _application, task, _source, _model = submitted
    original_read = test_runner.evidence.read_evidence

    def read(*args, **kwargs):
        result = original_read(*args, **kwargs)
        worker_lock._ownership_lost.set()
        return result

    task.status = TaskStatus.RUNNING.value
    db.session.commit()
    monkeypatch.setattr(test_runner.evidence, "read_evidence", read)
    assert test_runner._finalise(task, TaskStatus.PASSED, "", "PASS") is False
    db.session.refresh(task)
    assert task.status == TaskStatus.RUNNING.value
    assert original_read(task)["outcome"] is None


def test_takeover_waits_for_business_transaction_even_after_session_dies(app_ctx):
    import threading

    from sqlalchemy import text

    from app.extensions import db
    from app.services import worker_lock

    acquired = threading.Event()
    started = threading.Event()
    successors = []
    failures = []

    def take_over():
        try:
            with app_ctx.app_context():
                started.set()
                successors.append(worker_lock.acquire())
                acquired.set()
        except Exception as exc:
            failures.append(exc)

    with app_ctx.app_context():
        owner = worker_lock.acquire()
        successor_thread = threading.Thread(target=take_over)
        try:
            with worker_lock.ownership_guard() as owned:
                assert owned
                pid = owner._connection.execute(text("SELECT pg_backend_pid()")).scalar()
                with db.engine.connect() as other:
                    other.execute(text("SELECT pg_terminate_backend(:pid)"), {"pid": pid})
                    other.commit()
                successor_thread.start()
                assert started.wait(2)
                assert not acquired.wait(0.2)
                db.session.commit()
            assert acquired.wait(3)
            assert failures == []
        finally:
            successor_thread.join(3)
            for successor in successors:
                successor.release()
            owner.release()


def test_old_owner_release_cannot_decrement_successor_counter(app_ctx, monkeypatch):
    from sqlalchemy import text

    from app.extensions import db
    from app.services import license_service, worker_lock
    import run_worker

    with app_ctx.app_context():
        owner = run_worker.begin_worker_ownership(app_ctx)
        successor = None
        try:
            assert license_service.try_acquire()
            pid = owner._connection.execute(text("SELECT pg_backend_pid()")).scalar()
            with db.engine.connect() as other:
                other.execute(text("SELECT pg_terminate_backend(:pid)"), {"pid": pid})
                other.commit()
            successor = run_worker.begin_worker_ownership(app_ctx)
            assert license_service.try_acquire()
            assert license_service.get_in_use() == 1
            monkeypatch.setattr(worker_lock, "_active_owner", owner)
            license_service.release()
            assert worker_lock.ownership_was_lost()
            db.session.expire_all()
            assert license_service.get_in_use() == 1
        finally:
            if successor is not None:
                successor.release()
            owner.release()


def test_takeover_recovers_seal_without_allowing_old_finalisation(submitted, monkeypatch):
    import threading

    from sqlalchemy import text

    from app.extensions import db
    from app.models import TaskStatus
    from app.runners import test_runner
    from app.services import worker_lock

    application, task, _source, _model = submitted
    original_seal = test_runner.evidence.seal_attempt
    acquired = threading.Event()
    successors = []

    def take_over():
        with application.app_context():
            previous = worker_lock._active_owner
            successors.append(worker_lock.acquire())
            worker_lock._active_owner = previous
            acquired.set()

    owner = worker_lock.acquire()
    successor_thread = threading.Thread(target=take_over)

    def seal(*args, **kwargs):
        pid = owner._connection.execute(text("SELECT pg_backend_pid()")).scalar()
        with db.engine.connect() as other:
            other.execute(text("SELECT pg_terminate_backend(:pid)"), {"pid": pid})
            other.commit()
        successor_thread.start()
        assert not acquired.wait(0.2)
        result = original_seal(*args, **kwargs)
        assert not acquired.is_set()
        return result

    task.status = TaskStatus.RUNNING.value
    db.session.commit()
    monkeypatch.setattr(test_runner.evidence, "seal_attempt", seal)
    try:
        assert not test_runner._finalise(task, TaskStatus.PASSED, "", "PASS")
        assert acquired.wait(3)
        monkeypatch.setattr(worker_lock, "_active_owner", successors[0])
        worker_lock._ownership_lost.clear()
        from app.services.run_finalisation_service import finalise_attempt

        assert finalise_attempt(task.id, task.run_count, "passed", "PASS", "")
        db.session.refresh(task)
        assert task.status == TaskStatus.PASSED.value
        assert test_runner.evidence.read_evidence(task)["outcome"]["verdict"] == "PASS"
    finally:
        successor_thread.join(3)
        for successor in successors:
            successor.release()
        owner.release()


def test_losing_both_connections_cannot_commit_over_successor(app_ctx):
    import threading

    from sqlalchemy import text
    from sqlalchemy.exc import DBAPIError

    from app.extensions import db
    from app.models import Setting
    from app.services import license_service, worker_lock
    import run_worker

    acquired = threading.Event()
    successors = []

    def take_over():
        with app_ctx.app_context():
            previous = worker_lock._active_owner
            successor = run_worker.begin_worker_ownership(app_ctx)
            successors.append(successor)
            assert license_service.try_acquire()
            worker_lock._active_owner = previous
            acquired.set()

    with app_ctx.app_context():
        owner = run_worker.begin_worker_ownership(app_ctx)
        successor_thread = threading.Thread(target=take_over)
        try:
            with pytest.raises(DBAPIError):
                with worker_lock.ownership_guard() as owned:
                    assert owned
                    owner_pid = owner._connection.execute(text("SELECT pg_backend_pid()")).scalar()
                    business_pid = db.session.execute(text("SELECT pg_backend_pid()")).scalar()
                    db.session.execute(
                        text("UPDATE app_settings SET value = '8' WHERE key = :key"),
                        {"key": Setting.LICENSE_INUSE},
                    )
                    with db.engine.connect() as other:
                        for pid in (owner_pid, business_pid):
                            other.execute(text("SELECT pg_terminate_backend(:pid)"), {"pid": pid})
                        other.commit()
                    successor_thread.start()
                    assert acquired.wait(3)
                    db.session.commit()
            license_service.release()
            assert worker_lock.ownership_was_lost()
            db.session.expire_all()
            assert license_service.get_in_use() == 1
        finally:
            successor_thread.join(3)
            for successor in successors:
                successor.release()
            owner.release()


def test_healthy_worker_recovery_creates_its_own_app_context(app_ctx, monkeypatch):
    from app.services import worker_lock
    import run_worker

    calls = []
    with app_ctx.app_context():
        owner = worker_lock.acquire()
    monkeypatch.setattr("app.services.ai.jobs.recover", lambda *args, **kwargs: calls.append("ai"))
    monkeypatch.setattr(
        "app.jobqueue.tasks.recover_run_attempts",
        lambda **kwargs: calls.append("runs") or {"errors": []},
    )
    monkeypatch.setattr(
        "app.jobqueue.ut_tasks.recover_interrupted_jobs",
        lambda: calls.append("unit") or {"interrupted": [], "stopped": []},
    )
    try:
        run_worker.recover_owned_work(app_ctx, prewarm=lambda: calls.append("prewarm"))
        assert calls == ["ai", "runs", "unit", "prewarm"]
    finally:
        owner.release()


def test_lost_ownership_does_not_finalize_or_cancel_the_attempt(submitted, monkeypatch):
    from types import SimpleNamespace

    from app.extensions import db
    from app.models import TaskStatus
    from app.runners import test_runner
    from app.runners.silver_runner import RunnerError
    from app.services import worker_lock
    from test_run_evidence import service

    application, task, _source, _model = submitted
    cancelled = []
    worker_lock._ownership_lost.set()
    try:
        def run(_context):
            raise RunnerError("lock session disappeared")

        monkeypatch.setattr(test_runner, "build_runner", lambda backend: SimpleNamespace(run=run))
        monkeypatch.setattr(test_runner, "_write_row_result", lambda *args, **kwargs: cancelled.append("write"))
        monkeypatch.setattr(test_runner, "_cancel_project_queue_on_error", lambda *args: cancelled.append(args))
        task.status = TaskStatus.RUNNING.value
        db.session.commit()
        test_runner.execute(application, application.config_obj, task)
        db.session.refresh(task)
        assert task.status == TaskStatus.RUNNING.value
        assert not task.result
        assert service().read_evidence(task)["outcome"] is None
        assert cancelled == []
    finally:
        worker_lock._ownership_lost.clear()


def test_startup_recovery_stops_when_the_lock_is_already_lost(monkeypatch):
    from app.services import worker_lock
    import run_worker

    calls = []
    monkeypatch.setattr("app.services.ai.jobs.recover", lambda *args, **kwargs: calls.append("ai"))
    monkeypatch.setattr("app.jobqueue.tasks.recover_run_attempts", lambda **kwargs: calls.append("runs"))
    monkeypatch.setattr("app.jobqueue.ut_tasks.recover_interrupted_jobs", lambda: calls.append("unit"))
    worker_lock._ownership_lost.set()
    try:
        with pytest.raises(SystemExit):
            run_worker.recover_owned_work(object(), prewarm=lambda: calls.append("prewarm"))
    finally:
        worker_lock._ownership_lost.clear()
    assert calls == []


def test_cancellation_after_lock_loss_does_not_seal_the_attempt(submitted, monkeypatch):
    from types import SimpleNamespace

    from app.extensions import db
    from app.models import TaskStatus
    from app.runners import test_runner
    from app.runners.silver_runner import RunnerCancelled
    from app.services import worker_lock
    from test_run_evidence import service

    application, task, _source, _model = submitted
    worker_lock._ownership_lost.set()
    try:
        monkeypatch.setattr(
            test_runner, "build_runner",
            lambda backend: SimpleNamespace(run=lambda _context: (_ for _ in ()).throw(RunnerCancelled())),
        )
        task.status = TaskStatus.RUNNING.value
        db.session.commit()
        test_runner.execute(application, application.config_obj, task)
        db.session.refresh(task)
        assert task.status == TaskStatus.RUNNING.value
        assert task.result != "CANCELLED"
        assert service().read_evidence(task)["outcome"] is None
    finally:
        worker_lock._ownership_lost.clear()


def test_lock_loss_during_verdict_parsing_does_not_record_pass(submitted, monkeypatch):
    from types import SimpleNamespace

    from app.extensions import db
    from app.models import TaskStatus
    from app.runners import test_runner
    from app.services import worker_lock
    from test_run_evidence import service

    application, task, _source, _model = submitted

    def run(context):
        (context.log_dir / "jdgrslt.log").write_text("Test is Passed.\n", encoding="utf-8")

    def parse(*_args, **_kwargs):
        worker_lock._ownership_lost.set()
        return "PASS"

    monkeypatch.setattr(test_runner, "build_runner", lambda backend: SimpleNamespace(run=run))
    monkeypatch.setattr(test_runner, "_parse_verdict", parse)
    task.status = TaskStatus.RUNNING.value
    db.session.commit()
    try:
        test_runner.execute(application, application.config_obj, task)
        db.session.refresh(task)
        assert task.status == TaskStatus.RUNNING.value
        assert task.result != "PASS"
        assert service().read_evidence(task)["outcome"] is None
    finally:
        worker_lock._ownership_lost.clear()
