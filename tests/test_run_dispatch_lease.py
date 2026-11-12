"""Periodic recovery must not re-publish run attempts a worker already holds.

A dequeued run message is invisible to ``huey.pending()`` while its thread waits
for a license or pool instance, and license retries sit on the schedule. Both
used to be re-published every minute, and every duplicate occupied another
consumer thread. Waiting threads now hold a dispatch lease, recovery also reads
the schedule, and only a bounded number of attempts may block a thread.
"""
from __future__ import annotations

import contextlib
import types
from pathlib import Path
from unittest.mock import Mock

import pytest

from app.jobqueue import tasks
from app.runners.silver_pool import SilverInstancePool
from test_silver_pool import FakeDriver
from test_worker_shutdown_drain import queued_run  # noqa: F401 - shared fixture


@pytest.fixture(autouse=True)
def clean_queue():
    tasks.huey.flush()
    tasks._waiters = 0
    yield
    tasks.huey.flush()
    tasks._waiters = 0


def test_dispatch_lease_is_live_until_released_or_stale():
    lease = tasks._DispatchLease(7, 2)
    assert not tasks.dispatch_lease_is_live(7, 2)
    lease.renew(force=True)
    assert tasks.dispatch_lease_is_live(7, 2)
    assert not tasks.dispatch_lease_is_live(7, 3)
    stamp = tasks.huey.get(tasks._lease_key(7, 2), peek=True)[0]
    assert not tasks.dispatch_lease_is_live(7, 2, now=stamp + tasks._DISPATCH_LEASE_TTL_SECONDS + 1)
    lease.release()
    assert not tasks.dispatch_lease_is_live(7, 2)


def test_dispatch_lease_renewal_is_throttled_and_never_raises(monkeypatch):
    writes = []
    monkeypatch.setattr(tasks.huey, "put", lambda key, value: writes.append(key))
    lease = tasks._DispatchLease(1, 1)
    lease.renew(force=True)
    lease.renew()
    assert len(writes) == 1

    def broken(*_args):
        raise RuntimeError("queue database unavailable")

    monkeypatch.setattr(tasks.huey, "put", broken)
    tasks._DispatchLease(1, 2).renew(force=True)  # must not raise


def test_recovery_skips_scheduled_and_leased_attempts(monkeypatch):
    seen = {}

    def recover_queued(enqueue, *, is_pending):
        seen.update({identity: is_pending(*identity) for identity in ((1, 1), (2, 1), (3, 1), (3, 2))})
        return {"recovered": [], "skipped": [], "errors": []}

    monkeypatch.setattr("app.services.run_evidence_service.recover_queued_attempts", recover_queued)
    monkeypatch.setattr("app.services.run_recovery_service.recover_interrupted_attempts",
                        lambda *_args, **_kwargs: {"recovered": [], "skipped": [], "errors": []})
    monkeypatch.setattr(tasks.huey, "pending", lambda: [tasks.run_task.s(3, 2)])
    monkeypatch.setattr(tasks.huey, "scheduled", lambda: [tasks.run_task.s(1, 1)])
    tasks._DispatchLease(2, 1).renew(force=True)

    tasks.recover_run_attempts()
    assert seen == {(1, 1): True, (2, 1): True, (3, 1): False, (3, 2): True}


def test_wait_capacity_keeps_one_waiter_per_license_and_a_free_thread(monkeypatch):
    from app.services import license_service

    app = types.SimpleNamespace(config_obj=types.SimpleNamespace(HUEY_WORKERS=16),
                                app_context=contextlib.nullcontext)
    for limit, expected in ((1, 1), (2, 2), (8, 7), (16, 1)):
        monkeypatch.setattr(license_service, "get_limit", lambda limit=limit: limit)
        assert tasks._wait_capacity(app) == expected


def test_waiter_slots_are_bounded(monkeypatch):
    monkeypatch.setattr(tasks, "_wait_capacity", lambda app: 1)
    assert tasks._try_enter_wait(None)
    assert not tasks._try_enter_wait(None)
    tasks._leave_wait()
    assert tasks._try_enter_wait(None)


def test_non_blocking_pool_acquire_returns_at_capacity(tmp_path):
    pool = SilverInstancePool(FakeDriver(), Path(tmp_path))
    pool.set_target(1)
    first = pool.acquire(Path("m.sil"), blocking=False)
    assert first is not None  # created lazily below target
    should_cancel = Mock(return_value=False)
    assert pool.acquire(Path("m.sil"), should_cancel=should_cancel, blocking=False) is None
    pool.release(first)
    assert pool.acquire(Path("m.sil"), blocking=False) is first
    pool.shutdown()


# --- worker paths (PostgreSQL fixture) ------------------------------------- #
def test_pooled_overflow_is_rescheduled_instead_of_blocking(app_ctx, queued_run, monkeypatch):
    from app.extensions import db
    from app.models import Task

    task_id, pool, _instance, backend = queued_run
    pool.acquire.return_value = None
    rescheduled = []
    monkeypatch.setattr(tasks, "_try_enter_wait", lambda app: False)
    monkeypatch.setattr(tasks, "_reschedule", lambda *args: rescheduled.append(args))
    with app_ctx.app_context():
        tasks._run_task_pooled(app_ctx, app_ctx.config_obj, task_id, 1)
        assert pool.acquire.call_args.kwargs["blocking"] is False
        assert rescheduled == [(task_id, 1, tasks._OVERFLOW_RETRY_SECONDS)]
        backend.assert_not_called()
        assert db.session.get(Task, task_id).status == "queued"
        assert not tasks.dispatch_lease_is_live(task_id, 1)


def test_pooled_waiter_holds_a_dispatch_lease_while_waiting(app_ctx, queued_run, monkeypatch):
    task_id, pool, _instance, _backend = queued_run
    observed = {}

    def waiting_acquire(*_args, should_cancel, blocking, **_kwargs):
        observed["blocking"] = blocking
        observed["live"] = tasks.dispatch_lease_is_live(task_id, 1)
        observed["waiters"] = tasks._waiters
        return None

    pool.acquire.side_effect = waiting_acquire
    monkeypatch.setattr(tasks, "_wait_capacity", lambda app: 1)
    with app_ctx.app_context():
        tasks._run_task_pooled(app_ctx, app_ctx.config_obj, task_id, 1)
    assert observed == {"blocking": True, "live": True, "waiters": 1}
    assert tasks._waiters == 0
    assert not tasks.dispatch_lease_is_live(task_id, 1)


def test_dedicated_overflow_is_rescheduled_instead_of_sleeping(app_ctx, queued_run, monkeypatch):
    from app.extensions import db
    from app.models import Task
    from app.services import license_service

    task_id, _pool, _instance, backend = queued_run
    rescheduled = []
    monkeypatch.setattr(tasks, "_try_enter_wait", lambda app: False)
    monkeypatch.setattr(tasks, "_reschedule", lambda *args: rescheduled.append(args))
    monkeypatch.setattr(tasks.time, "sleep", Mock(side_effect=AssertionError("must not wait")))
    with app_ctx.app_context():
        license_service.set_limit(1)
        assert license_service.try_acquire()
        tasks._run_task_dedicated(app_ctx, app_ctx.config_obj, task_id, 1)
        license_service.release()
        assert rescheduled == [(task_id, 1, tasks._OVERFLOW_RETRY_SECONDS)]
        backend.assert_not_called()
        assert db.session.get(Task, task_id).status == "queued"
        assert license_service.get_in_use() == 0


def test_queue_liveness_counts_a_leased_attempt(app_ctx):
    from app.services.run_recovery_service import is_run_pending

    with app_ctx.app_context():
        assert not is_run_pending(42, 3)
        tasks._DispatchLease(42, 3).renew(force=True)
        assert is_run_pending(42, 3)
        assert not is_run_pending(42, 2)


def test_lease_of_a_dead_worker_on_this_host_is_not_live():
    import socket
    import time as _time

    from test_process_guard import _dead_pid

    tasks.huey.put(tasks._lease_key(8, 1), (_time.time(), socket.gethostname(), _dead_pid()))
    assert not tasks.dispatch_lease_is_live(8, 1)
    tasks.huey.put(tasks._lease_key(8, 1), (_time.time(), "other-host", 1))
    assert tasks.dispatch_lease_is_live(8, 1)


def test_recovery_rereads_the_queue_after_a_lease_miss(monkeypatch):
    """A held message is rescheduled before its lease is released; recovery
    must not lose it between its first queue snapshot and the lease check."""
    seen = {}
    scheduled = []

    def recover_queued(enqueue, *, is_pending):
        scheduled.append(tasks.run_task.s(5, 1))  # the hand-off happens now
        seen["held"] = is_pending(5, 1)
        seen["lost"] = is_pending(6, 1)
        return {"recovered": [], "skipped": [], "errors": []}

    monkeypatch.setattr("app.services.run_evidence_service.recover_queued_attempts", recover_queued)
    monkeypatch.setattr("app.services.run_recovery_service.recover_interrupted_attempts",
                        lambda *_args, **_kwargs: {"recovered": [], "skipped": [], "errors": []})
    monkeypatch.setattr(tasks.huey, "pending", lambda: [])
    monkeypatch.setattr(tasks.huey, "scheduled", lambda: list(scheduled))
    tasks.recover_run_attempts()
    assert seen == {"held": True, "lost": False}


def test_run_task_holds_the_lease_before_any_setup(monkeypatch):
    observed = {}

    def get_app():
        observed["live"] = tasks.dispatch_lease_is_live(9, 4)
        raise RuntimeError("stop here")

    monkeypatch.setattr(tasks, "_get_app", get_app)
    with pytest.raises(RuntimeError):
        tasks.run_task.call_local(9, 4)
    assert observed == {"live": True}
    assert not tasks.dispatch_lease_is_live(9, 4)
