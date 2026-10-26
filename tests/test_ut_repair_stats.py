"""Per-model outcomes of the local model's repair rounds (dashboard card)."""

from __future__ import annotations

from test_ut_pipeline import (
    GAP,
    _configure,
    _enqueue,
    _llm_reply,
    _row,
)
from test_ut_pipeline import env as workflow_env

from app.services.ai import provider

env = workflow_env


def test_rounds_are_classified_by_what_they_produced(env, monkeypatch):
    targets = _configure(env, rounds=2)
    env.bridge.gaps = [GAP]
    reply = {}
    monkeypatch.setattr(provider, "chat", lambda *a, **k: reply["text"])

    reply["text"] = _llm_reply(_row("b1:T"))  # A witnessed row reaches LLVM execution.
    _enqueue(env, targets["f"])
    reply["text"] = _llm_reply()                                 # proposes nothing
    _enqueue(env, targets["g"])
    reply["text"] = _llm_reply(_row("b9:NOT_OPEN"))              # only refused rows
    _enqueue(env, targets["f"])

    status, body = env.call("get", "/repair-stats")
    assert status == 200
    [model] = body["data"]["models"]
    assert model["model"] == "qwen3.8-27b-gsq"
    assert (model["jobs"], model["rounds"], model["useful"]) == (3, 4, 1)
    assert (model["stalled"], model["rejected"], model["unusable"], model["guarded"]) == (1, 2, 0, 0)
    assert model["cache_ratio"] is None and model["avg_elapsed_ms"] is None


def test_a_project_without_model_rounds_reports_nothing(env):
    _configure(env)
    status, body = env.call("get", "/repair-stats")
    assert status == 200 and body["data"] == {"models": []}
