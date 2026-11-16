"""Candidate-bound review snapshots, expected-value lineage and honest gap counts."""

from __future__ import annotations

from copy import deepcopy
import hashlib
import io
import json
from types import SimpleNamespace
import zipfile

import pytest

from app.extensions import db
from app.models import AuditLog, UtAttempt, UtJob
from app.services.ut_pipeline import pipeline
from test_ut_export import _approve, _export
from test_ut_pipeline import GOOD, _configure, _enqueue, _job, env  # noqa: F401


def _entry(env):
    response = _export(env)
    assert response.status_code == 200
    archive = zipfile.ZipFile(io.BytesIO(response.data))
    return json.loads(archive.read("manifest.json"))["files"][0]


def test_integrity_override_snapshots_machine_before_review_mutation(env, app_ctx):
    targets = _configure(env)
    [job_id] = _enqueue(env, targets["f"])
    before = _job(env, job_id)
    with app_ctx.app_context():
        job = db.session.get(UtJob, job_id)
        attempt = db.session.get(UtAttempt, job.current_attempt_id)
        attempt.check = {**attempt.check, "candidate_sha256": "wrong-candidate"}
        db.session.commit()

    _approve(env, job_id, "Accept with integrity concern documented")
    after = _job(env, job_id)
    trace = after["review_trace"]
    assert after["last_verdict"] == "BLOCKED"
    assert trace["human_override"] is True
    assert trace["review_gate"]["code"] == "INTEGRITY_ERROR"
    machine = trace["machine"]
    assert machine["machine_verdict"] == machine["attempt_verdict"] == "PASS"
    assert machine["state"] == before["state"]
    assert machine["gate_code"] == before["gate_code"] == "MACHINE_PASS"
    assert machine["gate_reason"] == before["reason"]
    with app_ctx.app_context():
        log = AuditLog.query.filter_by(action="ut.job.approve", object_id=str(job_id)).one()
        assert log.old_value == machine
        assert log.new_value["review_trace"] == trace
        job = db.session.get(UtJob, job_id)
        job.gate_code, job.reason = "CHANGED", "Later mutable projection"
        attempt = db.session.get(UtAttempt, job.current_attempt_id)
        attempt.check = {}
        db.session.commit()
    entry = _entry(env)
    assert entry["gate_code"] == "MACHINE_PASS"
    assert entry["gate_reason"] == before["reason"]
    assert entry["machine_verdict"] == "PASS" and entry["post_review_verdict"] == "BLOCKED"
    assert entry["review_trace"] == trace


def test_gap_counts_keep_declines_unproven_and_count_other_gaps_and_outputs(env, app_ctx):
    targets = _configure(env)
    [job_id] = _enqueue(env, targets["f"])
    with app_ctx.app_context():
        job = db.session.get(UtJob, job_id)
        parent = db.session.get(UtAttempt, job.current_attempt_id)
        attempt = pipeline._new_attempt(
            job, UtAttempt.ORIGIN_LLM, GOOD, reviser={"claimed": ["b2:T"]})
        check = deepcopy(parent.check)
        check["completeness"] = {
            "details_complete": True,
            "unresolved_count": 6,
            "unresolved": [
                {"obligation_id": f"b{i}:T", "gap_class": "MISSING_CASE"} for i in range(1, 5)
            ] + [{"obligation_id": "evidence", "gap_class": "EVIDENCE_GAP"},
                 {"obligation_id": "configuration", "gap_class": "MISSING_CASE",
                  "requirement": {"configuration_status": "NEEDS_REVIEW"}}],
            "output_pending_count": 1,
            "output_pending": [{"obligation_id": "output"}],
        }
        check["candidate_witnesses"]["witnessed_obligation_ids"] = ["b1:T", "configuration"]
        attempt.check, attempt.execution, attempt.verdict = check, deepcopy(parent.execution), "PASS"
        pipeline._new_attempt(job, UtAttempt.ORIGIN_LLM, None,
                              reviser={"declined": ["b3:T"]}, make_current=False)
        job.state, job.gate_code, job.reason = "needs_human", "OPEN_GAPS_EXHAUSTED", "Unproven obligations"
        db.session.commit()
    _approve(env, job_id, "Accept remaining obligations explicitly")
    entry = _entry(env)
    assert entry["open_gap_count"] == 6
    assert entry["human_override"] is True
    counts = entry["gap_counts"]
    assert counts["status"] == "COMPLETE"
    assert (counts["missing"], counts["claimed"], counts["witnessed"],
            counts["declined"], counts["unproven"], counts["repair_open"]) == (4, 1, 1, 1, 3, 2)
    assert counts["remaining_supplied"] == 5 and counts["reported_output_pending"] == 1
    assert counts["configuration_conflicts"] == ["configuration"]
    assert entry["expected_value_source"] == "GENERATOR"
    assert [item["source"] for item in entry["expected_value_lineage"]] == ["GENERATOR"]


@pytest.mark.parametrize("completeness", [
    None,
    {"unresolved": [], "unresolved_count": 12, "details_complete": False},
    {"unresolved": [], "unresolved_count": "unknown", "output_pending": [], "output_pending_count": 0},
    {"unresolved": [], "unresolved_count": 0, "output_pending_count": 4},
])
def test_incomplete_reports_never_export_zero_open_gaps(env, app_ctx, completeness):
    targets = _configure(env)
    [job_id] = _enqueue(env, targets["f"])
    with app_ctx.app_context():
        job = db.session.get(UtJob, job_id)
        attempt = db.session.get(UtAttempt, job.current_attempt_id)
        attempt.check = {**attempt.check, "completeness": completeness}
        db.session.commit()
    _approve(env, job_id, "Review incomplete report explicitly")
    entry = _entry(env)
    assert entry["open_gap_count"] is None
    assert entry["gap_counts"]["status"] == "UNKNOWN"
    assert entry["gap_counts"]["details_complete"] is False
    assert entry["gap_counts"]["errors"]
    assert entry["review_trace"]["review_gate"]["code"] == "REPORT_INCOMPLETE"


def test_complete_report_can_export_known_zero_and_rejection_keeps_trace(env, app_ctx):
    targets = _configure(env)
    [job_id] = _enqueue(env, targets["f"])
    with app_ctx.app_context():
        job = db.session.get(UtJob, job_id)
        attempt = db.session.get(UtAttempt, job.current_attempt_id)
        check = deepcopy(attempt.check)
        check["completeness"].update(output_pending=[], output_pending_count=0)
        attempt.check = check
        db.session.commit()
    _approve(env, job_id)
    assert _entry(env)["open_gap_count"] == 0
    [rejected] = _enqueue(env, targets["g"])
    status, body = env.call("post", f"/jobs/{rejected}/review",
                           json={"decision": "reject", "note": "Needs review"})
    assert status == 200
    trace = body["data"]["review_trace"]
    assert trace["decision"] == "reject" and trace["human_override"] is False
    assert trace["machine"]["gate_code"] == "MACHINE_PASS"
    with app_ctx.app_context():
        log = AuditLog.query.filter_by(action="ut.job.reject", object_id=str(rejected)).one()
        assert log.old_value == trace["machine"]


def test_human_expected_values_keep_original_baseline_in_export(env):
    targets = _configure(env)
    [job_id] = _enqueue(env, targets["f"])
    replacement = GOOD.replace(b",0,1", b",0,9")
    status, body = env.call("post", f"/jobs/{job_id}/revision",
                           json={"csv": replacement.decode("cp932"), "note": "Reviewed expected output"})
    assert status == 202, body
    _approve(env, job_id)
    entry = _entry(env)
    assert entry["expected_value_source"] == "HUMAN"
    assert entry["baseline"]["csv_sha256"] == hashlib.sha256(GOOD).hexdigest()
    assert entry["csv_sha256"] == hashlib.sha256(replacement).hexdigest()
    assert [item["source"] for item in entry["expected_value_lineage"]] == ["GENERATOR", "HUMAN"]


def test_legacy_approval_is_not_presented_as_a_pre_review_snapshot(env, app_ctx):
    targets = _configure(env)
    [job_id] = _enqueue(env, targets["f"])
    _approve(env, job_id)
    with app_ctx.app_context():
        job = db.session.get(UtJob, job_id)
        generation = dict(job.generation)
        del generation["review_trace"]
        job.generation = generation
        db.session.commit()
    entry = _entry(env)
    assert entry["trace_status"] == "CURRENT_EVIDENCE_ONLY"
    assert entry["review_trace"] is None and entry["human_override"] is None
    assert entry["expected_value_source"] == "GENERATOR"


def _attempt(identifier, origin, parent=None, **metadata):
    return SimpleNamespace(id=identifier, job_id=1, seq=identifier, origin=origin,
                           csv_sha256=hashlib.sha256(str(identifier).encode()).hexdigest(),
                           reviser={"parent_attempt_id": parent, **metadata}, created_by=7)


@pytest.mark.parametrize("human", [False, True])
def test_expected_source_follows_typed_receipt_and_retains_generator_baseline(human):
    baseline = _attempt(1, "GENERATOR")
    parent = _attempt(2, "HUMAN" if human else "LLM", 1)
    derived = _attempt(3, "HOST_DERIVED", 2, oracle_source="IMPLEMENTATION_DERIVED",
                       from_sha256=parent.csv_sha256)
    derived.reviser["typed_candidate"] = {
        "contract": "ut-agent.host-typed-candidate/1", "expected_value_source": "IMPLEMENTATION_DERIVED",
        "source_candidate_sha256": parent.csv_sha256, "sha256": derived.csv_sha256,
    }
    attempts = {item.id: item for item in (baseline, parent, derived)}
    trace = pipeline._expected_value_trace(derived, attempts)
    assert trace["expected_value_source"] == "IMPLEMENTATION_DERIVED"
    assert trace["baseline"]["csv_sha256"] == baseline.csv_sha256
    sources = [item["source"] for item in trace["expected_value_lineage"]]
    assert sources == (["GENERATOR", "HUMAN", "IMPLEMENTATION_DERIVED"] if human
                       else ["GENERATOR", "IMPLEMENTATION_DERIVED"])
    retry = _attempt(4, "HOST_DERIVED", 3, host_retry_of=derived.id)
    retry.csv_sha256 = derived.csv_sha256
    attempts[retry.id] = retry
    retried = pipeline._expected_value_trace(retry, attempts)
    assert retried == trace
    retry.csv_sha256 = "changed"
    assert pipeline._expected_value_trace(retry, attempts)["expected_value_source"] == "UNKNOWN"
    trace = pipeline._expected_value_trace(parent, attempts)
    assert trace["expected_value_source"] == ("HUMAN" if human else "GENERATOR")
    derived.reviser["typed_candidate"]["source_candidate_sha256"] = "different"
    assert pipeline._expected_value_trace(derived, attempts)["expected_value_source"] == "UNKNOWN"


@pytest.mark.parametrize("case", ["missing", "generator-missing", "cycle", "foreign", "no-baseline", "untyped"])
def test_broken_lineage_does_not_invent_expected_provenance(case):
    baseline, child = _attempt(1, "GENERATOR"), _attempt(2, "LLM", 1)
    attempts = {1: baseline, 2: child}
    if case in ("missing", "generator-missing"):
        del attempts[1]
        if case == "generator-missing":
            child.origin = "GENERATOR"
    elif case == "cycle":
        child.reviser["parent_attempt_id"] = child.id
    elif case == "foreign":
        baseline.job_id = 99
    elif case == "no-baseline":
        baseline.origin = "LLM"
    else:
        child.origin = "HOST_DERIVED"
        child.reviser["oracle_source"] = "IMPLEMENTATION_DERIVED"
    trace = pipeline._expected_value_trace(child, attempts)
    assert trace["expected_value_source"] == "UNKNOWN"
    assert trace["expected_value_lineage_complete"] is False
    if case in ("missing", "generator-missing", "cycle", "foreign", "no-baseline"):
        assert trace["baseline"] is None
