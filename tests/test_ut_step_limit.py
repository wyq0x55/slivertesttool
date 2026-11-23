"""The declared per-function semantic step ceiling: config, freeze, command line."""

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


def test_the_ceiling_is_sent_only_when_declared(tmp_path, monkeypatch):
    bridge, seen = _bridge(monkeypatch)
    bridge.generate("m.json", "i.csv", "P", tmp_path, ["f"], progress_dir=tmp_path)
    assert "--function-step-limit" not in seen[0]

    bridge.generate("m.json", "i.csv", "P", tmp_path, ["f"], progress_dir=tmp_path,
                    function_step_limit=1200)
    assert "--function-step-limit" in seen[1]
    assert seen[1][seen[1].index("--function-step-limit") + 1] == "1200"


@pytest.mark.parametrize("value", [0, -1, True, "5000", 1.5])
def test_the_ceiling_must_be_a_positive_integer(tmp_path, monkeypatch, value):
    bridge, seen = _bridge(monkeypatch)
    with pytest.raises(BridgeError, match="positive integer"):
        bridge.generate("m.json", "i.csv", "P", tmp_path, ["f"], progress_dir=tmp_path,
                        function_step_limit=value)
    assert seen == []


# --------------------------------------------------------------------------- #
# Frozen snapshot read
# --------------------------------------------------------------------------- #
def test_an_undeclared_ceiling_leaves_the_tool_default():
    assert pipeline.frozen_step_limit({}) is None
    assert pipeline.frozen_step_limit({"function_step_limit": None}) is None
    assert pipeline.frozen_step_limit({"function_step_limit": 250_000_000}) == 250_000_000


@pytest.mark.parametrize("value", [0, -7, True, "5000", 1.5])
def test_a_tampered_frozen_ceiling_stops_the_batch(value):
    with pytest.raises(BridgeError, match="Invalid frozen function_step_limit"):
        pipeline.frozen_step_limit({"function_step_limit": value})


def test_only_the_declared_ceiling_is_treated_as_an_unsupported_tool():
    exc = BridgeError("ut-agent failed: unrecognized arguments: --function-step-limit 5000")
    assert pipeline._step_limit_unsupported(exc, 5000) is True
    assert pipeline._step_limit_unsupported(exc, None) is False
    assert pipeline._step_limit_unsupported(BridgeError("unrecognized arguments: --other"), 5000) is False


# --------------------------------------------------------------------------- #
# Configuration, freezing and the launch
# --------------------------------------------------------------------------- #
def _batch_snapshot(env, batch_id):
    from app.extensions import db
    from app.models.ut_pipeline import UtBatch
    with env.client.application.app_context():
        return (db.session.get(UtBatch, batch_id).config_snapshot or {})


def test_a_declared_ceiling_is_frozen_into_the_batch(env):
    targets = _configure(env)
    status, body = env.call("put", "/config", json={"function_step_limit": 5000})
    assert status == 200, body
    assert body["data"]["function_step_limit"] == 5000

    _enqueue(env, targets["f"])
    assert env.bridge.step_limit == 5000


def test_an_undeclared_ceiling_stays_undeclared(env):
    targets = _configure(env)
    _enqueue(env, targets["f"])
    _, body = env.call("get", "/config")
    assert body["data"]["function_step_limit"] is None
    assert env.bridge.step_limit is None


def test_a_cleared_ceiling_is_accepted_as_null(env):
    _configure(env)
    env.call("put", "/config", json={"function_step_limit": 5000})
    status, body = env.call("put", "/config", json={"function_step_limit": None})
    assert status == 200, body
    assert body["data"]["function_step_limit"] is None
    status, body = env.call("put", "/config", json={"function_step_limit": ""})
    assert status == 200 and body["data"]["function_step_limit"] is None


@pytest.mark.parametrize("value", [0, 1_000_000_001, "5000", True, 1.5])
def test_out_of_range_ceilings_are_rejected(env, value):
    _configure(env)
    status, _body = env.call("put", "/config", json={"function_step_limit": value})
    assert status == 400


def test_a_new_ceiling_never_rewrites_a_queued_batch(env):
    targets = _configure(env)
    env.call("put", "/config", json={"function_step_limit": 5000})
    status, batch = env.call("post", "/batches", json={"target_ids": [targets["f"]]})
    assert status == 202, batch
    first = batch["data"]["id"]
    assert _batch_snapshot(env, first)["function_step_limit"] == 5000

    status, _body = env.call("put", "/config", json={"function_step_limit": 9000})
    assert status == 200
    assert _batch_snapshot(env, first)["function_step_limit"] == 5000

    status, second = env.call("post", "/batches", json={"target_ids": [targets["g"]]})
    assert status == 202, second
    assert _batch_snapshot(env, second["data"]["id"])["function_step_limit"] == 9000


def test_an_older_tool_is_reported_as_an_environment_block(env):
    targets = _configure(env)
    env.call("put", "/config", json={"function_step_limit": 5000})
    env.bridge.generate_error = "ut-agent failed: unrecognized arguments: --function-step-limit 5000"
    job_ids = _enqueue(env, targets["f"])
    job = _job(env, job_ids[0])
    assert job["state"] == "blocked"
    assert job["gate_code"] == "ENV_BLOCKED"
    assert "ut-agent" in job["reason"]


def test_an_older_tool_without_a_declared_ceiling_fails_generation(env):
    targets = _configure(env)
    env.bridge.generate_error = "ut-agent failed: unrecognized arguments: --function-step-limit 5000"
    job_ids = _enqueue(env, targets["f"])
    job = _job(env, job_ids[0])
    assert job["state"] == "error"
    assert job["gate_code"] == "GENERATION_FAILED"