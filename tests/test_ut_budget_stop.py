"""Stopping a run: the forecast's early stop and an operator's stop request.

The forecast may end a function once the batch declares how many boundaries must
agree; an operator may end it at any boundary through a stop file.  Both keep the
finished cases, hand the rest to review and never touch an expectation.
"""

import pytest
from test_ut_pipeline import _configure, _enqueue, _job
from test_ut_pipeline import env as workflow_env

from app.services.ut_pipeline import pipeline
from app.services.ut_pipeline.bridge import BridgeError, BridgeSettings, UtAgentBridge

env = workflow_env


# --------------------------------------------------------------------------- #
# Bridge command line (no database)
# --------------------------------------------------------------------------- #
def _bridge(monkeypatch):
    bridge = UtAgentBridge(BridgeSettings("root", "python", "", 5, 5, 5))
    seen = []
    monkeypatch.setattr(bridge, "_run",
                        lambda args, **kwargs: seen.append(list(args)) or {"units": []})
    return bridge, seen


def test_the_stop_observation_count_is_sent_only_when_declared(tmp_path, monkeypatch):
    bridge, seen = _bridge(monkeypatch)
    bridge.generate("m.json", "i.csv", "P", tmp_path, ["f"], progress_dir=tmp_path)
    assert "--budget-stop-observations" not in seen[0]

    bridge.generate("m.json", "i.csv", "P", tmp_path, ["f"], progress_dir=tmp_path,
                    budget_stop_observations=2)
    assert seen[1][seen[1].index("--budget-stop-observations") + 1] == "2"


@pytest.mark.parametrize("value", [0, 11, True, "2", 1.5])
def test_the_observation_count_is_one_to_ten(tmp_path, monkeypatch, value):
    bridge, seen = _bridge(monkeypatch)
    with pytest.raises(BridgeError, match="1 to 10"):
        bridge.generate("m.json", "i.csv", "P", tmp_path, ["f"], progress_dir=tmp_path,
                        budget_stop_observations=value)
    assert seen == []


def test_forecast_history_is_sent_to_generation_when_present(tmp_path, monkeypatch):
    bridge, seen = _bridge(monkeypatch)
    history = tmp_path / "forecast-history"
    bridge.generate("m.json", "i.csv", "P", tmp_path, ["f"], progress_dir=tmp_path,
                    history_dir=history, budget_stop_observations=2)
    assert seen[0][seen[0].index("--forecast-history-dir") + 1] == str(history)


def test_the_stop_file_is_always_sent_when_given(tmp_path, monkeypatch):
    bridge, seen = _bridge(monkeypatch)
    path = str(tmp_path / "stop.request")
    bridge.generate("m.json", "i.csv", "P", tmp_path, ["f"], progress_dir=tmp_path,
                    stop_file=path)
    assert seen[0][seen[0].index("--stop-file") + 1] == path


def test_only_a_missing_stop_file_flag_is_an_unsupported_tool():
    exc = BridgeError("ut-agent failed: unrecognized arguments: --stop-file")
    assert pipeline._stop_file_unsupported(exc) is True
    assert pipeline._stop_file_unsupported(BridgeError("unrecognized arguments: --other")) is False


# --------------------------------------------------------------------------- #
# Frozen snapshot read
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("value", [0, 11, True, "2", 1.5])
def test_a_tampered_frozen_count_stops_the_batch(value):
    with pytest.raises(BridgeError, match="Invalid frozen budget_stop_observations"):
        pipeline.frozen_budget_stop({"budget_stop_observations": value})


# --------------------------------------------------------------------------- #
# Configuration and freezing
# --------------------------------------------------------------------------- #
def test_the_count_is_saved_and_frozen_into_the_batch(env):
    targets = _configure(env)
    status, body = env.call("put", "/config", json={"budget_stop_observations": 3})
    assert status == 200, body
    assert body["data"]["budget_stop_observations"] == 3

    _enqueue(env, targets["f"])
    assert env.bridge.budget_stop_observations == 3


def test_a_blank_setting_freezes_and_passes_the_default_count(env, app_ctx):
    from app.extensions import db
    from app.models.ut_pipeline import UtBatch, UtJob

    targets = _configure(env)
    job_id, = _enqueue(env, targets["f"])
    assert env.bridge.budget_stop_observations == 2
    with app_ctx.app_context():
        job = db.session.get(UtJob, job_id)
        batch = db.session.get(UtBatch, job.batch_id)
        assert batch.config_snapshot["budget_stop_observations"] == 2


@pytest.mark.parametrize("snapshot", [{}, {"budget_stop_observations": None}])
def test_old_snapshots_pass_the_default_count_to_the_cli(snapshot, tmp_path, monkeypatch):
    bridge, seen = _bridge(monkeypatch)
    bridge.generate("m.json", "i.csv", "P", tmp_path, ["f"], progress_dir=tmp_path,
                    budget_stop_observations=pipeline.frozen_budget_stop(snapshot))
    assert seen[0][seen[0].index("--budget-stop-observations") + 1] == "2"


def test_a_cleared_count_is_accepted_as_null(env):
    _configure(env)
    env.call("put", "/config", json={"budget_stop_observations": 2})
    status, body = env.call("put", "/config", json={"budget_stop_observations": ""})
    assert status == 200, body
    assert body["data"]["budget_stop_observations"] is None


@pytest.mark.parametrize("value", [0, 11, "2", True, 1.5])
def test_out_of_range_counts_are_rejected(env, value):
    _configure(env)
    status, _body = env.call("put", "/config", json={"budget_stop_observations": value})
    assert status == 400


def test_a_new_count_never_rewrites_a_queued_batch(env):
    targets = _configure(env)
    env.call("put", "/config", json={"budget_stop_observations": 2})
    status, batch = env.call("post", "/batches", json={"target_ids": [targets["f"]]})
    assert status == 202, batch
    first = batch["data"]["id"]

    status, _body = env.call("put", "/config", json={"budget_stop_observations": 4})
    assert status == 200
    from app.extensions import db
    from app.models.ut_pipeline import UtBatch
    with env.client.application.app_context():
        snapshot = db.session.get(UtBatch, first).config_snapshot or {}
    assert snapshot["budget_stop_observations"] == 2


# --------------------------------------------------------------------------- #
# The stop file
# --------------------------------------------------------------------------- #
def test_a_running_worker_is_told_to_stop_through_a_file(env):
    targets = _configure(env)
    job_ids = _enqueue(env, targets["f"])
    from app.extensions import db
    from app.models.ut_pipeline import UtJob
    with env.client.application.app_context():
        job = db.session.get(UtJob, job_ids[0])
        job.state = UtJob.GENERATING
        db.session.commit()
        assert pipeline._request_stop(job, 1) == 0
        path = pipeline._stop_file(job.project_id, job.batch_id)
        assert path.is_file()
        pipeline._halt(job)
        assert not path.exists()


def test_a_queued_job_is_stopped_without_a_file(env):
    targets = _configure(env)
    job_ids = _enqueue(env, targets["f"])
    from app.extensions import db
    from app.models.ut_pipeline import UtJob
    with env.client.application.app_context():
        job = db.session.get(UtJob, job_ids[0])
        job.state = UtJob.QUEUED
        job.stop_requested_at = None
        db.session.commit()
        path = pipeline._stop_file(job.project_id, job.batch_id)
        pipeline._clear_stop_file(path)
        assert pipeline._request_stop(job, 1) == 1
        assert not path.exists()


def test_generation_runs_against_the_batch_stop_file(env):
    targets = _configure(env)
    job_ids = _enqueue(env, targets["f"])
    job = _job(env, job_ids[0])
    assert env.bridge.stop_file is not None
    assert env.bridge.stop_file.endswith("stop.request")
    assert job["state"] != "blocked"


def test_an_older_tool_without_a_stop_file_blocks_the_job(env):
    targets = _configure(env)
    env.bridge.generate_error = "ut-agent failed: unrecognized arguments: --stop-file"
    job_ids = _enqueue(env, targets["f"])
    job = _job(env, job_ids[0])
    assert job["state"] == "blocked"
    assert job["gate_code"] == "ENV_BLOCKED"
    assert "--stop-file" in job["reason"]


def test_an_older_tool_without_forecast_stop_blocks_even_with_a_blank_setting(env):
    targets = _configure(env)
    env.bridge.generate_error = "ut-agent failed: unrecognized arguments: --budget-stop-observations 2"
    job_id, = _enqueue(env, targets["f"])
    job = _job(env, job_id)
    assert job["state"] == "blocked"
    assert job["gate_code"] == "ENV_BLOCKED"
    assert "--budget-stop-observations" in job["reason"]
