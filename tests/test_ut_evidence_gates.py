"""Pure regressions for configuration, identity and host evidence gates."""

import hashlib
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from app.models.ut_pipeline import GateCode, UtJob
from app.services.lanmatrix.errors import ServiceError
from app.services.ut_pipeline import diagnostic, pipeline

CSV = b'mod,"a.c/f","f",1,0\r\n#COMMENT,"a.c/x"\r\n,5\r\n'
SHA = hashlib.sha256(CSV).hexdigest()


def _check(*, verdict="PASS", unresolved=None, pending=None, witness=True, digest=SHA):
    unresolved, pending = unresolved or [], pending or []
    return {"verdict": verdict, "issues": [], "candidate_sha256": digest,
            "completeness": {"unresolved": unresolved, "unresolved_count": len(unresolved),
                             "output_pending": pending, "output_pending_count": len(pending),
                             "details_complete": True},
            "candidate_witnesses": {"contract": "ut-agent.candidate-witness/1",
                "candidate_sha256": digest, "evidence": {"identified": True},
                "witnessed_obligation_ids": ["b1:T"] if witness else []}}


def _attempt(path, check=None, execution=None, sha=SHA):
    return SimpleNamespace(id=10, seq=1, origin="GENERATOR", csv_path=str(path),
                           csv_sha256=sha, check=check, execution=execution,
                           verdict=(check or {}).get("verdict"))


def _job():
    return SimpleNamespace(id=1, project_id=4, batch_id=2, state=UtJob.REPAIRING,
                           current_attempt_id=10, repair_rounds=0, max_repair_rounds=5,
                           reason="", gate_code="", generation={}, reference_csv="",
                           target=SimpleNamespace(function="f", source_rel="a.c", unit_rel="u"),
                           last_verdict="")


def test_configuration_conflicts_are_preserved_as_human_gate_not_repair_work():
    conflict = {"obligation_id": "B", "gap_class": "MISSING_CASE",
                "requirement": {"configuration_status": "NEEDS_REVIEW"}}
    check = _check(unresolved=[conflict])
    status = pipeline.gap_status(check, [{"claimed": ["B"]}], SHA)
    assert status["configuration_conflicts"] == ["B"] and status["open"] == []
    state, reason, code = pipeline.next_state(check, "GENERATOR", 0, 5, status)
    assert (state, code) == (UtJob.NEEDS_HUMAN, GateCode.CONFIGURATION_CONFLICT)
    assert "配置冲突" in reason


def test_policy_level_configuration_conflict_is_not_machine_pass():
    check = _check()
    check["test_requirements"] = {"status": "NEEDS_REVIEW"}
    state, _, code = pipeline.next_state(check, "GENERATOR", 0, 5, pipeline.gap_status(check, [], SHA))
    assert (state, code) == (UtJob.NEEDS_HUMAN, GateCode.CONFIGURATION_CONFLICT)


def test_truncated_report_uses_explicit_incomplete_gate():
    check = _check(unresolved=[{"obligation_id": "A", "gap_class": "MISSING_CASE"}])
    check["completeness"]["unresolved_count"] = 60
    state, reason, code = pipeline.next_state(check, "GENERATOR", 0, 5,
                                               pipeline.gap_status(check, [], SHA))
    assert (state, code) == (UtJob.NEEDS_HUMAN, GateCode.REPORT_INCOMPLETE)
    assert "重新生成" in reason


def test_report_without_completeness_is_unknown_not_implicitly_complete():
    report = diagnostic.gap_report({"verdict": "PASS", "issues": []})
    assert report["details_complete"] is False
    state, _, code = pipeline._report_gate({"verdict": "PASS", "issues": []})
    assert (state, code) == (UtJob.NEEDS_HUMAN, GateCode.REPORT_INCOMPLETE)


@pytest.mark.parametrize("completeness", [None, False, [], 0, {}])
@pytest.mark.parametrize("verdict", ["PASS", "FAIL"])
def test_empty_or_invalid_report_neither_passes_nor_schedules_repair(completeness, verdict):
    check = {"verdict": verdict, "issues": [
        {"code": "COVERAGE_C1_GAP", "severity": "error", "repairable": True}],
        "completeness": completeness}
    assert diagnostic.gap_report(check)["details_complete"] is False
    state, _, code = pipeline.next_state(check, "GENERATOR", 0, 5, pipeline.gap_status(check, [], SHA))
    assert (state, code) == (UtJob.NEEDS_HUMAN, GateCode.REPORT_INCOMPLETE)


@pytest.mark.parametrize("completeness", [None, {}])
@pytest.mark.parametrize("verdict,expected_gate", [
    ("BLOCKED", GateCode.ENV_BLOCKED), ("INVALID", GateCode.UNKNOWN_VERDICT),
])
def test_missing_report_does_not_hide_a_prior_execution_failure(completeness, verdict, expected_gate):
    check = {"verdict": verdict, "completeness": completeness, "issues": [
        {"code": "EXECUTION_BLOCKED", "severity": "error", "repairable": False,
         "detail": [{"code": "ASSET_MISSING", "path": "AMS_Setting"}]}]}
    state, reason, code = pipeline.next_state(check, "GENERATOR", 0, 5, pipeline.gap_status(check, [], SHA))
    assert code == expected_gate
    assert state == (UtJob.BLOCKED if verdict == "BLOCKED" else UtJob.NEEDS_HUMAN)
    if verdict == "BLOCKED":
        assert "ASSET_MISSING" in reason and "AMS_Setting" in reason


@pytest.mark.parametrize("where", ["check", "witness", "host_binding"])
def test_existing_digest_conflicts_block_and_remove_proof(where):
    check = _check()
    execution = None
    if where == "check":
        check["candidate_sha256"] = "other"
    elif where == "witness":
        check["candidate_witnesses"]["candidate_sha256"] = "other"
    else:
        execution = {"input_binding": {"candidate": {"sha256": "other"}}}
    errors = diagnostic.integrity_errors(check, SHA, execution)
    assert errors
    assert pipeline._verified_obligation_ids(check, SHA, execution) == set()
    blocked = pipeline._integrity_check(check, errors[0])
    assert blocked["verdict"] == "BLOCKED"
    assert blocked["candidate_witnesses"]["evidence"]["identified"] is False


def test_host_nested_candidate_binding_is_the_identity_source():
    execution = {"input_binding": {"candidate": {"path": "candidate.csv", "sha256": SHA}}}
    assert diagnostic.integrity_errors(_check(), SHA, execution) == []
    assert diagnostic.integrity_errors({"candidate_sha256": None}, SHA, execution) == []


def test_result_integrity_records_unknown_optional_digest(tmp_path, monkeypatch):
    candidate = tmp_path / "candidate.csv"
    candidate.write_bytes(CSV)
    check = _check(digest=None)
    check["candidate_witnesses"]["candidate_sha256"] = None
    check["counts_as_target_evidence"] = False
    attempt = _attempt(candidate, check)
    gates = []
    monkeypatch.setattr(pipeline, "_integrity_gate", lambda *args: gates.append(args))
    assert pipeline._result_integrity(_job(), attempt, check, {
        "input_binding": {"candidate": {"sha256": SHA}}, "counts_as_target_evidence": False})
    assert check["candidate_identity"]["status"] == "UNKNOWN"
    assert check["counts_as_target_evidence"] is False and not gates


def test_pre_repair_file_change_stops_before_model_or_round(tmp_path, monkeypatch):
    candidate = tmp_path / "candidate.csv"
    candidate.write_bytes(CSV + b"\r\n")
    attempt = _attempt(candidate, _check())
    job = _job()
    model, gate = Mock(), Mock()
    session = SimpleNamespace(get=lambda model, ident: {1: job, 2: SimpleNamespace(constraints={}), 10: attempt}[ident],
                              commit=Mock())
    monkeypatch.setattr(pipeline, "_integrity_gate", gate)
    monkeypatch.setattr(pipeline, "db", SimpleNamespace(session=session))
    monkeypatch.setattr(pipeline.repair, "revise", model)
    pipeline._repair_step(job)
    assert job.repair_rounds == 0 and not model.called
    assert gate.call_count == 1
    assert "完整性检查失败" in gate.call_args.args[2]


def test_post_execution_identity_conflict_is_blocked(tmp_path, monkeypatch):
    candidate = tmp_path / "candidate.csv"
    candidate.write_bytes(CSV)
    attempt, job, gate = _attempt(candidate, _check()), _job(), Mock()
    monkeypatch.setattr(pipeline, "_gate", gate)
    execution = {"input_binding": {"candidate": {"sha256": "other"}},
                 "counts_as_target_evidence": False}
    assert not pipeline._result_integrity(job, attempt, attempt.check, execution)
    assert gate.call_args.args[3] == GateCode.INTEGRITY_ERROR
    assert attempt.check["verdict"] == attempt.verdict == job.last_verdict == "BLOCKED"


def test_repair_checks_binding_before_contacting_model(tmp_path, monkeypatch):
    candidate = tmp_path / "candidate.csv"
    candidate.write_bytes(CSV)
    check = _check()
    check["candidate_sha256"] = "other"
    attempt = _attempt(candidate, check, {"input_binding": {"candidate": {"sha256": "other"}}})
    model = Mock(side_effect=AssertionError("model must not be contacted"))
    monkeypatch.setattr(pipeline, "db", SimpleNamespace(session=SimpleNamespace(
        get=lambda model, ident: {1: _job(), 2: SimpleNamespace(constraints={}), 10: attempt}[ident],
        commit=Mock())))
    monkeypatch.setattr(pipeline, "_gate", Mock())
    monkeypatch.setattr(pipeline.repair, "revise", model)
    pipeline._repair_step(_job())
    assert not model.called


def test_repair_checks_binding_before_contacting_model_pure(tmp_path, monkeypatch):
    candidate = tmp_path / "candidate.csv"
    candidate.write_bytes(CSV)
    check = _check()
    check["candidate_sha256"] = "other"
    execution = {"input_binding": {"candidate": {"sha256": "other"}}}
    model = Mock(side_effect=AssertionError("model must not be contacted"))
    monkeypatch.setattr(pipeline.repair.provider, "invoke_chat", model)
    meta = pipeline.repair.revise(constraints={}, target={}, csv_bytes=CSV, check=check,
                                  execution=execution, history=[], open_gaps=[], allowed=set())
    assert meta.csv_bytes is None and meta.meta["code"] == "INTEGRITY_ERROR"
    assert meta.meta["model_contacted"] is False and not model.called


def test_approval_rechecks_policy_conflict_and_requires_note(monkeypatch):
    check = _check()
    check["test_requirements"] = {"status": "NEEDS_REVIEW"}
    attempt = _attempt(SimpleNamespace(), check)
    job = SimpleNamespace(state=UtJob.AWAITING_REVIEW, current_attempt_id=10,
                          gate_code=GateCode.MACHINE_PASS, superseded_by=None)
    monkeypatch.setattr(pipeline, "_locked_job", lambda *args: job)
    monkeypatch.setattr(pipeline, "_read_attempt_csv", lambda attempt: CSV)
    monkeypatch.setattr(pipeline, "db", SimpleNamespace(session=SimpleNamespace(get=lambda *args: attempt)))
    with pytest.raises(ServiceError, match="审核意见"):
        pipeline.review(4, 1, "approve", 7, "")


@pytest.mark.parametrize("note", ["", "人工已审阅"])
def test_approval_cannot_override_a_changed_version_file(monkeypatch, note):
    attempt = _attempt(SimpleNamespace(), _check())
    job = SimpleNamespace(state=UtJob.AWAITING_REVIEW, current_attempt_id=10,
                          gate_code=GateCode.MACHINE_PASS, superseded_by=None)
    monkeypatch.setattr(pipeline, "_locked_job", lambda *args: job)
    monkeypatch.setattr(pipeline, "_read_attempt_csv", lambda attempt: (_ for _ in ()).throw(
        ServiceError("Candidate CSV changed", code="INTEGRITY_ERROR")))
    monkeypatch.setattr(pipeline, "db", SimpleNamespace(session=SimpleNamespace(get=lambda *args: attempt)))
    with pytest.raises(ServiceError, match="恢复原版本文件") as error:
        pipeline.review(4, 1, "approve", 7, note)
    assert error.value.code == "INTEGRITY_ERROR"
    assert job.state == UtJob.AWAITING_REVIEW


def test_host_pass_uses_shared_configuration_gate():
    check = _check()
    check["test_requirements"] = {"status": "NEEDS_REVIEW"}
    execution = {"counts_as_target_evidence": False,
                 "input_binding": {"candidate": {"sha256": SHA}}}
    state, _, code = pipeline._report_gate(check, SHA, execution)
    assert (state, code) == (UtJob.NEEDS_HUMAN, GateCode.CONFIGURATION_CONFLICT)
    assert execution["counts_as_target_evidence"] is False


@pytest.mark.parametrize("case,expected_gate", [
    ("digest", GateCode.INTEGRITY_ERROR), ("truncated", GateCode.REPORT_INCOMPLETE),
    ("configuration", GateCode.CONFIGURATION_CONFLICT), ("complete", GateCode.MACHINE_PASS),
])
def test_host_execution_applies_all_report_gates_before_pass(tmp_path, monkeypatch, case, expected_gate):
    candidate = tmp_path / "candidate.csv"
    candidate.write_bytes(CSV)
    attempt = _attempt(candidate, _check())
    job = _job()
    job.state = UtJob.EXECUTING
    job.stop_requested_at = None
    job.generation = {"ir_json": str(tmp_path / "ir.json"),
                      "intent_manifest": str(tmp_path / "intents.json")}
    job.reference_csv = str(candidate)
    execution = {"status": "HOST_PASS", "evidence_source": "HOST_EXECUTION",
                 "counts_as_target_evidence": False,
                 "input_binding": {"candidate": {"path": str(candidate), "sha256": "other" if case == "digest" else SHA}}}
    check = _check()
    if case == "truncated":
        check["completeness"]["unresolved"] = [{"obligation_id": f"B{i}"} for i in range(50)]
        check["completeness"]["unresolved_count"] = 60
    elif case == "configuration":
        check["test_requirements"] = {"status": "NEEDS_REVIEW"}
    gate = Mock()
    monkeypatch.setattr(pipeline, "_gate", gate)
    monkeypatch.setattr(pipeline, "_read_attempt_csv", lambda attempt: CSV)
    monkeypatch.setattr(pipeline.host, "execute", lambda **kwargs: (execution, check))
    monkeypatch.setattr(pipeline, "_fresh_exec_dir", lambda *args: tmp_path / "out")
    session = SimpleNamespace(get=lambda model, ident: {1: job, 2: SimpleNamespace(), 10: attempt}[ident],
                              commit=Mock(), refresh=Mock(), is_active=True)
    monkeypatch.setattr(pipeline, "db", SimpleNamespace(session=session))
    monkeypatch.setattr(pipeline.timing, "db", SimpleNamespace(session=session))
    monkeypatch.setattr(pipeline, "current_app", SimpleNamespace(config={"UT_REPAIR_TIMEOUT": 1}))
    pipeline._execute_host_step(job, attempt, {"execution_backend": "clang"})
    assert gate.call_args.args[3] == expected_gate
    assert attempt.verdict == ("BLOCKED" if case == "digest" else "PASS")
    assert attempt.execution["counts_as_target_evidence"] is False
