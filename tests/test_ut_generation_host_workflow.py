"""Real LLVM execution must release a still-running batch generation barrier."""

import json
import subprocess

import pytest
from test_ut_host_workflow import real_workflow as workflow_fixture
from test_ut_pipeline import _job
from test_ut_pipeline import env as workflow_env  # noqa: F401

real_workflow = workflow_fixture


@pytest.mark.parametrize("real_workflow", ["explicit"], indirect=True)
def test_completed_host_unit_executes_before_the_next_unit_generates(real_workflow, monkeypatch, tmp_path):
    env, bridge, calls, _config = real_workflow
    marker, waiting = tmp_path / "first-executed", tmp_path / "second-waiting"
    script = f"""
import time
from pathlib import Path
from ut_agent.targets.winams import index
original=index._generate_single_target
def gated(*args,**kwargs):
    row=kwargs.get('row') or args[0]
    if row.function=='relay':
        Path({str(waiting)!r}).touch()
        deadline=time.monotonic()+30
        while not Path({str(marker)!r}).exists():
            if time.monotonic()>deadline:
                raise RuntimeError('completed function waited for whole-batch generation')
            time.sleep(.01)
    return original(*args,**kwargs)
index._generate_single_target=gated
from ut_agent.cli import main
raise SystemExit(main())
"""
    spawn = subprocess.Popen

    def instrument(command, **kwargs):
        if command[1:5] == ["-m", "ut_agent", "bridge", "generate"]:
            command = [command[0], "-c", script, *command[3:], "--jobs", "1"]
        return spawn(command, **kwargs)

    monkeypatch.setattr(subprocess, "Popen", instrument)
    execute = bridge.execute
    executions = []

    def observed(**kwargs):
        result = execute(**kwargs)
        if not executions:
            assert waiting.exists() and not marker.exists()
            assert result["execution"]["status"] == "HOST_PASS"
            marker.touch()
        executions.append(result)
        return result

    monkeypatch.setattr(bridge, "execute", observed)
    assert env.call("post", "/targets/import")[0] == 200
    _, targets = env.call("get", "/targets")
    ids = [target["id"] for target in targets["data"] if target["function"] in {"classify", "relay"}]
    status, batch = env.call("post", "/batches", json={"target_ids": ids})
    assert status == 202, batch
    _, jobs = env.call("get", "/jobs", query_string={"batch_id": batch["data"]["id"]})
    for item in jobs["data"]:
        job = _job(env, item["id"])
        assert job["state"] == "awaiting_review", {"state": job["state"], "reason": job["reason"]}
        assert len(job["attempts"]) == 1 and job["reviewed_at"] is None
        timing = job["timing"]
        assert timing["history_complete"]
        assert any(row["step"] == "host_execute" and row["duration_ms"] > 0 for row in timing["steps"])
        assert all(row["status"] == "completed" for row in timing["generation"]["function"])
        assert {row["phase"] for row in timing["generation"]["function"]} >= {
            "solving", "single_function_generation", "csv_projection"}
        assert job["generation"]["phase_timings"]
        queued = next(row for row in timing["generation"]["function"] if row["phase"] == "queued")
        assert queued["ended_at"] and queued["duration_ms"] >= 0
        assert len(timing["generation"]["shared"]) == 1
        assert timing["generation"]["shared"][0]["status"] == "completed"
        from app.services.ut_pipeline import pipeline

        monkeypatch.setattr(pipeline, "_generation_progress", lambda _: {})
        assert _job(env, item["id"])["timing"]["generation"]["shared"] == timing["generation"]["shared"]
    assert len(executions) == 2 and marker.exists()
    generation_calls = [call for call in calls if call[0] == "generate"]
    assert len(generation_calls) == 1
    assert not any(call[0] in {"winams-execute", "capture"} for call in calls)
    out = generation_calls[0][generation_calls[0].index("--out") + 1]
    from pathlib import Path

    report = json.loads((Path(out) / "index-generation-report.json").read_text(encoding="utf-8"))
    assert report["extraction"]["mode"] == "one_cpp_invocation_targets_file"
    assert report["extraction"]["targets"] == 2
