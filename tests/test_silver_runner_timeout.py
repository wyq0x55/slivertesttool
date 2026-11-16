from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from app.runners.silver_runner import (
    RunContext, RunnerCancelled, RunnerError, SilverRunner,
)


@pytest.fixture
def context(tmp_path):
    return RunContext(
        test_id="TC1", run_dir=tmp_path / "inputs", log_dir=tmp_path / "logs",
        sil_path=tmp_path / "model.sil", gui=False, timeout=5,
    )


@pytest.mark.parametrize("pooled", [False, True])
@pytest.mark.parametrize("completed", [False, True])
def test_run_completion_and_timeout(context, monkeypatch, pooled, completed):
    runner = SilverRunner()
    handle = SimpleNamespace(
        set_silver_property=Mock(), run_until=Mock(return_value=completed),
        open=Mock(), exit=Mock(),
    )
    monkeypatch.setattr(runner, "prepare_run_sil", lambda _context: context.sil_path)
    monkeypatch.setattr(runner, "launch", lambda *_args: handle)
    run = (lambda: runner.configure_and_run(handle, context)) if pooled else (
        lambda: runner.run(context))

    if completed:
        run()
    else:
        with pytest.raises(RunnerError, match="^timeout$"):
            run()

    handle.run_until.assert_called_once_with("isFinished != True", timeout=5)
    if pooled:
        handle.open.assert_called_once_with(str(context.sil_path))
        handle.exit.assert_not_called()
    else:
        handle.exit.assert_called_once_with(force=True)


@pytest.mark.parametrize("completed", [False, True])
def test_cancellation_takes_precedence_over_completion(context, completed):
    def run_until(*_args, **_kwargs):
        context.cancel_event.set()
        return completed

    handle = SimpleNamespace(set_silver_property=Mock(), run_until=run_until)
    with pytest.raises(RunnerCancelled, match="Cancelled during run"):
        SilverRunner()._execute(handle, context)


def test_cancellation_before_run_does_not_start_execution(context):
    context.cancel_event.set()
    handle = SimpleNamespace(set_silver_property=Mock(), run_until=Mock())

    with pytest.raises(RunnerCancelled, match="Cancelled before run"):
        SilverRunner()._execute(handle, context)

    handle.run_until.assert_not_called()


@pytest.mark.parametrize("cancelled", [False, True])
def test_execution_error_preserves_cancellation_classification(context, cancelled):
    def run_until(*_args, **_kwargs):
        if cancelled:
            context.cancel_event.set()
        raise RuntimeError("Silver stopped")

    handle = SimpleNamespace(set_silver_property=Mock(), run_until=run_until)
    expected = RunnerCancelled if cancelled else RunnerError
    with pytest.raises(expected, match="Cancelled during run|Execution error"):
        SilverRunner()._execute(handle, context)
