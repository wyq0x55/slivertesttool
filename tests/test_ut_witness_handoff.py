"""Budget-exhaustion handoff: the frozen record, triage and proposal actions.

The observed states come from ut-agent's generation report; silver freezes them
once per generation attempt and offers the witness actions only where that record
names admissible obligations.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.services.ut_pipeline import handoff
from app.services.ut_pipeline.bridge import BridgeError
from test_ut_pipeline import _configure, _enqueue, _job, env as workflow_env  # noqa: F401


def _record(state="HANDOFF_READY", **overrides):
    if state == "NO_SUITE":
        record = {
            "state": state,
            "budget": {"budget_exhausted": None, "total": None, "completed": None,
                       "remaining": None, "completed_is_solved": False},
            "obligations": {"total": 0, "unresolved": 0, "by_cause": {}},
            "admissible": {"count": 0, "obligation_ids": [], "refused": [],
                           "unclassified": 0},
            "mcdc": {"total": 0, "pending_review": 0, "pending_pair_ids": []},
            "ledger_confirmed": False,
            "error": "GenerationBudgetExceeded: derive_obligations",
        }
    else:
        record = {
            "state": state,
            "budget": {"budget_exhausted": True, "total": 250000000, "completed": 210,
                       "remaining": 0, "completed_is_solved": False},
            "obligations": {"total": 12, "unresolved": 3,
                            "by_cause": {"BUDGET_EXHAUSTED": 3}},
            "admissible": {"count": 0 if state == "NO_ADMISSIBLE_OBLIGATION" else 3,
                           "obligation_ids": [] if state == "NO_ADMISSIBLE_OBLIGATION"
                           else ["a", "b", "c"],
                           "refused": [], "unclassified": 0},
            "mcdc": {"total": 4, "pending_review": 1, "pending_pair_ids": ["p1"]},
            "ledger_confirmed": False,
        }
    record.update(overrides)
    return record


def _report(*functions, **report):
    states, records = {}, {}
    for name, record in functions:
        records[name] = dict(record, function=name)
        states[record["state"]] = states.get(record["state"], 0) + 1
    document = {"run_status": "COMPLETED", "units": [], "expected_units": [],
                "triage_handoff": {"states": states, "functions": records}}
    document.update(report)
    return document


def _generated(env, target=1, report=None):
    """Configure, generate one function and return its job id."""
    _configure(env)
    env.bridge.report = report
    return _enqueue(env, target)[0]


def _frozen(env, job_id):
    return _job(env, job_id)["witness"]


def _ready(env):
    return _generated(env, report=_report(("f", _record()), ("g", _record())))


def test_a_completed_report_freezes_the_handoff(workflow_env):
    env = workflow_env
    job_id = _ready(env)

    witness = _frozen(env, job_id)
    assert witness["contract"] == "silver.ut-witness/1"
    record = witness["handoff"]
    assert record["state"] == "HANDOFF_READY"
    assert record["source"] == "index-generation-report.json"
    assert record["report_run_status"] == "COMPLETED"
    assert record["budget"]["budget_exhausted"] is True
    assert record["budget"]["completed"] == 210
    assert record["obligations"] == {"total": 12, "unresolved": 3,
                                     "by_cause": {"BUDGET_EXHAUSTED": 3}}
    assert record["admissible"]["obligation_ids"] == ["a", "b", "c"]
    assert record["mcdc"]["pending_review"] == 1
    assert len(record["report_sha256"]) == 64
    assert record["frozen_at"].endswith("+00:00")
    # Nothing is triaged or proposed until an operator asks for it.
    assert witness["triage"] is None and witness["proposal"] is None


def test_a_missing_report_is_unknown_rather_than_nothing_to_do(workflow_env):
    env = workflow_env
    record = _frozen(env, _generated(env))["handoff"]

    assert record["state"] == handoff.UNKNOWN
    assert "index-generation-report.json" in record["reason"]
    assert record["budget"]["total"] is None
    assert record["admissible"]["count"] is None


def test_an_unfinished_report_is_unknown(workflow_env):
    env = workflow_env
    report = _report(("f", _record()), run_status="FAILED", error="boom")
    record = _frozen(env, _generated(env, report=report))["handoff"]

    assert record["state"] == handoff.UNKNOWN
    assert "run_status=FAILED" in record["reason"] and "boom" in record["reason"]


def test_a_function_the_report_omits_is_unknown(workflow_env):
    env = workflow_env
    record = _frozen(env, _generated(env, report=_report(("g", _record()))))["handoff"]

    assert record["state"] == handoff.UNKNOWN
    assert "未包含该函数" in record["reason"]


def test_an_unrecognised_state_is_unknown(workflow_env):
    env = workflow_env
    report = _report(("f", _record(state="SOMETHING_NEW")))
    record = _frozen(env, _generated(env, report=report))["handoff"]

    assert record["state"] == handoff.UNKNOWN
    assert "SOMETHING_NEW" in record["reason"]


def test_no_suite_is_kept_separate_without_blocking_the_job(workflow_env):
    env = workflow_env
    report = _report(("f", _record("NO_SUITE")), ("g", _record()))
    job_id = _generated(env, target=1, report=report)

    witness = _frozen(env, job_id)
    assert witness["handoff"]["state"] == "NO_SUITE"
    assert "GenerationBudgetExceeded" in witness["handoff"]["error"]
    # The completed cases still reach review; the handoff is an observation.
    assert _job(env, job_id)["state"] == "awaiting_review"


def test_freezing_does_not_touch_the_generation_directory(workflow_env):
    env = workflow_env
    job_id = _ready(env)
    generation = Path(env.bridge.progress_dir).parent / "generation"
    report_path = generation / "index-generation-report.json"
    before = (sorted(p.name for p in generation.iterdir()), report_path.read_bytes())

    from app.models.ut_pipeline import UtJob

    with env.client.application.app_context():
        stored = UtJob.query.filter_by(id=job_id).one()
        frozen = handoff.freeze(stored, generation)

    after = (sorted(p.name for p in generation.iterdir()), report_path.read_bytes())
    assert before == after
    assert frozen["handoff"]["state"] == _frozen(env, job_id)["handoff"]["state"]


def test_triage_needs_a_handoff_ready_function(workflow_env):
    env = workflow_env
    report = _report(("f", _record("NO_ADMISSIBLE_OBLIGATION")))
    job_id = _generated(env, report=report)

    status, body = env.call("post", f"/jobs/{job_id}/witness/triage", json={})
    assert status == 400
    assert "没有可提议的项" in body["error"]["message"]
    assert _frozen(env, job_id)["triage"] is None


def test_no_suite_refuses_triage_and_says_what_is_missing(workflow_env):
    env = workflow_env
    job_id = _generated(env, report=_report(("f", _record("NO_SUITE"))))

    status, body = env.call("post", f"/jobs/{job_id}/witness/triage", json={})
    assert status == 400
    assert "缺少分拣输入" in body["error"]["message"]


def test_unreadable_handoff_refuses_triage(workflow_env):
    env = workflow_env
    job_id = _generated(env)

    status, body = env.call("post", f"/jobs/{job_id}/witness/triage", json={})
    assert status == 400 and "分拣记录不可用" in body["error"]["message"]


def test_triage_records_a_receipt_and_leaves_the_job_alone(workflow_env):
    env = workflow_env
    job_id = _ready(env)

    status, body = env.call("post", f"/jobs/{job_id}/witness/triage", json={})
    assert status == 202, body
    assert body["data"] == {"status": "QUEUED", "step": "triage"}

    witness = _frozen(env, job_id)
    triage = witness["triage"]
    assert triage["status"] == "PROPOSABLE" and triage["run"] == 1
    assert triage["proposable"] == 2 and triage["excluded"] == 1
    assert triage["excluded_by_cause"] == {"STALE": 1}
    assert len(triage["triage_sha256"]) == 64
    out = Path(triage["out"])
    assert out.name == "triage" and out.parent.name == "run1" and out.is_dir()
    assert witness["handoff"]["state"] == "HANDOFF_READY"
    job = _job(env, job_id)
    assert job["state"] == "awaiting_review" and job["last_verdict"] == "PASS"
    # The tool received the stored generation inputs, not copies.
    assert env.bridge.triage_arguments["ir"].endswith("-ir.json")
    assert env.bridge.triage_arguments["suite"].endswith("-intents.json")


def test_each_triage_run_gets_its_own_directory(workflow_env):
    env = workflow_env
    job_id = _ready(env)

    env.call("post", f"/jobs/{job_id}/witness/triage", json={})
    first = Path(_frozen(env, job_id)["triage"]["out"])
    _, body = env.call("post", f"/jobs/{job_id}/witness/triage", json={})

    assert body["data"]["status"] == "QUEUED"
    second = Path(_frozen(env, job_id)["triage"]["out"])
    assert _frozen(env, job_id)["triage"]["run"] == 2
    assert first != second and first.is_dir() and second.is_dir()
    assert (first / "triage.json").is_file()


def test_a_failed_triage_is_recorded_instead_of_left_running(workflow_env):
    env = workflow_env
    job_id = _ready(env)
    env.bridge.triage_result = BridgeError(
        "ut-agent witness triage 失败（exit=2）：suite 与函数不一致")

    status, body = env.call("post", f"/jobs/{job_id}/witness/triage", json={})
    assert status == 202, body

    triage = _frozen(env, job_id)["triage"]
    assert triage["status"] == "FAILED"
    assert "suite 与函数不一致" in triage["error"]
    assert _job(env, job_id)["witness"]["triage"]["status"] == "FAILED"


def test_a_batch_never_proposes_on_its_own(workflow_env):
    env = workflow_env
    _ready(env)

    assert getattr(env.bridge, "propose_arguments", None) is None
    assert getattr(env.bridge, "triage_arguments", None) is None


def test_propose_needs_a_proposable_triage(workflow_env):
    env = workflow_env
    job_id = _ready(env)

    status, body = env.call("post", f"/jobs/{job_id}/witness/propose", json={})
    assert status == 400
    assert "还没有可提议的分拣结果" in body["error"]["message"]
    assert _frozen(env, job_id)["proposal"] is None


def test_propose_refuses_a_model_outside_the_loopback(workflow_env):
    env = workflow_env
    job_id = _ready(env)
    env.call("post", f"/jobs/{job_id}/witness/triage", json={})

    from app.services.ai import config as ai_config

    with env.client.application.app_context():
        ai_config.update_ai_config({"ai_api_base": "http://10.0.0.5:8000/v1"})

    status, body = env.call("post", f"/jobs/{job_id}/witness/propose", json={})
    assert status == 400 and "loopback" in body["error"]["message"]
    assert getattr(env.bridge, "propose_arguments", None) is None


def test_propose_records_candidates_and_the_operator(workflow_env):
    env = workflow_env
    job_id = _ready(env)
    env.call("post", f"/jobs/{job_id}/witness/triage", json={})

    status, body = env.call("post", f"/jobs/{job_id}/witness/propose", json={})
    assert status == 202 and body["data"]["step"] == "propose"

    witness = _frozen(env, job_id)
    proposal = witness["proposal"]
    assert proposal["status"] == "PROPOSABLE" and proposal["source"] == "PROPOSE"
    assert proposal["proposable"] == 2 and proposal["model"] == "qwen3.8-27b-gsq"
    assert proposal["run"] == 1 and proposal["actor_id"] == 1
    assert Path(proposal["candidates"]).name == "candidates.json"
    assert Path(proposal["candidates"]).is_file()
    assert len(proposal["candidates_sha256"]) == 64
    assert witness["triage"]["status"] == "PROPOSABLE"
    # A proposal is a candidate only: the job keeps its review state.
    assert _job(env, job_id)["state"] == "awaiting_review"


def test_a_superseded_job_offers_no_handoff_action(workflow_env):
    env = workflow_env
    job_id = _ready(env)
    # A second job for the same function supersedes the first one.
    _enqueue(env, 1)

    assert _job(env, job_id)["superseded_by"] is not None
    status, body = env.call("post", f"/jobs/{job_id}/witness/triage", json={})
    assert status == 400 and body["error"]["code"] == "SUPERSEDED"