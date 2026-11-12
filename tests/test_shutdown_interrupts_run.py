"""A run broken by the worker's own shutdown is an interruption, not an ERROR.

The worker's exit path disposes every pooled Silver instance, including ones a
task thread is still running on. The resulting runner error must not finalise
the attempt as ERROR or cancel the project's queue: the attempt stays RUNNING
for startup recovery, as it did when the worker was simply killed.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.runners import process_guard
from test_run_evidence import service, submitted  # noqa: F401 - shared fixtures


@pytest.fixture
def shutting_down(monkeypatch):
    monkeypatch.setattr(process_guard, "_shutting_down", process_guard.threading.Event())
    process_guard.begin_shutdown()
    yield


def test_shutdown_runner_error_leaves_the_attempt_for_recovery(submitted, shutting_down, monkeypatch):
    from app.extensions import db
    from app.models import TaskStatus
    from app.runners import test_runner
    from app.runners.silver_runner import RunnerError

    application, task, _source, _model = submitted
    cancelled, writes = [], []

    def run(_context):
        raise RunnerError("Silver instance exited")

    monkeypatch.setattr(test_runner, "build_runner", lambda backend: SimpleNamespace(run=run))
    monkeypatch.setattr(test_runner, "_write_row_result", lambda *args, **kwargs: writes.append(args))
    monkeypatch.setattr(test_runner, "_cancel_project_queue_on_error",
                        lambda *args: cancelled.append(args))
    with application.app_context():
        task.status = TaskStatus.RUNNING.value
        db.session.commit()
        test_runner.execute(application, application.config_obj, task)
        db.session.refresh(task)
        assert task.status == TaskStatus.RUNNING.value
        assert not task.result
        assert service().read_evidence(task)["outcome"] is None
    assert not cancelled and not writes
