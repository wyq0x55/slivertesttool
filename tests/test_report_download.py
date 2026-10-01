from __future__ import annotations

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
    from app.models import LMUser, Project, ProjectMember, Task

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
        assert archive.namelist() == ["TC-1/stdout.log"]
        assert archive.read("TC-1/stdout.log") == b"run output"


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
        assert archive.namelist() == ["TC-1/stdout.log", "TC-2/stdout.log"]
        assert archive.read("TC-1/stdout.log") == b"first"
        assert archive.read("TC-2/stdout.log") == b"second"


def test_report_download_requires_project_membership(report_env):
    with report_env["client"].session_transaction() as session:
        session["lm_user_id"] = report_env["outsider_id"]

    response = report_env["client"].get(
        _download_url(report_env["project_id"], report_env["first_key"])
    )

    assert response.status_code == 403


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
        leaked_name = "TC-1/external/private.txt"
    else:
        link = log_dir / "private.txt"
        try:
            link.symlink_to(external_file)
        except (OSError, NotImplementedError) as exc:
            pytest.skip(f"file symlinks are unavailable: {exc}")
        leaked_name = "TC-1/private.txt"

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
