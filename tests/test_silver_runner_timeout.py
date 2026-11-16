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
        open=Mock(), exit=Mock(), clear=Mock(),
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
        handle.clear.assert_called_once_with()
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


def stop_record(module="judge.py"):
    return (
        f"WARNING Silver: Simulation ended by 'Python.dll: {module} (Python3)' "
        "during modify_outputs()\n"
    ).encode()


@pytest.mark.parametrize("pooled", [False, True])
@pytest.mark.parametrize("json_mode", [False, True])
@pytest.mark.parametrize("verdict", ["PASS", "FAIL"])
def test_normal_judge_stop_is_not_a_timeout(context, monkeypatch, pooled, json_mode, verdict):
    runner = SilverRunner()
    console = context.log_dir / "Console.log"
    context.log_dir.mkdir(parents=True)
    module = "silver_json_runner.py" if json_mode else "judge.py"
    testcase = context.run_dir / context.test_id
    testcase.mkdir(parents=True)
    (testcase / module).touch()
    if pooled:
        context.console_log = context.log_dir.parent / "pool-console.log"
        console = context.console_log
    console.write_bytes(b"previous case\n")

    def run_until(*_args, **_kwargs):
        (context.log_dir / "jdgrslt.log").write_text(
            "Test is Passed." if verdict == "PASS" else "Test is failed in Step6!!!!",
            encoding="utf-8",
        )
        with console.open("ab") as stream:
            stream.write(stop_record(module))
        return False

    handle = SimpleNamespace(
        set_silver_property=Mock(), run_until=run_until, open=Mock(), exit=Mock(),
        clear=Mock(),
    )
    monkeypatch.setattr(runner, "prepare_run_sil", lambda _context: context.sil_path)
    monkeypatch.setattr(runner, "launch", lambda *_args: handle)
    if pooled:
        runner.configure_and_run(handle, context)
    else:
        runner.run(context)


@pytest.mark.parametrize("signal", ["stale", "wrong_module", "spoofed", "pass_only", "truncated"])
def test_false_completion_requires_current_judge_stop(context, signal):
    context.log_dir.mkdir(parents=True)
    console = context.log_dir / "Console.log"
    console.write_bytes(stop_record() if signal in {"stale", "truncated"} else b"")

    def run_until(*_args, **_kwargs):
        (context.log_dir / "jdgrslt.log").write_text("Test is Passed.", encoding="utf-8")
        if signal == "wrong_module":
            console.write_bytes(stop_record("other.py"))
        elif signal == "spoofed":
            console.write_bytes(b"INFO Silver: " + stop_record())
        elif signal == "truncated":
            console.write_bytes(b"")
        return False

    handle = SimpleNamespace(set_silver_property=Mock(), run_until=run_until)
    with pytest.raises(RunnerError, match="^timeout$"):
        SilverRunner()._execute(handle, context)


@pytest.mark.parametrize("cancelled", [False, True])
def test_stop_record_does_not_override_exception_or_cancellation(context, cancelled):
    context.log_dir.mkdir(parents=True)
    console = context.log_dir / "Console.log"

    def run_until(*_args, **_kwargs):
        console.write_bytes(stop_record())
        if cancelled:
            context.cancel_event.set()
            return False
        raise RuntimeError("module failure")

    handle = SimpleNamespace(set_silver_property=Mock(), run_until=run_until)
    expected = RunnerCancelled if cancelled else RunnerError
    with pytest.raises(expected, match="Cancelled during run|Execution error"):
        SilverRunner()._execute(handle, context)


@pytest.mark.parametrize("failure", [None, "timeout", "cancelled"])
def test_pooled_cleanup_failure_preserves_execution_outcome(context, monkeypatch, failure):
    runner = SilverRunner()
    monkeypatch.setattr(runner, "prepare_run_sil", lambda _context: context.sil_path)
    handle = SimpleNamespace(open=Mock(), clear=Mock(side_effect=RuntimeError("unload failed")))
    if failure == "timeout":
        expected = RunnerError("timeout")
    elif failure == "cancelled":
        expected = RunnerCancelled("Cancelled during run.")
    else:
        expected = None
    monkeypatch.setattr(runner, "_execute", Mock(side_effect=expected))

    with pytest.raises(RunnerError if expected is None else type(expected)) as caught:
        runner.configure_and_run(handle, context)
    if expected is None:
        assert str(caught.value) == "Failed to unload Silver model: unload failed"
    else:
        assert caught.value is expected
    handle.clear.assert_called_once_with()
