"""Pure Host append/typed-oracle lifecycle; no database, model or engine."""

import csv
import hashlib
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from app.models import UtAttempt, UtJob
from app.models.ut_pipeline import GateCode
from app.services.ai.provider import ProviderError
from app.services.lanmatrix.errors import ServiceError, VersionConflict
from app.services.ut_pipeline import host_candidate, pipeline, repair
from app.services.ut_pipeline.bridge import BridgeSettings, UtAgentBridge

REFERENCE = b'mod,"a.c/f","f",1,1\r\n#COMMENT,"a.c/x","return"\r\n,0,0\r\n'
SOURCE = REFERENCE + b';$L$,LLM add A\r\n,5,\r\n'
DERIVED = SOURCE.replace(b',5,\r\n', b',5,1\r\n')
GAP = {"obligation_id": "A", "gap_class": "MISSING_CASE"}


def sha(data):
    return hashlib.sha256(data).hexdigest()


def artifact(tmp_path, source=SOURCE, data=DERIVED):
    sandbox = tmp_path / "execution" / "sandbox"
    sandbox.mkdir(parents=True, exist_ok=True)
    path = sandbox / "typed.csv"
    path.write_bytes(data)
    return {"status": "HOST_PASS", "evidence_source": "HOST_EXECUTION", "counts_as_target_evidence": False,
            "sandbox": str(sandbox), "typed_candidate": {
        "contract": "ut-agent.host-typed-candidate/1", "path": str(path), "sha256": sha(data),
        "source_candidate_sha256": sha(source), "expected_value_source": "IMPLEMENTATION_DERIVED",
        "reference_rows": 1}}


def test_typed_artifact_accepts_only_appended_blank_outputs(tmp_path):
    execution = artifact(tmp_path)
    data, receipt = host_candidate.read_typed_candidate(execution, SOURCE, REFERENCE, tmp_path / "execution")
    assert data == DERIVED and receipt == execution["typed_candidate"]


@pytest.mark.parametrize("case", ["contract", "source-sha", "sha", "source-kind", "outside", "sandbox",
                                   "relative", "bool-count", "count", "original", "input", "rows", "columns",
                                   "malformed", "unclosed", "unchanged", "nonblank", "failed-execution",
                                   "foreign-evidence", "target-flag"])
def test_untrusted_typed_artifact_is_rejected(tmp_path, case):
    source, data = SOURCE, DERIVED
    if case == "original":
        data = DERIVED.replace(b',0,0', b',0,9')
    elif case == "input":
        data = DERIVED.replace(b',5,1', b',6,1')
    elif case == "rows":
        data += b',9,1\r\n'
    elif case == "columns":
        data = DERIVED.replace(b'a.c/x', b'a.c/y')
    elif case == "malformed":
        data += b',7\r\n'
    elif case == "unclosed":
        data += b',"7,1\r\n'
    elif case == "unchanged":
        data = SOURCE
    elif case == "nonblank":
        source = SOURCE.replace(b',5,\r\n', b',5,2\r\n')
    execution = artifact(tmp_path, source, data)
    receipt = execution["typed_candidate"]
    if case == "contract":
        receipt["contract"] = "unknown"
    elif case == "failed-execution":
        execution["status"] = "HOST_MISMATCH"
    elif case == "foreign-evidence":
        execution["evidence_source"] = "WINAMS_EXECUTION"
    elif case == "target-flag":
        execution["counts_as_target_evidence"] = True
    elif case == "source-sha":
        receipt["source_candidate_sha256"] = "other"
    elif case == "sha":
        receipt["sha256"] = "other"
    elif case == "source-kind":
        receipt["expected_value_source"] = "MODEL"
    elif case == "outside":
        outside = tmp_path / "outside.csv"
        outside.write_bytes(data)
        receipt["path"] = str(outside)
    elif case == "sandbox":
        execution["sandbox"] = str(tmp_path)
    elif case == "relative":
        receipt["path"] = "typed.csv"
    elif case in {"bool-count", "count"}:
        receipt["reference_rows"] = True if case == "bool-count" else 0
    with pytest.raises((ValueError, csv.Error)):
        host_candidate.read_typed_candidate(execution, source, REFERENCE, tmp_path / "execution")


def checked(data, *, witnessed=False, verdict="PASS"):
    return {"verdict": verdict, "executed": True, "issues": [], "candidate_sha256": sha(data),
            "evidence_source": "HOST_EXECUTION", "counts_as_target_evidence": False, "delivery_validated": False,
            "completeness": {"unresolved": [deepcopy(GAP)], "unresolved_count": 1},
            "candidate_witnesses": {"contract": "ut-agent.candidate-witness/1", "candidate_sha256": sha(data),
                "input_mapping_identified": True, "evidence": {"identified": True},
                "input_repair_readiness": {"ready": True, "code": "READY", "reason": "", "blocked_columns": []},
                "witnessed_obligation_ids": ["A"] if witnessed else []},
            "input_ranges": {"contract": {"contract": "ut-agent.input-domains/1", "columns": [
                {"index": 1, "column": "a.c/x", "status": "PROVEN", "min": 0, "max": 255}]}}}


@pytest.fixture()
def lifecycle(tmp_path, monkeypatch):
    from test_ut_evidence_gates import _job

    job = _job()
    job.state, job.stop_requested_at = UtJob.EXECUTING, None
    job.to_dict = lambda: {"current_attempt_id": job.current_attempt_id}
    reference = tmp_path / "reference.csv"
    reference.write_bytes(REFERENCE)
    job.reference_csv = str(reference)
    job.target.to_dict = lambda: {"function": "f", "source_rel": "a.c"}
    config = {"execution_backend": "clang", "manifest_path": "manifest.json", "stub_mode": "llm",
              "stub_model": "stub-model", "stub_max_rounds": 2, "repair_model": "case-model"}
    batch = SimpleNamespace(config_snapshot=config, constraints={})
    attempts = {}

    def new_attempt(job, origin, data, *, reviser=None, make_current=True, **kwargs):
        ident = 10 + len(attempts)
        path = tmp_path / f"{ident}.csv"
        if data is not None:
            path.write_bytes(data)
        attempt = SimpleNamespace(id=ident, seq=len(attempts) + 1, origin=origin,
            csv_path=str(path) if data is not None else "", csv_sha256=sha(data) if data is not None else "",
            check=None, execution=None, verdict="", note=kwargs.get("note", ""),
            reviser={**(reviser or {}), "parent_attempt_id": job.current_attempt_id})
        attempts[ident] = attempt
        if make_current and data is not None:
            job.current_attempt_id = ident
        return attempt

    new_attempt(job, UtAttempt.ORIGIN_GENERATOR, REFERENCE)
    history = SimpleNamespace(filter_by=lambda **kw: SimpleNamespace(order_by=lambda *args: list(attempts.values())))
    session = SimpleNamespace(get=lambda model, ident: batch if ident == 2 else attempts[ident],
                              query=Mock(), commit=Mock(), refresh=Mock(), is_active=True)
    monkeypatch.setattr(pipeline, "db", SimpleNamespace(session=session))
    monkeypatch.setattr(pipeline.timing, "db", SimpleNamespace(session=session))
    monkeypatch.setattr(pipeline, "UtAttempt", SimpleNamespace(**{name: getattr(UtAttempt, name) for name in (
        "ORIGIN_GENERATOR", "ORIGIN_LLM", "ORIGIN_HUMAN", "ORIGIN_HOST_DERIVED")},
        query=history, seq=object()))
    monkeypatch.setattr(pipeline, "_new_attempt", new_attempt)
    monkeypatch.setattr(pipeline, "_job_dir", lambda job: tmp_path)
    monkeypatch.setattr(pipeline, "_fresh_exec_dir", lambda job, a: tmp_path / f"exec-{a.seq}")
    monkeypatch.setattr(pipeline, "_gate", lambda job, state, reason="", code="": (
        setattr(job, "state", state), setattr(job, "reason", reason), setattr(job, "gate_code", code)))
    monkeypatch.setattr(pipeline, "current_app", SimpleNamespace(config={"UT_REPAIR_TIMEOUT": 1}))
    monkeypatch.setattr(pipeline, "_locked_job", lambda *args: job)
    monkeypatch.setattr(pipeline.repair.provider, "invoke_chat", Mock(side_effect=AssertionError("No model calls")))

    def gaps(job, attempt, **kwargs):
        meta = [a.reviser for a in attempts.values() if a.origin == UtAttempt.ORIGIN_LLM]
        return pipeline.gap_status(attempt.check or {}, meta, attempt.csv_sha256, attempt.execution)

    monkeypatch.setattr(pipeline, "_job_gaps", gaps)
    bridge = SimpleNamespace(check=Mock(side_effect=lambda path, **kwargs: checked(
        Path(path).read_bytes(), witnessed=True, verdict="FORMAT_OK")),
        execute=Mock(side_effect=AssertionError("host.execute is isolated in this unit test")))
    monkeypatch.setattr(pipeline, "get_bridge", lambda: bridge)

    def execute(**kwargs):
        data = Path(kwargs["candidate"]).read_bytes()
        execution = {"status": "HOST_PASS", "evidence_source": "HOST_EXECUTION",
                     "counts_as_target_evidence": False, "delivery_validated": False,
                     "input_binding": {"candidate": {"sha256": sha(data)}}}
        if data == SOURCE:
            execution.update(artifact(kwargs["out"], data))
        return execution, checked(data, witnessed=data != REFERENCE)

    monkeypatch.setattr(pipeline.host, "execute", execute)
    monkeypatch.setattr(pipeline.repair, "prompt_prefix", lambda *args: {"version": repair.PROMPT_VERSION})
    revision = Mock(return_value=repair.Revision(SOURCE, {"rows": [{"obligation": "A"}], "claimed": ["A"]}))
    monkeypatch.setattr(pipeline.repair, "revise", revision)
    return SimpleNamespace(job=job, attempts=attempts, bridge=bridge, revision=revision, config=config,
                           new_attempt=new_attempt, session=session)


def test_host_append_derive_reexecute_then_review(lifecycle):
    ctx, job = lifecycle, lifecycle.job
    pipeline._execute_step(job)
    assert job.state == UtJob.REPAIRING and job.repair_rounds == 0
    pipeline._repair_step(job)
    assert job.state == UtJob.EXECUTING and job.repair_rounds == 1
    assert ctx.revision.call_args.kwargs["model"] == "case-model"
    assert ctx.revision.call_args.kwargs["allowed"] == {"A"}
    assert ctx.bridge.check.call_args.kwargs.get("execution") is None
    appended_id = job.current_attempt_id
    pipeline._execute_step(job)
    child = ctx.attempts[job.current_attempt_id]
    assert child.origin == UtAttempt.ORIGIN_HOST_DERIVED and job.state == UtJob.EXECUTING
    assert child.reviser["oracle_source"] == "IMPLEMENTATION_DERIVED"
    assert child.reviser["parent_attempt_id"] == appended_id
    assert child.csv_sha256 == sha(DERIVED) and child.check is None
    assert job.last_verdict == "" and job.repair_rounds == 1
    with pytest.raises(VersionConflict):
        pipeline.review(4, 1, "approve", 7, "note", expected_attempt_id=appended_id)
    with pytest.raises(ServiceError, match="require execution"):
        pipeline.review(4, 1, "approve", 7, "note", expected_attempt_id=child.id)
    pipeline._execute_step(job)
    assert job.state == UtJob.AWAITING_REVIEW and child.check["candidate_sha256"] == sha(DERIVED)
    assert child.check["counts_as_target_evidence"] is False and child.check["delivery_validated"] is False
    assert ctx.attempts[appended_id].csv_sha256 == sha(SOURCE)
    assert not ctx.bridge.execute.called


@pytest.mark.parametrize("case", ["unknown", "unbound", "coverage", "coverage-unknown", "environment", "exhausted", "human"])
def test_host_cases_without_repair_authority_stay_human(lifecycle, case):
    ctx, job = lifecycle, lifecycle.job
    attempt = ctx.attempts[job.current_attempt_id]
    attempt.check, attempt.execution = checked(REFERENCE), {"status": "HOST_PASS"}
    if case == "unknown":
        attempt.check["candidate_witnesses"]["evidence"]["identified"] = False
    elif case == "unbound":
        attempt.check["candidate_witnesses"]["input_mapping_identified"] = False
    elif case == "coverage":
        attempt.check.update(verdict="FAIL", issues=[{"code": "HOST_COVERAGE_UNAVAILABLE", "severity": "error"}])
    elif case == "coverage-unknown":
        attempt.check["issues"] = [{"code": "HOST_COVERAGE_REQUIRED_UNKNOWN", "severity": "warning"}]
    elif case == "environment":
        attempt.execution["status"] = "HOST_UNSUPPORTED"
        attempt.check["verdict"] = "BLOCKED"
    elif case == "exhausted":
        job.repair_rounds = job.max_repair_rounds
    else:
        attempt.origin = UtAttempt.ORIGIN_HUMAN
    pipeline._repair_step(job)
    assert job.state == UtJob.NEEDS_HUMAN and not ctx.revision.called


def test_declining_required_cases_keeps_them_unproven(lifecycle):
    ctx, job = lifecycle, lifecycle.job
    pipeline._execute_step(job)
    parent = ctx.attempts[job.current_attempt_id]
    ctx.revision.return_value = repair.Revision(
        None, {"rows": [], "declined": ["A"], "error": "No proven input mapping"})
    pipeline._repair_step(job)
    gaps = pipeline._job_gaps(job, parent)
    assert gaps["open"] == [] and gaps["declined"] == 1
    assert gaps["unproven"] == 1
    assert job.state == UtJob.NEEDS_HUMAN and job.gate_code == GateCode.CHECK_BLOCKED
    assert job.current_attempt_id == parent.id
    assert parent.check["completeness"]["unresolved_count"] == 1


@pytest.mark.parametrize("readiness", [
    None,
    {"ready": False, "code": "STUB_SLOT_UNBOUND", "reason": "Inherited slot has no typed binding",
     "blocked_columns": ["stub@return[0]"]},
])
def test_unproven_input_repair_capability_stops_before_model(lifecycle, readiness):
    ctx, job = lifecycle, lifecycle.job
    parent = ctx.attempts[job.current_attempt_id]
    parent.check, parent.execution = checked(REFERENCE), {"status": "HOST_PASS"}
    witnesses = parent.check["candidate_witnesses"]
    if readiness is None:
        witnesses.pop("input_repair_readiness")
    else:
        witnesses["input_repair_readiness"] = readiness
    pipeline._repair_step(job)
    assert job.state == UtJob.NEEDS_HUMAN and job.gate_code == GateCode.CHECK_BLOCKED
    assert job.repair_rounds == 0 and not ctx.revision.called
    assert len(ctx.attempts) == 1
    if readiness:
        assert readiness["reason"] in job.reason


def test_required_case_error_with_proven_repair_capability_can_repair(lifecycle):
    ctx, job = lifecycle, lifecycle.job
    parent = ctx.attempts[job.current_attempt_id]
    parent.check, parent.execution = checked(REFERENCE, verdict="FAIL"), {"status": "HOST_PASS"}
    parent.check["issues"] = [{"code": "INPUT_OBLIGATION_UNRESOLVED", "severity": "error",
                               "repairable": True}]
    state, _, _ = pipeline._host_next_state(job, parent, pipeline._job_gaps(job, parent))
    assert state == UtJob.REPAIRING


def test_human_pass_does_not_discharge_required_unproven_cases(lifecycle):
    ctx, job = lifecycle, lifecycle.job
    parent = ctx.attempts[job.current_attempt_id]
    parent.check, parent.execution = checked(REFERENCE), {"status": "HOST_PASS"}
    parent.origin = UtAttempt.ORIGIN_HUMAN
    gaps = pipeline._job_gaps(job, parent)
    assert pipeline._host_next_state(job, parent, gaps)[0] == UtJob.NEEDS_HUMAN
    assert pipeline.next_state(parent.check, parent.origin, 0, 5, gaps)[0] == UtJob.NEEDS_HUMAN


def test_model_history_retains_bounded_validation_feedback():
    attempt = SimpleNamespace(seq=2, origin=UtAttempt.ORIGIN_LLM, verdict="INVALID", check={},
                              reviser={"error": "declined must be a list",
                                       "reply_head": "x" * 3000})
    entry = pipeline._history_item(attempt)
    assert entry["error"] == "declined must be a list"
    assert entry["reply_head"] == "x" * 2000
    assert entry["added"] == 0 and entry["codes"] == []


def test_mixed_proof_gaps_allow_only_missing_case_work_and_still_block_review(lifecycle):
    ctx, job = lifecycle, lifecycle.job
    attempt = ctx.attempts[job.current_attempt_id]
    attempt.execution = {"status": "HOST_PASS", "evidence_source": "HOST_EXECUTION"}
    attempt.check = checked(REFERENCE, verdict="FAIL")
    proof_gap = {"obligation_id": "B", "gap_class": "SEMANTIC_EVIDENCE"}
    attempt.check["completeness"] = {"unresolved": [deepcopy(GAP), proof_gap], "unresolved_count": 2}
    proof_issue = {"code": "INPUT_OBLIGATION_PROOF_GAP", "severity": "error",
                   "repairable": False, "message": "B needs typed evidence"}
    attempt.check["issues"] = [proof_issue, {
        "code": "INPUT_OBLIGATION_UNRESOLVED", "severity": "error", "repairable": True}]
    gaps = pipeline._host_gaps(job, attempt)
    assert [gap["obligation_id"] for gap in gaps["open"]] == ["A"]
    assert pipeline._host_next_state(job, attempt, gaps)[0] == UtJob.REPAIRING
    attempt.check["candidate_witnesses"]["witnessed_obligation_ids"] = ["A"]
    attempt.check["issues"] = [proof_issue]
    gaps = pipeline._host_gaps(job, attempt)
    assert gaps["open"] == [] and gaps["unproven"] == 0
    state, reason, code = pipeline._host_next_state(job, attempt, gaps)
    assert state == UtJob.NEEDS_HUMAN and code == GateCode.CHECK_BLOCKED
    assert "B needs typed evidence" in reason
    assert not ctx.revision.called


def test_retry_keeps_failed_request_diagnostics(lifecycle):
    ctx, job = lifecycle, lifecycle.job
    parent = ctx.attempts[job.current_attempt_id]
    first = pipeline._fresh_model_dir(job, parent)
    first.mkdir(parents=True)
    request = first / "request-messages.json"
    request.write_bytes(b'{"failed":"request"}')
    second = pipeline._fresh_model_dir(job, parent)
    assert first != second and second.parent == first.parent
    assert not second.exists() and request.read_bytes() == b'{"failed":"request"}'


@pytest.mark.parametrize("contacted", [False, True])
def test_diagnostic_failure_blocks_without_miscounting_model_rounds(lifecycle, contacted):
    ctx, job = lifecycle, lifecycle.job
    pipeline._execute_step(job)
    original = job.current_attempt_id
    ctx.revision.return_value = repair.Revision(
        None, {"code": "DIAGNOSTIC_WRITE_FAILED", "error": "Cannot retain exchange",
               "model_contacted": contacted})
    pipeline._repair_step(job)
    assert job.state == UtJob.NEEDS_HUMAN and job.gate_code == "DIAGNOSTIC_WRITE_FAILED"
    assert job.repair_rounds == int(contacted) and job.current_attempt_id == original


@pytest.mark.parametrize("other_code", ["HOST_POLICY_NOT_BOUND", "HOST_CSV_NOT_BOUND",
                                       "ABI_POINTER_SIZE_MISMATCH", None])
@pytest.mark.parametrize("environment_code", ["HOST_TOOL_UNAVAILABLE", "HOST_PROFILE_RUNTIME_UNAVAILABLE"])
def test_missing_tool_does_not_make_semantic_or_binding_failures_retryable(lifecycle, other_code, environment_code):
    ctx, job = lifecycle, lifecycle.job
    attempt = ctx.attempts[job.current_attempt_id]
    attempt.execution = {"status": "HOST_UNSUPPORTED"}
    attempt.check = checked(REFERENCE, verdict="BLOCKED")
    attempt.check["issues"] = [{"code": environment_code, "severity": "error",
                                "message": "tool unavailable: clang"}]
    if other_code:
        attempt.check["issues"].append({"code": other_code, "severity": "error"})
    state, reason, code = pipeline._host_next_state(job, attempt, pipeline._host_gaps(job, attempt))
    assert state == (UtJob.NEEDS_HUMAN if other_code else UtJob.BLOCKED)
    assert code == (GateCode.CHECK_BLOCKED if other_code else GateCode.ENV_BLOCKED)
    if not other_code:
        assert "tool unavailable: clang" in reason
    assert not ctx.revision.called


def test_case_model_outage_retries_repair_without_stub_renewal(lifecycle):
    ctx, job = lifecycle, lifecycle.job
    pipeline._execute_step(job)
    ctx.revision.side_effect = ProviderError("offline")
    pipeline._repair_step(job)
    assert job.state == UtJob.ERROR and job.repair_rounds == 0
    assert job.generation["host_step"] == "repair"
    old = job.current_attempt_id
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(pipeline.audit, "record", Mock())
        pipeline.retry(4, 1)
    assert job.current_attempt_id == old and len(ctx.attempts) == 1
    assert job.state == UtJob.REPAIRING and job.repair_rounds == 0


def test_stub_outage_retry_renews_attempt_and_executes_host(lifecycle, monkeypatch):
    ctx, job = lifecycle, lifecycle.job
    job.state, job.gate_code = UtJob.ERROR, GateCode.MODEL_UNAVAILABLE
    job.generation["host_step"] = "execute"
    old = job.current_attempt_id
    monkeypatch.setattr(pipeline.audit, "record", Mock())
    pipeline.retry(4, 1)
    assert job.state == UtJob.EXECUTING and job.current_attempt_id != old
    assert len(ctx.attempts) == 2 and job.repair_rounds == 0
    assert ctx.attempts[job.current_attempt_id].reviser["host_retry_of"] == old


def test_static_witness_rejection_does_not_replace_current_host_candidate(lifecycle):
    ctx, job = lifecycle, lifecycle.job
    pipeline._execute_step(job)
    old = job.current_attempt_id
    ctx.bridge.check.side_effect = lambda path, **kw: checked(Path(path).read_bytes(), verdict="BLOCKED")
    pipeline._repair_step(job)
    assert job.current_attempt_id == old and job.gate_code == GateCode.CANDIDATE_REJECTED
    assert job.repair_rounds == 1 and len(ctx.attempts) == 2
    assert ctx.attempts[old + 1].verdict == "BLOCKED" and not ctx.bridge.execute.called


def test_static_claim_without_bound_witness_is_rejected(lifecycle):
    ctx, job = lifecycle, lifecycle.job
    pipeline._execute_step(job)
    old = job.current_attempt_id
    ctx.bridge.check.side_effect = lambda path, **kw: checked(Path(path).read_bytes(), verdict="FORMAT_OK")
    pipeline._repair_step(job)
    assert job.current_attempt_id == old and job.gate_code == GateCode.CANDIDATE_REJECTED
    assert ctx.attempts[old + 1].check["issues"][-1]["code"] == "INPUT_WITNESS_EVIDENCE_MISSING"


def test_bridge_host_manifest_is_optional_and_forwarded(monkeypatch, tmp_path):
    bridge = UtAgentBridge(BridgeSettings.from_config({}))
    run = Mock(return_value={"prepared": {}, "execution": {}, "check": {}})
    monkeypatch.setattr(bridge, "_run", run)
    args = {key: tmp_path / key for key in ("ir_json", "intents", "reference", "candidate", "out")}
    bridge.host_prepare(**args)
    assert "--manifest" not in run.call_args.args[0]
    bridge.host_prepare(**args, manifest=tmp_path / "manifest.json")
    assert run.call_args.args[0][-2:] == ["--manifest", str(tmp_path / "manifest.json")]
    bridge.host_execute(**args, config={"manifest_path": str(tmp_path / "manifest.json")})
    command = run.call_args.args[0]
    assert command[command.index("--manifest") + 1] == str(tmp_path / "manifest.json")
    assert "--winams" not in command


def test_host_prompt_preserves_expected_values_and_uses_typed_oracle():
    prompt = repair._system_prompt({"execution_backend": "clang"})
    assert "WinAMS" not in prompt
    assert "typed implementation oracle" in prompt and "Existing expectations remain immutable" in prompt


@pytest.mark.parametrize("case,code", [("digest", GateCode.INTEGRITY_ERROR),
                                      ("configuration", GateCode.CONFIGURATION_CONFLICT),
                                      ("incomplete", GateCode.REPORT_INCOMPLETE)])
def test_host_report_gates_stop_before_case_model(lifecycle, monkeypatch, case, code):
    ctx, job = lifecycle, lifecycle.job
    check = checked(REFERENCE)
    if case == "digest":
        check["candidate_sha256"] = "other"
    elif case == "configuration":
        check["test_requirements"] = {"status": "NEEDS_REVIEW"}
    else:
        check["completeness"]["unresolved_count"] = 60
    execution = {"status": "HOST_PASS", "evidence_source": "HOST_EXECUTION"}
    monkeypatch.setattr(pipeline.host, "execute", lambda **kw: (execution, check))
    pipeline._execute_step(job)
    assert job.gate_code == code and not ctx.revision.called and job.repair_rounds == 0


def test_typed_artifact_failure_keeps_parent_and_does_not_consume_round(lifecycle, monkeypatch):
    ctx, job = lifecycle, lifecycle.job
    parent = ctx.new_attempt(job, UtAttempt.ORIGIN_LLM, SOURCE)
    root = Path(parent.csv_path).parent / f"exec-{parent.seq}"
    execution = artifact(root)
    execution.update(status="HOST_PASS", evidence_source="HOST_EXECUTION")
    execution["typed_candidate"]["sha256"] = "changed"
    check = checked(SOURCE, witnessed=True)
    monkeypatch.setattr(pipeline.host, "execute", lambda **kw: (execution, check))
    pipeline._execute_step(job)
    assert job.current_attempt_id == parent.id and len(ctx.attempts) == 2
    assert job.gate_code == GateCode.INTEGRITY_ERROR and job.repair_rounds == 0
    assert parent.check["verdict"] == "BLOCKED" and parent.check["evidence_integrity"]["status"] == "UNKNOWN"


EXECUTED = REFERENCE + b';$L$,TRUE\r\n,1,1\r\n;$L$,LLM add A\r\n,5,1\r\n'
PRESENTED = REFERENCE.replace(b',0,0\r\n', b';$L$,FALSE\r\n,0,0\r\n;$L$,\x91g\x8d\x87\x82\xb9(FALSE)\r\n,5,1\r\n'
                              b';$L$,TRUE\r\n,1,1\r\n')


def presented(tmp_path, source=EXECUTED, data=PRESENTED):
    sandbox = tmp_path / "execution" / "sandbox"
    sandbox.mkdir(parents=True, exist_ok=True)
    path = sandbox / "presented-candidate.csv"
    path.write_bytes(data)
    return {"status": "HOST_PASS", "sandbox": str(sandbox), "presented_candidate": {
        "contract": "ut-agent.host-presented-candidate/1", "path": str(path), "sha256": sha(data),
        "source_candidate_sha256": sha(source)}}


def test_presented_candidate_is_a_relabelled_permutation_of_the_executed_rows(tmp_path):
    assert host_candidate.read_presented_candidate(presented(tmp_path), EXECUTED) == PRESENTED
    assert host_candidate.read_presented_candidate({"status": "HOST_PASS"}, EXECUTED) is None
    assert host_candidate.read_presented_candidate(None, EXECUTED) is None


@pytest.mark.parametrize("case", ["contract", "source-sha", "sha", "failed-execution", "outside",
                                   "cell", "dropped-row", "header"])
def test_untrusted_presented_candidate_is_rejected(tmp_path, case):
    data = {"cell": PRESENTED.replace(b",5,1", b",5,2"),
            "dropped-row": PRESENTED.replace(b";$L$,TRUE\r\n,1,1\r\n", b""),
            "header": PRESENTED.replace(b'"f",1,1', b'"g",1,1')}.get(case, PRESENTED)
    execution = presented(tmp_path, data=data)
    receipt = execution["presented_candidate"]
    if case == "contract":
        receipt["contract"] = "unknown"
    elif case == "source-sha":
        receipt["source_candidate_sha256"] = sha(SOURCE)
    elif case == "sha":
        receipt["sha256"] = sha(b"other")
    elif case == "failed-execution":
        execution["status"] = "HOST_MISMATCH"
    elif case == "outside":
        outside = tmp_path / "presented.csv"
        outside.write_bytes(data)
        receipt["path"] = str(outside)
    with pytest.raises((ValueError, TypeError)):
        host_candidate.read_presented_candidate(execution, EXECUTED)


def test_attempt_csv_serves_presented_form_and_raw_form_for_revision(tmp_path, monkeypatch):
    path = tmp_path / "a03-host_derived.csv"
    path.write_bytes(EXECUTED)
    attempt = SimpleNamespace(csv_path=str(path), csv_sha256=sha(EXECUTED), execution=presented(tmp_path))
    monkeypatch.setattr(pipeline, "_csv_attempt", lambda *args: attempt)
    assert pipeline.attempt_csv(1, 2, 3) == (PRESENTED, path.name)
    assert pipeline.attempt_csv(1, 2, 3, raw=True) == (EXECUTED, path.name)
    attempt.execution["presented_candidate"]["sha256"] = sha(b"other")
    with pytest.raises(ServiceError):
        pipeline.attempt_csv(1, 2, 3)
