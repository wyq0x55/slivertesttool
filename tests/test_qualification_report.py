"""Qualification report presentation, case scoping and immutable input checks."""

import hashlib
import re
import zipfile
from types import SimpleNamespace

import pytest

from app.services.qualification_report_renderer import render_reports
from app.services import qualification_report_service as reports
from app.services.qualification_report_service import build_qualification_report, parse_judge_rows


def case(status="Passed", **kwargs):
    return {"id": "TC-1", "name": "Name", "purpose": "Purpose", "category": "Category",
            "status": status, "rows": [], **kwargs}


def report(cases):
    return {"general": {}, "suites": [{"name": "SYS", "cases": cases}]}


def test_preserves_reference_css_logo_statistics_and_offline_links():
    pages = render_reports(report([case()] * 14 + [case("Failed")] * 9 + [case("Untested")] * 27), "Report")
    overview, detail = pages.values()
    css = re.search(r"<style[^>]*>(.*?)</style>", overview, re.S).group(1)
    logo = re.search(r'<img[^>]*src="([^"]*)', overview).group(1)
    assert hashlib.sha256(css.encode()).hexdigest() == "7b7552df6b4ab899956f0b9f692b8470106a961db0ebaacd7fce907851139731"
    assert hashlib.sha256(logo.encode()).hexdigest() == "a6dae0362275667a7c2d2ce4c6e9e971cad74280a769702b6da3a1af0f0628c4"
    assert "61% of executed test cases" in overview
    assert "46% of all test cases" in overview
    assert "Test failed" in overview
    assert '<table class="TestCaseTable">' in detail
    assert '<table class="TestCaseTable">' not in overview
    for page in pages.values():
        for href in re.findall(r'href="([^"]+)"', page):
            assert href.startswith("#") or href in pages
        assert 'src="http' not in page


def test_escapes_all_fields_and_generates_safe_unique_names():
    attack = '<script>alert("x")</script>'
    unsafe = case(id=attack, name=attack, purpose=attack, category=attack,
                  rows=[{"description": attack + "\nnext", "checks": [{"target": attack, "passed": False}]}])
    data = report([unsafe])
    data["general"]["owner"] = attack
    data["suites"] += [{"name": "../bad", "cases": [unsafe]}, {"name": "SYS", "cases": []}]
    pages = render_reports(data, "Report")
    assert len(pages) == 4
    assert all("/" not in name and "\\" not in name for name in pages)
    for page in pages.values():
        assert attack not in page
    assert "&lt;script&gt;" in pages["Report_SYS.html"]
    assert "next<br>" in pages["Report_SYS.html"]
    with pytest.raises(ValueError):
        render_reports(data, "../Report")


@pytest.mark.parametrize("statuses, banner", [
    (["Passed"], "Test passed"), (["Untested"], "Test Untested"),
    (["T.B.D"], "Test T.B.D"), (["Passed", "Untested"], "Test passed"),
    (["Failed", "Untested"], "Test failed"),
])
def test_banner_uses_executed_results_and_untested_status(statuses, banner):
    assert banner in next(iter(render_reports(report([case(s) for s in statuses]), "Report").values()))


def test_judge_rows_keep_latest_exact_case_observations_and_stop_at_verdict():
    text = """INFO:root:Test case ID.TC-1 is started!
INFO:root:Test is failed in Step1!!!!
INFO:root:Test case ID.ID;;TC-10 is started!
INFO:root:Test is failed in Step2!!!!
INFO:root:Test case ID.ID;;TC-1 is started!
INFO:root:-------------------Step1-------------------
INFO:root:Preparation
INFO:root:Step1 Set input
INFO:root:●
INFO:root:    Monitoring target: State ( STATE )
INFO:root:    Expected Value:ON ( (exp==ON(1)) )
INFO:root:    Observed Value:0x1 ( 1.0 )
INFO:root:    確認タイミング：50ms
INFO:root:Step.1 is passed at 0.05s.
INFO:root:All steps are verified.Test is Passed.
INFO:root:Step.99 is failed at 10s.
"""
    rows = parse_judge_rows(text, "TC-1")
    observed = next(row for row in rows if row["checks"])
    assert observed["timestamp"] == "0.05s"
    assert observed["step"] == "Preparation"
    assert observed["description"] == "Step1 Set input"
    assert observed["result"] == "passed"
    assert observed["checks"] == [{"target": "State ( STATE )", "expected": "ON ( (exp==ON(1)) )",
                                    "observed": "0x1 ( 1.0 )", "timing": "50ms", "passed": True}]
    assert not any("Step.99" in row["description"] for row in rows)
    assert parse_judge_rows(text, "TC-missing") == []


def test_nested_subroutine_checks_return_to_parent_and_mock_verdicts_are_accepted():
    raw = """INFO:root:Test case ID.ID;;TC-1 is started!
INFO:root:-------------------Step1-------------------
INFO:root:Parent category
INFO:root:Step1 Parent operation
INFO:root: Subroutine(Child) is started!
INFO:root:------------------- Subroutine(Child) Step1-------------------
INFO:root:Child category
INFO:root: Subroutine(Child) Step1 Child operation
INFO:root: Subroutine(Grandchild) is started!
INFO:root:------------------- Subroutine(Grandchild) Step1-------------------
INFO:root:Grandchild category
INFO:root: Subroutine(Grandchild) Step1 Grandchild operation
INFO:root:Grandchild Step.1 is passed at 0.0s.
INFO:root: Subroutine(Grandchild) is ended!
INFO:root:Child Step.1 is passed at 0.0s.
INFO:root: Subroutine(Child) is ended!
INFO:root:▲
INFO:root:    Monitoring target: Parent target
INFO:root:    Expected Value:1
INFO:root:    Observed Value:0
INFO:root:    確認タイミング：任意
INFO:root:Step.1 is failed at 1.0s.
INFO:root:Test is failed in Step1!!!!
"""
    results = [row for row in parse_judge_rows(raw, "TC-1") if row["result"] != "-"]
    assert [row["step"] for row in results] == ["Grandchild category", "Child category", "Parent category"]
    assert results[-1]["description"] == "Step1 Parent operation"
    assert results[-1]["checks"][0]["target"] == "Parent target"
    assert results[-1]["result"] == "failed"
    mock = parse_judge_rows("Test case ID.ID;;TC-1 is started!\nStep.1 is passed\nAll steps are verified.Test is Passed.", "TC-1")
    assert mock[1]["result"] == "passed"


def test_system_under_test_matches_dash_only_fields_on_every_page():
    data = report([case("Untested")])
    data["general"] = {key: "Must not be added" for key in (
        "vehicle", "document", "owner", "project_owner", "software_version",
        "test_db_version", "date", "conclusion", "remarks")}
    for page in render_reports(data, "Report").values():
        values = re.findall(r'id="Inf\d+">([^<]*)</td>', page)
        assert values == ["-"] * 9
        assert "Must not be added" not in page
        assert "親レポート参照" not in page
        assert "NoNeed" not in page


def test_empty_project_can_export_without_selecting_cases(monkeypatch):
    monkeypatch.setattr(reports, "_project_cases", lambda project_id: [])
    stream, _ = build_qualification_report(SimpleNamespace(id=7, name="SYS"))
    with zipfile.ZipFile(stream) as archive:
        assert len(archive.namelist()) == 2
        assert 'id="testall">0</td>' in archive.read(archive.namelist()[0]).decode()


def test_pinned_report_uses_snapshot_and_rejects_integrity_failures(tmp_path, monkeypatch):
    from test_run_evidence import prepared, service

    selected, source, _ = prepared(tmp_path)
    selected.task_name = "Mutable task name"
    selected.result = "FAIL"
    selected.submitter = "Owner"
    selected.finished_at = None
    evidence = service()
    root = evidence.pin_attempt(selected, source, approved_inputs={"row": {
        "test_name": "Approved name", "purpose": "Approved purpose", "category_name": "Approved category"}})
    (root / "results").mkdir()
    (root / "results" / "jdgrslt.log").write_text(
        "INFO:root:Test case ID.TC-1 is started!\nINFO:root:All steps are verified.Test is Passed.", encoding="utf-8")
    evidence.seal_attempt(selected, status="passed", verdict="PASS", message="", evidence_kind="mock", runner_backend="mock")
    before = {path: path.read_bytes() for path in root.rglob("*") if path.is_file()}
    selected.sil_version = "mutated-version"
    monkeypatch.setattr(reports, "_project_cases", lambda project_id: [reports._case(selected)])
    project = SimpleNamespace(id=7, name="SYS")
    stream, _ = build_qualification_report(project)
    with zipfile.ZipFile(stream) as archive:
        detail = archive.read(archive.namelist()[1]).decode()
    assert "Approved name" in detail and "Approved purpose" in detail
    assert "Approved category" in detail and "Evidence: mock" not in detail
    assert "Case TC-1 : Passed" in detail
    assert "mutated-version" not in detail
    assert before == {path: path.read_bytes() for path in root.rglob("*") if path.is_file()}
    (root / "results" / "jdgrslt.log").write_text("tampered", encoding="utf-8")
    with pytest.raises(evidence.EvidenceError, match="integrity"):
        build_qualification_report(project)


def test_unsealed_attempt_cannot_inherit_a_live_pass_or_missing_final_outcome(tmp_path, monkeypatch):
    from test_run_evidence import prepared, service

    selected, source, _ = prepared(tmp_path)
    selected.task_name = "Task"
    selected.result = "PASS"
    selected.submitter = "Owner"
    selected.finished_at = None
    service().pin_attempt(selected, source)
    monkeypatch.setattr(reports, "_project_cases", lambda project_id: [reports._case(selected)])
    project = SimpleNamespace(id=7, name="SYS")
    stream, _ = build_qualification_report(project)
    with zipfile.ZipFile(stream) as archive:
        assert "Test Untested" in archive.read(archive.namelist()[0]).decode()
    selected.status = "passed"
    with pytest.raises(service().EvidenceError, match="no sealed outcome"):
        build_qualification_report(project)
