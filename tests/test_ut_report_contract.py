"""Malformed completeness reports cannot authorize repair or machine PASS."""

from copy import deepcopy

import pytest

from app.models.ut_pipeline import GateCode, UtJob
from app.services.ut_pipeline import diagnostic, pipeline


def _complete():
    return {"unresolved": [], "unresolved_count": 0, "output_pending": [],
            "output_pending_count": 0, "details_complete": True}


INVALID_REPORTS = [
    pytest.param({"details_complete": True}, id="flag-only"),
    pytest.param({"unresolved_count": 0}, id="count-only"),
    pytest.param({"unresolved": []}, id="list-only"),
    pytest.param({"unresolved": None, "unresolved_count": 0}, id="null-list"),
    pytest.param({"unresolved": {}, "unresolved_count": 0}, id="object-list"),
    pytest.param({"unresolved": "B", "unresolved_count": 1}, id="string-list"),
    pytest.param({"unresolved": [], "unresolved_count": False}, id="false-count"),
    pytest.param({"unresolved": [{"obligation_id": "B"}], "unresolved_count": True}, id="true-count"),
    pytest.param({"unresolved": [], "unresolved_count": 0.0}, id="float-count"),
    pytest.param({"unresolved": [], "unresolved_count": "0"}, id="string-count"),
    pytest.param({"unresolved": [], "unresolved_count": -1}, id="negative-count"),
    pytest.param({"unresolved": [None], "unresolved_count": 1}, id="null-record"),
    pytest.param({"unresolved": [{}], "unresolved_count": 1}, id="missing-id"),
    pytest.param({"unresolved": [{"obligation_id": []}], "unresolved_count": 1}, id="invalid-id"),
    pytest.param({"unresolved": [{"obligation_id": " "}], "unresolved_count": 1}, id="blank-id"),
    pytest.param({"unresolved": [{"obligation_id": "B"}] * 2, "unresolved_count": 2}, id="duplicate-id"),
    pytest.param({"unresolved": [{"obligation_id": "B", "gap_class": "MISSING_CASE", "requirement": []}],
                  "unresolved_count": 1}, id="invalid-requirement"),
    pytest.param({**_complete(), "details_complete": 1}, id="numeric-flag"),
    pytest.param({**_complete(), "details_complete": "true"}, id="string-flag"),
    pytest.param({**_complete(), "details_complete": None}, id="null-flag"),
    pytest.param({"unresolved": [], "unresolved_count": 0, "output_pending": []}, id="pending-list-only"),
    pytest.param({"unresolved": [], "unresolved_count": 0, "output_pending_count": 0}, id="pending-count-only"),
    pytest.param({**_complete(), "output_pending": None}, id="pending-null-list"),
    pytest.param({**_complete(), "output_pending_count": False}, id="pending-bool-count"),
    pytest.param({**_complete(), "output_pending": ["O"], "output_pending_count": 1}, id="pending-invalid-record"),
    pytest.param({**_complete(), "output_pending": [{"obligation_id": "O"}],
                  "output_pending_count": 0}, id="pending-count-mismatch"),
    pytest.param(True, id="nonempty-bool-report"),
    pytest.param(1, id="nonempty-int-report"),
    pytest.param("report", id="nonempty-string-report"),
    pytest.param([{}], id="nonempty-list-report"),
]


@pytest.mark.parametrize("completeness", INVALID_REPORTS)
@pytest.mark.parametrize("verdict", ["PASS", "FAIL"])
def test_invalid_reports_do_not_pass_or_schedule_model_work(completeness, verdict):
    check = {"verdict": verdict, "completeness": completeness, "issues": [
        {"code": "COVERAGE_C1_GAP", "severity": "error", "repairable": True}]}
    before = deepcopy(check)
    report = diagnostic.gap_report(check)
    assert report["details_complete"] is False and report["errors"]
    gaps = pipeline.gap_status(check, [])
    state, reason, code = pipeline.next_state(check, "GENERATOR", 0, 5, gaps)
    assert (state, code) == (UtJob.NEEDS_HUMAN, GateCode.REPORT_INCOMPLETE)
    assert "重新校验" in reason
    assert check == before


@pytest.mark.parametrize("completeness", INVALID_REPORTS)
@pytest.mark.parametrize("verdict,expected_gate", [
    ("BLOCKED", GateCode.ENV_BLOCKED), ("INVALID", GateCode.UNKNOWN_VERDICT),
])
def test_invalid_reports_preserve_prior_execution_failures(completeness, verdict, expected_gate):
    check = {"verdict": verdict, "completeness": completeness, "issues": [
        {"code": "EXECUTION_BLOCKED", "severity": "error", "repairable": False,
         "detail": [{"code": "ASSET_MISSING", "path": "AMS_Setting"}]}]}
    state, reason, code = pipeline.next_state(check, "GENERATOR", 0, 5, pipeline.gap_status(check, []))
    assert code == expected_gate
    assert state == (UtJob.BLOCKED if verdict == "BLOCKED" else UtJob.NEEDS_HUMAN)
    if verdict == "BLOCKED":
        assert "ASSET_MISSING" in reason and "AMS_Setting" in reason


@pytest.mark.parametrize("completeness", INVALID_REPORTS)
def test_invalid_reports_remove_offline_repair_scope(completeness):
    from test_ut_diagnostic_packet import check_document, packet

    check = check_document()
    check["completeness"] = completeness
    result = packet(check)
    assert result["gap_report"]["details_complete"] is False
    assert result["modification_boundary"]["allowed_obligations"] == []


@pytest.mark.parametrize("modern", [False, True])
def test_supported_report_formats_keep_explicit_counts(modern):
    completeness = _complete() if modern else {"unresolved": [], "unresolved_count": 0}
    check = {"verdict": "PASS", "completeness": completeness, "issues": []}
    report = diagnostic.gap_report(check)
    assert report["details_complete"] is True and report["errors"] == []
    assert report["output_pending_reported"] is modern
    assert pipeline.next_state(check, "GENERATOR", 0, 5, pipeline.gap_status(check, []))[0] == UtJob.AWAITING_REVIEW


def test_host_bridge_fixture_supplies_a_complete_report(tmp_path):
    from test_ut_host import HostBridge
    from test_ut_pipeline import GOOD

    candidate = tmp_path / "candidate.csv"
    candidate.write_bytes(GOOD)
    result = HostBridge(tmp_path).host_execute(out=tmp_path / "out", candidate=candidate, reference=candidate)
    check = result["check"]
    assert diagnostic.gap_report(check)["details_complete"] is True
    assert pipeline._report_gate(check, execution=result["execution"]) is None
    assert check["counts_as_target_evidence"] is False and check["delivery_validated"] is False
