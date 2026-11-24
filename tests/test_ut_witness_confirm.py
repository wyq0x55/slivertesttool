"""Server-side witness confirmation: host replay, ledger write, evidence limits.

Confirmation replays the proposal's candidates on the host toolchain the batch
froze.  It records what the harness observed, which is not target or delivery
evidence, and it never changes this batch's conclusion.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.services.ut_pipeline.bridge import BridgeError
from test_ut_pipeline import _configure, _enqueue, _job, env as workflow_env  # noqa: F401
from test_ut_witness_handoff import _frozen, _ready, _record, _report  # noqa: F401


def _candidates(env, manifest=None):
    """A ready job with a finished proposal, optionally bound to a manifest."""
    _configure(env)
    if manifest is not None:
        status, body = env.call("put", "/config", json={"manifest_path": str(manifest)})
        assert status == 200, body
    env.bridge.report = _report(("f", _record()), ("g", _record()))
    job_id = _enqueue(env, 1)[0]
    env.call("post", f"/jobs/{job_id}/witness/triage", json={})
    status, body = env.call("post", f"/jobs/{job_id}/witness/propose", json={})
    assert status == 202, body
    return job_id


def _manifest(tmp_path: Path, *, ledger: str | None = "witness-ledger.json",
              delivered: bool = False) -> Path:
    document = {"project_id": "P1", "delivered": delivered}
    if ledger is not None:
        document["witness_ledger"] = {"path": ledger}
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    return path


def test_confirm_needs_a_finished_proposal(workflow_env):
    env = workflow_env
    job_id = _ready(env)
    env.call("post", f"/jobs/{job_id}/witness/triage", json={})

    status, body = env.call("post", f"/jobs/{job_id}/witness/confirm", json={"write": False})

    assert status == 400 and body["error"]["code"] == "VALIDATION_ERROR"
    assert "候选" in body["error"]["message"]
    assert getattr(env.bridge, "confirm_arguments", None) is None


def test_confirm_runs_on_the_frozen_toolchain_and_records_host_evidence(workflow_env):
    env = workflow_env
    job_id = _candidates(env)

    status, body = env.call("post", f"/jobs/{job_id}/witness/confirm", json={"write": False})
    assert status == 202 and body["data"]["step"] == "confirm"

    arguments = env.bridge.confirm_arguments
    assert arguments["clang"] == "clang" and arguments["llvm_cov"] == "llvm-cov"
    assert arguments["llvm_profdata"] == "llvm-profdata"
    assert arguments["profile_runtime"] == "" and arguments["write"] is False
    # The wrapper applies UtAgentBridge.WITNESS_CONFIRM_TIMEOUT (120) when unset.
    assert arguments["timeout"] is None
    assert Path(arguments["candidates"]).name == "candidates.json"

    confirm = _frozen(env, job_id)["confirm"]
    assert confirm["status"] == "CONFIRMED" and confirm["confirmed"] == 1
    assert confirm["evidence_source"] == "HOST_EXECUTION"
    assert confirm["counts_as_target_evidence"] is False
    assert confirm["delivery_validated"] is False
    assert confirm["run"] == 1 and Path(confirm["out"]).name == "confirm"
    assert Path(confirm["out"]).parent.name == "run1"
    assert Path(confirm["confirm_report"]).is_file()
    assert len(confirm["report_sha256"]) == 64
    assert confirm["write"] is False and confirm["ledger_sha256"] is None
    assert confirm["actor_id"] == 1


def test_confirm_never_changes_the_job_or_the_candidates(workflow_env):
    env = workflow_env
    job_id = _candidates(env)
    before = _frozen(env, job_id)["proposal"]
    candidates = Path(before["candidates"]).read_bytes()

    env.call("post", f"/jobs/{job_id}/witness/confirm", json={"write": False})

    witness = _frozen(env, job_id)
    assert witness["proposal"] == before
    assert Path(before["candidates"]).read_bytes() == candidates
    job = _job(env, job_id)
    assert job["state"] == "awaiting_review"


def test_write_needs_a_declared_ledger(workflow_env, tmp_path):
    env = workflow_env
    job_id = _candidates(env, manifest=_manifest(tmp_path, ledger=None))

    status, body = env.call("post", f"/jobs/{job_id}/witness/confirm", json={"write": True})

    assert status == 400 and body["error"]["code"] == "VALIDATION_ERROR"
    assert "witness_ledger.path" in body["error"]["message"]
    assert getattr(env.bridge, "confirm_arguments", None) is None


def test_write_refuses_a_delivered_manifest(workflow_env, tmp_path):
    env = workflow_env
    job_id = _candidates(env, manifest=_manifest(tmp_path, delivered=True))

    status, body = env.call("post", f"/jobs/{job_id}/witness/confirm", json={"write": True})

    assert status == 400 and "台账已锁定" in body["error"]["message"]
    assert getattr(env.bridge, "confirm_arguments", None) is None


def test_write_merges_the_ledger_and_records_its_hash(workflow_env, tmp_path):
    env = workflow_env
    manifest = _manifest(tmp_path)
    job_id = _candidates(env, manifest=manifest)

    status, body = env.call("post", f"/jobs/{job_id}/witness/confirm", json={"write": True})
    assert status == 202, body

    confirm = _frozen(env, job_id)["confirm"]
    ledger = tmp_path / "witness-ledger.json"
    assert confirm["write"] is True and Path(confirm["ledger"]) == ledger
    assert confirm["previous_sha256"] is None
    assert ledger.is_file() and len(confirm["ledger_sha256"]) == 64


def test_writing_a_second_time_records_the_previous_ledger_hash(workflow_env, tmp_path):
    env = workflow_env
    manifest = _manifest(tmp_path)
    job_id = _candidates(env, manifest=manifest)
    env.call("post", f"/jobs/{job_id}/witness/confirm", json={"write": True})
    first = _frozen(env, job_id)["confirm"]["ledger_sha256"]

    env.call("post", f"/jobs/{job_id}/witness/confirm", json={"write": True})

    confirm = _frozen(env, job_id)["confirm"]
    assert confirm["previous_sha256"] == first
    assert Path(confirm["out"]).name == "confirm2"


def test_a_failed_confirm_is_recorded_instead_of_left_running(workflow_env):
    env = workflow_env
    job_id = _candidates(env)
    env.bridge.confirm_result = BridgeError("clang 不可用")

    status, body = env.call("post", f"/jobs/{job_id}/witness/confirm", json={"write": False})
    assert status == 202, body

    confirm = _frozen(env, job_id)["confirm"]
    assert confirm["status"] == "FAILED" and "clang 不可用" in confirm["error"]
    assert confirm["counts_as_target_evidence"] is False


def test_confirm_rejects_a_non_boolean_write(workflow_env):
    env = workflow_env
    job_id = _candidates(env)

    status, body = env.call("post", f"/jobs/{job_id}/witness/confirm", json={"write": "true"})

    assert status == 400 and body["error"]["code"] == "VALIDATION_ERROR"
    assert getattr(env.bridge, "confirm_arguments", None) is None