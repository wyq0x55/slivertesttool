from __future__ import annotations

import csv
import io
import os
import subprocess
import uuid
import zipfile
from pathlib import Path

import pytest


@pytest.fixture
def report_env(app_ctx, client, tmp_path):
    from app.extensions import db
    from app.models import LMUser, Project, ProjectMember, Task, TestItemRow

    suffix = uuid.uuid4().hex[:10]
    workspace = tmp_path / "workspace"
    with app_ctx.app_context():
        owner = LMUser(username=f"ro{suffix}", display_name="Owner",
                       password_hash="x")
        reader = LMUser(username=f"rr{suffix}", display_name="Reader",
                        password_hash="x")
        outsider = LMUser(username=f"rx{suffix}", display_name="Outsider",
                          password_hash="x")
        db.session.add_all([owner, reader, outsider])
        db.session.commit()

        project = Project(code=f"RP{suffix}", name="Reports", owner_id=owner.id)
        other_project = Project(code=f"RO{suffix}", name="Other", owner_id=owner.id)
        db.session.add_all([project, other_project])
        db.session.commit()
        db.session.add(ProjectMember(project_id=project.id, user_id=reader.id,
                                     role="reader"))

        first_key = f"T{suffix}1"
        second_key = f"T{suffix}2"
        foreign_key = f"T{suffix}3"
        db.session.add_all([
            Task(task_key=first_key, project_id=project.id, test_id="TC-1",
                 workspace=str(workspace), submitter="reader"),
            Task(task_key=second_key, project_id=project.id, test_id="TC-2",
                 workspace=str(workspace), submitter="reader"),
            Task(task_key=foreign_key, project_id=other_project.id,
                 test_id="TC-3", workspace=str(workspace), submitter="owner"),
        ])
        db.session.add_all([
            TestItemRow(project_id=project.id, sheet="test", row_order=1,
                        case_id="LEGACY-1", title="First", custom_values={"test_id": "TC-1"}),
            TestItemRow(project_id=project.id, sheet="test", row_order=2,
                        case_id="TC-2", title="Second"),
            TestItemRow(project_id=project.id, sheet="test", row_order=3,
                        case_id="TC-4", title="Never run"),
            TestItemRow(project_id=project.id, sheet="test", row_order=4,
                        case_id="TC-5", title="Not required", custom_values={"item_created": "不要"}),
            TestItemRow(project_id=other_project.id, sheet="test", row_order=1,
                        case_id="TC-3", title="Other project"),
        ])
        db.session.commit()
        project_id = project.id
        other_project_id = other_project.id
        reader_id = reader.id
        outsider_id = outsider.id

    with client.session_transaction() as session:
        session["lm_user_id"] = reader_id

    return {
        "client": client,
        "workspace": workspace,
        "project_id": project_id,
        "other_project_id": other_project_id,
        "reader_id": reader_id,
        "outsider_id": outsider_id,
        "first_key": first_key,
        "second_key": second_key,
        "foreign_key": foreign_key,
    }


def _log_dir(env, test_id: str) -> Path:
    from app.runners import run_layout

    return run_layout.log_dir(env["workspace"], test_id)


def _download_url(project_id: int, task_key: str) -> str:
    return f"/api/v1/projects/{project_id}/tasks/{task_key}/download"


def test_reader_can_export_whole_project_without_selecting_any_cases(report_env):
    response = report_env["client"].get(
        f"/api/v1/projects/{report_env['project_id']}/tasks/qualification-report",
    )
    assert response.status_code == 200
    assert response.mimetype == "application/zip"
    with zipfile.ZipFile(io.BytesIO(response.data)) as archive:
        assert len(archive.namelist()) == 2
        detail = archive.read(archive.namelist()[1]).decode("utf-8")
        assert "Case TC-1 : Untested" in detail
        assert "Case TC-2 : Untested" in detail
        assert "Case TC-4 : Untested" in detail
        assert "Case TC-5 : Untested" in detail
        assert 'id="testall">4</td>' in archive.read(archive.namelist()[0]).decode()
        assert "TC-3" not in detail
        assert detail.count("Case TC-1 : Untested") == 1
    single = report_env["client"].get(
        f"/api/v1/projects/{report_env['project_id']}/tasks/{report_env['first_key']}/qualification-report")
    assert single.status_code == 200
    with zipfile.ZipFile(io.BytesIO(single.data)) as archive:
        assert "Case TC-5 : Untested" in archive.read(archive.namelist()[1]).decode()


def test_qualification_export_checks_membership_and_ignores_selection_filters(report_env):
    url = f"/api/v1/projects/{report_env['project_id']}/tasks/qualification-report"
    client = report_env["client"]
    response = client.get(url, query_string={"keys": report_env["foreign_key"]})
    assert response.status_code == 200
    with zipfile.ZipFile(io.BytesIO(response.data)) as archive:
        detail = archive.read(archive.namelist()[1]).decode()
        assert "Case TC-4 : Untested" in detail
        assert "TC-3" not in detail
    with client.session_transaction() as session:
        session["lm_user_id"] = report_env["outsider_id"]
    assert client.get(url, query_string={"keys": report_env["first_key"]}).status_code == 403


def test_qualification_export_rejects_invalid_evidence(report_env, monkeypatch):
    from app.services import qualification_report_service, run_evidence_service

    def invalid(*args):
        raise run_evidence_service.EvidenceError("Invalid sealed attempt")

    monkeypatch.setattr(qualification_report_service, "build_qualification_report", invalid)
    response = report_env["client"].get(
        f"/api/v1/projects/{report_env['project_id']}/tasks/{report_env['first_key']}/qualification-report")
    assert response.status_code == 409
    assert response.json["error"]["code"] == "EVIDENCE_INVALID"


def test_project_report_combines_results_with_unrun_and_not_required_cases(report_env, app_ctx):
    from app.extensions import db
    from app.models import Task

    with app_ctx.app_context():
        for key, status, verdict in ((report_env["first_key"], "passed", "PASS"),
                                     (report_env["second_key"], "failed", "FAIL")):
            selected = Task.query.filter_by(task_key=key).one()
            selected.status, selected.result = status, verdict
        db.session.commit()
    log_dir = _log_dir(report_env, "TC-1")
    log_dir.mkdir(parents=True)
    (log_dir / "jdgrslt.log").write_text(
        "INFO:root:Test case ID.ID;;TC-1 is started!\n"
        "INFO:root:Step.1 is passed at 0.1s.\n"
        "INFO:root:All steps are verified.Test is Passed.", encoding="utf-8")
    response = report_env["client"].get(
        f"/api/v1/projects/{report_env['project_id']}/tasks/qualification-report")
    assert response.status_code == 200
    with zipfile.ZipFile(io.BytesIO(response.data)) as archive:
        overview, detail = [archive.read(name).decode() for name in archive.namelist()]
    assert 'id="testall">4</td>' in overview
    assert 'id="test1-1">2</td>' in overview
    assert 'id="test2-1">2</td>' in overview
    assert "Case TC-1 : Passed" in detail
    assert "Case TC-2 : Failed" in detail
    assert "Case TC-4 : Untested" in detail and "Case TC-5 : Untested" in detail
    assert "0.1s" in detail
    assert "Evidence:" not in overview + detail


def test_project_report_uses_latest_live_task_without_reviving_a_pending_retest(report_env, app_ctx):
    from datetime import datetime
    from app.extensions import db
    from app.models import Task

    with app_ctx.app_context():
        first = Task.query.filter_by(task_key=report_env["first_key"]).one()
        first.status, first.result = "passed", "PASS"
        newer = Task(task_key="T-RETEST", project_id=report_env["project_id"], test_id="TC-1",
                     status="queued", result="PASS", workspace="")
        db.session.add(newer)
        db.session.add(Task(task_key="T-FOREIGN", project_id=report_env["other_project_id"],
                            test_id="TC-1", status="failed", result="FAIL"))
        db.session.commit()
    url = f"/api/v1/projects/{report_env['project_id']}/tasks/qualification-report"
    response = report_env["client"].get(url)
    assert response.status_code == 200
    with zipfile.ZipFile(io.BytesIO(response.data)) as archive:
        detail = archive.read(archive.namelist()[1]).decode()
        assert "Case TC-1 : Untested" in detail
        assert "Case TC-1 : Passed" not in detail
    with app_ctx.app_context():
        Task.query.filter_by(task_key="T-RETEST").one().deleted_at = datetime.now()
        db.session.commit()
    response = report_env["client"].get(url)
    with zipfile.ZipFile(io.BytesIO(response.data)) as archive:
        assert "Case TC-1 : Passed" in archive.read(archive.namelist()[1]).decode()


def test_project_report_includes_all_rows_beyond_task_window_without_extra_sheets(report_env, app_ctx):
    from datetime import datetime
    from app.extensions import db
    from app.models import Task, TestItemRow

    with app_ctx.app_context():
        pid = report_env["project_id"]
        db.session.add_all([TestItemRow(project_id=pid, sheet="test", row_order=i + 10,
                                       case_id=f"UNRUN-{i}") for i in range(205)])
        db.session.add_all([
            TestItemRow(project_id=pid, sheet="test", row_order=215, case_id="TC-4"),
            TestItemRow(project_id=pid, sheet="test", row_order=216, case_id=""),
            TestItemRow(project_id=pid, sheet="test", row_order=217, case_id="DELETED", deleted_at=datetime.now()),
            *[TestItemRow(project_id=pid, sheet=sheet, row_order=1, case_id=f"EXCLUDED-{sheet}")
              for sheet in ("const", "lib", "io")],
            Task(task_key="T-ORPHAN", project_id=pid, test_id="ORPHAN", status="failed", result="FAIL"),
        ])
        db.session.commit()
    response = report_env["client"].get(
        f"/api/v1/projects/{report_env['project_id']}/tasks/qualification-report")
    assert response.status_code == 200
    with zipfile.ZipFile(io.BytesIO(response.data)) as archive:
        overview, detail = [archive.read(name).decode() for name in archive.namelist()]
    assert 'id="testall">211</td>' in overview
    assert detail.count('<table class="TestCaseTable">') == 211
    assert "Case UNRUN-204 : Untested" in detail
    assert detail.count("Case TC-4 : Untested") == 2
    assert "Case - : Untested" in detail
    assert "DELETED" not in detail and "EXCLUDED-" not in detail and "ORPHAN" not in detail


def test_reader_can_download_single_report_without_stored_report_zip(report_env):
    log_dir = _log_dir(report_env, "TC-1")
    log_dir.mkdir(parents=True)
    (log_dir / "stdout.log").write_text("run output", encoding="utf-8")
    (log_dir / "report.zip").write_bytes(b"stale archive")

    response = report_env["client"].get(
        _download_url(report_env["project_id"], report_env["first_key"])
    )

    assert response.status_code == 200
    assert response.mimetype == "application/zip"
    assert "TC-1_report.zip" in response.headers["Content-Disposition"]
    with zipfile.ZipFile(io.BytesIO(response.data)) as archive:
        assert archive.namelist() == ["log/TC-1/stdout.log", "all.csv"]
        assert archive.read("log/TC-1/stdout.log") == b"run output"
        assert list(csv.reader(io.StringIO(archive.read("all.csv").decode("utf-8-sig")))) == [
            ["Test_No.", "Result", "Path"], ["TC-1", "-", "log/TC-1"],
        ]


def test_batch_report_download_is_project_scoped_and_contains_selected_files(report_env):
    for test_id, content in (("TC-1", "first"), ("TC-2", "second"),
                             ("TC-3", "foreign")):
        log_dir = _log_dir(report_env, test_id)
        log_dir.mkdir(parents=True)
        (log_dir / "stdout.log").write_text(content, encoding="utf-8")

    response = report_env["client"].get(
        f"/api/v1/projects/{report_env['project_id']}/tasks/download_batch",
        query_string={
            "keys": [report_env["first_key"], report_env["second_key"],
                     report_env["foreign_key"]]
        },
    )

    assert response.status_code == 200
    with zipfile.ZipFile(io.BytesIO(response.data)) as archive:
        assert archive.namelist() == ["log/TC-1/stdout.log", "log/TC-2/stdout.log", "all.csv"]
        assert archive.read("log/TC-1/stdout.log") == b"first"
        assert archive.read("log/TC-2/stdout.log") == b"second"
        assert list(csv.reader(io.StringIO(archive.read("all.csv").decode("utf-8-sig")))) == [
            ["Test_No.", "Result", "Path"],
            ["TC-1", "-", "log/TC-1"], ["TC-2", "-", "log/TC-2"],
        ]


def test_report_download_requires_project_membership(report_env):
    with report_env["client"].session_transaction() as session:
        session["lm_user_id"] = report_env["outsider_id"]

    response = report_env["client"].get(
        _download_url(report_env["project_id"], report_env["first_key"])
    )

    assert response.status_code == 403


def test_result_bundle_preserves_silver_ids_verdicts_and_relative_paths(tmp_path):
    from types import SimpleNamespace
    from app.runners import run_layout
    from app.services import report_service

    ids = ["ID;;MWCPD-TY-SWEL.6-TC_ALGO-001001", "ID;;測試,002", "ID;;TC-003"]
    tasks = []
    for i, (test_id, verdict) in enumerate(zip(ids, ["PASS", "FAIL", "NoNeed"])):
        task = SimpleNamespace(task_key=f"T{i}", test_id=test_id, workspace=str(tmp_path),
                               result=verdict, run_count=1, report_path="")
        tasks.append(task)
        if i < 2:
            log_dir = run_layout.log_dir(tmp_path, test_id)
            (log_dir / "nested").mkdir(parents=True)
            (log_dir / "nested" / "stdout.log").write_text(verdict, encoding="utf-8")

    buffer, added = report_service.build_reports_stream(tasks)
    assert added == 2
    with zipfile.ZipFile(buffer) as archive:
        rows = list(csv.DictReader(io.StringIO(archive.read("all.csv").decode("utf-8-sig"))))
        assert rows == [
            {"Test_No.": ids[0], "Result": "PASS", "Path": f"log/{ids[0]}"},
            {"Test_No.": ids[1], "Result": "FAIL", "Path": f"log/{ids[1]}"},
            {"Test_No.": ids[2], "Result": "NoNeed", "Path": "-"},
        ]
        assert set(archive.namelist()) == {"all.csv", *[
            f"log/{test_id}/nested/stdout.log" for test_id in ids[:2]
        ]}
        for row in rows[:2]:
            assert not Path(row["Path"]).is_absolute()
            assert archive.read(row["Path"] + "/nested/stdout.log").decode() == row["Result"]


def test_result_bundle_allocates_unique_folders_even_for_suffix_and_case_collisions(tmp_path):
    from types import SimpleNamespace
    from app.runners import run_layout
    from app.services import report_service

    tasks = []
    for i, test_id in enumerate(["A", "A", "A_1", "a"]):
        log_dir = run_layout.log_dir(tmp_path, test_id)
        log_dir.mkdir(exist_ok=True, parents=True)
        (log_dir / "stdout.log").write_text("output", encoding="utf-8")
        tasks.append(SimpleNamespace(task_key=f"T{i}", test_id=test_id, workspace=str(tmp_path),
                                     result="PASS", run_count=1, report_path=""))
    buffer, added = report_service.build_reports_stream(tasks)
    assert added == 4
    with zipfile.ZipFile(buffer) as archive:
        rows = list(csv.DictReader(io.StringIO(archive.read("all.csv").decode("utf-8-sig"))))
        assert [row["Path"] for row in rows] == ["log/A", "log/A_1", "log/A_1_1", "log/a_2"]
        assert len({name.casefold() for name in archive.namelist()}) == 5
        assert all(row["Path"] + "/stdout.log" in archive.namelist() for row in rows)


def test_batch_download_deduplicates_keys_and_rejects_selection_without_results(report_env):
    url = f"/api/v1/projects/{report_env['project_id']}/tasks/download_batch"
    client = report_env["client"]
    assert client.get(url, query_string={"keys": report_env["first_key"]}).status_code == 404
    assert client.get(url, query_string={"keys": report_env["foreign_key"]}).status_code == 404
    log_dir = _log_dir(report_env, "TC-1")
    log_dir.mkdir(parents=True)
    (log_dir / "stdout.log").write_text("output", encoding="utf-8")
    response = client.get(url, query_string={"keys": [
        report_env["first_key"], report_env["first_key"], report_env["second_key"],
    ]})
    assert response.status_code == 200
    with zipfile.ZipFile(io.BytesIO(response.data)) as archive:
        rows = list(csv.reader(io.StringIO(archive.read("all.csv").decode("utf-8-sig"))))
        assert rows == [["Test_No.", "Result", "Path"],
                        ["TC-1", "-", "log/TC-1"], ["TC-2", "-", "-"]]


def test_report_download_does_not_follow_symlinks_outside_result_tree(
    report_env, tmp_path
):
    log_dir = _log_dir(report_env, "TC-1")
    log_dir.mkdir(parents=True)
    (log_dir / "stdout.log").write_text("run output", encoding="utf-8")
    external_file = tmp_path / "private.txt"
    external_file.write_text("private host data", encoding="utf-8")
    if os.name == "nt":
        external_dir = tmp_path / "outside-result-tree"
        external_dir.mkdir()
        (external_dir / "private.txt").write_text(
            "private host data", encoding="utf-8"
        )
        link = log_dir / "external"
        linked = subprocess.run(
            ["cmd.exe", "/c", "mklink", "/J", str(link), str(external_dir)],
            capture_output=True,
            text=True,
        )
        if linked.returncode:
            pytest.skip("directory junctions are unavailable")
        leaked_name = "log/TC-1/external/private.txt"
    else:
        link = log_dir / "private.txt"
        try:
            link.symlink_to(external_file)
        except (OSError, NotImplementedError) as exc:
            pytest.skip(f"file symlinks are unavailable: {exc}")
        leaked_name = "log/TC-1/private.txt"

    try:
        response = report_env["client"].get(
            _download_url(report_env["project_id"], report_env["first_key"])
        )

        assert response.status_code == 200
        with zipfile.ZipFile(io.BytesIO(response.data)) as archive:
            assert leaked_name not in archive.namelist()
    finally:
        if os.name == "nt" and link.exists():
            link.rmdir()


def test_report_download_rejects_a_result_directory_link(report_env, tmp_path):
    log_root = report_env["workspace"] / "log"
    log_root.mkdir(parents=True)
    external_dir = tmp_path / "outside-result-tree"
    external_dir.mkdir()
    (external_dir / "private.txt").write_text("private data", encoding="utf-8")
    (external_dir / "jdgrslt.log").write_text("private judge data", encoding="utf-8")
    link = log_root / "TC-1"
    if os.name == "nt":
        linked = subprocess.run(
            ["cmd.exe", "/c", "mklink", "/J", str(link), str(external_dir)],
            capture_output=True,
            text=True,
        )
        if linked.returncode:
            pytest.skip("directory junctions are unavailable")
    else:
        try:
            link.symlink_to(external_dir, target_is_directory=True)
        except (OSError, NotImplementedError) as exc:
            pytest.skip(f"directory symlinks are unavailable: {exc}")

    try:
        response = report_env["client"].get(
            _download_url(report_env["project_id"], report_env["first_key"])
        )
        judge_response = report_env["client"].get(
            f"/api/v1/projects/{report_env['project_id']}/tasks/"
            f"{report_env['first_key']}/jdgrslt"
        )

        assert response.status_code == 404
        assert judge_response.status_code == 200
        assert judge_response.json["data"]["available"] is False
    finally:
        if os.name == "nt" and link.exists():
            link.rmdir()
