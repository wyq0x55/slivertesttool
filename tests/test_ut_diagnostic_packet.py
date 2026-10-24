"""Pure packet and state tests: no application factory or database fixtures."""

import hashlib
from types import SimpleNamespace

import pytest

from app.services.lanmatrix.errors import ServiceError
from app.services.ut_pipeline import diagnostic, increment, pipeline

CSV = ("mod,a.c/f,f,2,1\r\n#COMMENT,a.c/x,a.c/y,a.c/f@@\r\n"
       ";$L$,private label must not be bundled\r\n" +
       "".join(f",{i},0,777\r\n" for i in range(70))).encode("cp932")
SHA = hashlib.sha256(CSV).hexdigest()


def check_document():
    gaps = [{"obligation_id": f"B{i}", "gap_class": "MISSING_CASE"} for i in range(75)]
    return {"candidate_sha256": SHA, "verdict": "PASS",
            "completeness": {"unresolved": gaps, "unresolved_count": 75,
                             "output_pending": [{"obligation_id": "O"}], "details_complete": True},
            "issues": [{"code": "INPUT_OBLIGATION_UNRESOLVED", "detail": gaps[:50]}],
            "input_ranges": {"contract": {"contract": "ut-agent.input-domains/1", "columns": [
                {"index": 1, "column": "a.c/x", "status": "PROVEN", "min": 0, "max": 100},
                {"index": 2, "column": "a.c/y", "status": "UNKNOWN"}]}},
            "candidate_witnesses": {"contract": "ut-agent.candidate-witness/1", "candidate_sha256": SHA,
                "evidence": {"identified": True}, "input_mapping_identified": True,
                "repair_context": [{"obligation_id": "B0", "path": [{"decision": "x > 1",
                    "location": {"file": "src/a.c", "line": 41}, "required_outcome": False,
                    "atoms": [{"variable": "x", "input_tokens": ["I1"], "binding": "DIRECT_INPUT"}]}]}]}}


def packet(check=None, data=CSV, **options):
    return diagnostic.build_packet(project_id=4, job_id=9,
        attempt={"id": 12, "seq": 3, "origin": "HUMAN", "csv_sha256": options.get("attempt_sha", SHA),
                 "check": check if check is not None else check_document()},
        target={"function": "f", "source_rel": "src/a.c"}, csv_bytes=data,
        constraints={"revision_rules": ["locked"], "test_policy_sha256": "frozen"},
        provenance={"generator_commit": "old-commit"}, allowed={"B0"},
        editable_columns=options.get("editable_columns", {0}))


def test_packet_contains_all_rows_exact_columns_domains_and_full_evidence():
    result = packet()
    assert result["contract"] == "ut-platform.diagnostic-packet/2"
    assert result["attempt_id"] == 12 and result["version"]["seq"] == 3
    assert result["candidate_sha256"] == SHA
    assert len(result["inputs"]["rows"]) == 70
    assert result["inputs"]["rows"][-1] == {"base_row": 70, "inputs": {"I1": "69", "I2": "0"}}
    assert result["inputs"]["columns"][0] == {"token": "I1", "index": 1, "column": "a.c/x",
        "domain": {"index": 1, "column": "a.c/x", "status": "PROVEN", "min": 0, "max": 100}}
    assert result["inputs"]["columns"][1]["domain"]["status"] == "UNKNOWN"
    assert len(result["check"]["completeness"]["unresolved"]) == 75
    assert len(result["check"]["issues"][0]["detail"]) == 50
    assert result["check"]["candidate_witnesses"]["repair_context"][0]["path"][0]["location"]["line"] == 41
    assert result["test_configuration"]["revision_rules"] == ["locked"]
    assert result["provenance"]["generator_commit"] == "old-commit"
    assert result["typed_evidence_status"] == "IDENTIFIED"
    assert "private label" not in str(result) and "777" not in str(result["inputs"])


def test_packet_reply_contract_preserves_append_only_verifier_scope():
    result = packet()
    boundary = result["modification_boundary"]
    assert boundary["allowed_obligations"] == ["B0"]
    assert boundary["editable_input_tokens"] == ["I1"]
    assert boundary["max_rows"] == 20 and boundary["existing_rows"] == "immutable"
    assert result["reply_contract"]["auto_apply"] is False
    reply = result["reply_contract"]["example"]["rows"][0]
    reply.update(obligation="B0", base_row=1, inputs={"I1": "71"})
    table = increment.parse(CSV.decode("cp932"))
    applied = increment.apply(table, [reply], set(boundary["allowed_obligations"]),
                              result["check"]["input_ranges"]["contract"], editable_columns={0})
    assert applied.accepted and applied.csv_bytes.endswith(b",71,0,\r\n")
    reply["inputs"] = {"I2": "2"}
    assert not increment.apply(table, [reply], {"B0"}, editable_columns={0}).accepted


@pytest.mark.parametrize("field", ["identified", "input_mapping_identified", "domain"])
def test_missing_or_mismatched_mapping_stays_explicit(field):
    check = check_document()
    if field == "domain":
        check["input_ranges"]["contract"]["columns"][0]["column"] = "similar_name"
    elif field == "identified":
        check["candidate_witnesses"]["evidence"][field] = False
    else:
        check["candidate_witnesses"][field] = False
    result = packet(check)
    assert result["typed_evidence_status"] == "UNKNOWN"
    if field == "domain":
        assert result["inputs"]["columns"][0]["domain"] is None
        assert result["inputs"]["domain_mapping_status"] == "UNKNOWN"
    assert "decline" in result["modification_boundary"]["unidentified_mapping"]


def test_packet_refuses_changed_csv_or_check_hash():
    with pytest.raises(ValueError, match="hash"):
        packet(data=CSV + b"\r\n")
    check = check_document()
    check["candidate_sha256"] = "other"
    with pytest.raises(ValueError, match="different candidate"):
        packet(check)


@pytest.mark.parametrize("record", [b",1,2\r\n", b",1,2,3,4\r\n", b',"unterminated\r\n'])
def test_packet_never_certifies_lossy_csv_parsing(record):
    data = CSV + record
    digest = hashlib.sha256(data).hexdigest()
    with pytest.raises(ValueError, match="malformed"):
        packet(check={"candidate_sha256": digest}, data=data, attempt_sha=digest)


@pytest.mark.parametrize("field,value", [("candidate_sha256", "DIFFERENT_CANDIDATE"),
                                         ("candidate_sha256", None), ("contract", "unsupported/2")])
def test_packet_does_not_identify_unbound_witness_evidence(field, value):
    check = check_document()
    check["candidate_witnesses"][field] = value
    assert packet(check)["typed_evidence_status"] == "UNKNOWN"


def test_truncated_pending_output_list_also_blocks_repairs_and_packet_scope():
    check = check_document()
    check["completeness"] = {"unresolved": [], "unresolved_count": 0,
        "output_pending": [{"obligation_id": f"O{i}"} for i in range(50)], "output_pending_count": 60}
    check["verdict"] = "FAIL"
    check["issues"] = [{"code": "COVERAGE_GAP", "severity": "error", "repairable": True}]
    status = pipeline.gap_status(check, [], SHA)
    assert status["report_complete"] is False
    assert pipeline.next_state(check, "GENERATOR", 0, 5, status)[0] == "needs_human"
    result = packet(check)
    assert result["gap_report"]["output_pending_complete"] is False
    assert result["gap_report"]["details_complete"] is False
    assert result["modification_boundary"]["allowed_obligations"] == []


@pytest.mark.parametrize("verdict", ["PASS", "FAIL"])
def test_historical_60_reported_50_supplied_never_completes_or_spends_rounds(verdict):
    gaps = [{"obligation_id": f"B{i}", "gap_class": "MISSING_CASE"} for i in range(50)]
    check = {"verdict": verdict, "candidate_sha256": SHA, "issues": [],
             "completeness": {"unresolved_count": 60, "unresolved": gaps},
             "candidate_witnesses": {"contract": "ut-agent.candidate-witness/1",
                 "candidate_sha256": SHA, "evidence": {"identified": True},
                 "witnessed_obligation_ids": [g["obligation_id"] for g in gaps]}}
    status = pipeline.gap_status(check, [], SHA)
    assert status["report_complete"] is False and status["open"] == []
    assert status["missing"] == status["witnessed"] == 50
    for rounds in (0, 5):
        state, reason, _ = pipeline.next_state(check, "GENERATOR", rounds, 5, status)
        assert state == "needs_human"
        assert "重新生成" in reason and "重新校验" in reason
    result = packet(check)
    assert result["gap_report"]["details_complete"] is False
    assert result["modification_boundary"]["allowed_obligations"] == []


def test_explicit_incomplete_flag_and_complete_full_list():
    check = check_document()
    assert pipeline.gap_status(check, [])["report_complete"] is True
    check["completeness"]["details_complete"] = False
    assert pipeline.gap_status(check, [])["report_complete"] is False


def test_attempt_transport_checks_captured_hash_without_database(tmp_path):
    path = tmp_path / "attempt.csv"
    path.write_bytes(CSV)
    attempt = SimpleNamespace(csv_path=str(path), csv_sha256=SHA)
    assert pipeline._read_attempt_csv(attempt) == CSV
    path.write_bytes(CSV + b"\r\n")
    with pytest.raises(ServiceError) as error:
        pipeline._read_attempt_csv(attempt)
    assert error.value.code == "INTEGRITY_ERROR"
