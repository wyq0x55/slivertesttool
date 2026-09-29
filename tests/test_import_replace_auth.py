"""replace_all import commit must require import.replace again.

Editor has import.run but not import.replace. A project admin can create a
replace_all preview; commit must still reject the editor and leave rows intact.
"""
from __future__ import annotations

import importlib
import os
import tempfile
import uuid

import pytest

EDITOR_PASSWORD = "editor-pass"
ADMIN_PASSWORD = "padmin-pass"
_ENV_KEYS = (
    "DATABASE_URL",
    "TEST_DATABASE_URL",
    "HUEY_DATABASE_URL",
    "SECRET_KEY",
    "SILVER_POOL_ENABLED",
    "SILVER_POOL_PREWARM",
    "SILVER_GUI",
)


@pytest.fixture(scope="module")
def app():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    saved = {key: os.environ.get(key) for key in _ENV_KEYS}
    sqlite_url = "sqlite:///" + path
    os.environ["DATABASE_URL"] = sqlite_url
    os.environ.pop("TEST_DATABASE_URL", None)
    os.environ["HUEY_DATABASE_URL"] = sqlite_url
    os.environ["SECRET_KEY"] = "import-replace-auth-test"
    os.environ["SILVER_POOL_ENABLED"] = "0"
    os.environ["SILVER_POOL_PREWARM"] = "0"
    os.environ["SILVER_GUI"] = "0"

    import app.config as config_mod
    import app as app_pkg

    importlib.reload(config_mod)
    importlib.reload(app_pkg)

    application = app_pkg.create_app()
    from app.bootstrap import bootstrap_app

    bootstrap_app(application)
    with application.app_context():
        yield application

    for key, value in saved.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value
    try:
        os.unlink(path)
    except OSError:
        pass


@pytest.fixture
def client(app):
    return app.test_client()


@pytest.fixture
def replace_case(app):
    from app.extensions import db
    from app.models import (
        AuditLog, DataJob, FieldDefinition, LMUser, Project, ProjectMember,
        TestItemRow,
    )

    suffix = uuid.uuid4().hex[:10]
    editor = LMUser(
        username="ed" + suffix, display_name="Editor", is_system_admin=False,
    )
    editor.set_password(EDITOR_PASSWORD)
    admin = LMUser(
        username="pa" + suffix, display_name="Project Admin",
        is_system_admin=False,
    )
    admin.set_password(ADMIN_PASSWORD)
    db.session.add_all([editor, admin])
    db.session.commit()

    project = Project(
        code=("R" + suffix)[:32], name="Replace auth",
        owner_id=admin.id, created_by=admin.id,
    )
    db.session.add(project)
    db.session.commit()
    keep_case_id = "KEEP-" + suffix
    new_case_id = "NEW-" + suffix
    db.session.add_all([
        ProjectMember(
            project_id=project.id, user_id=admin.id, role="project_admin",
        ),
        ProjectMember(
            project_id=project.id, user_id=editor.id, role="editor",
        ),
        FieldDefinition(
            project_id=project.id, field_key="case_id", display_name="case_id",
            data_type="text", sheet="test", is_system=True, is_active=True,
        ),
    ])
    keep = TestItemRow(
        project_id=project.id, case_id=keep_case_id, title="existing",
        row_order=1, created_by=admin.id, updated_by=admin.id,
    )
    db.session.add(keep)
    db.session.commit()
    job = DataJob(
        project_id=project.id, job_type="import", status="previewed",
        original_filename="replace.xlsx",
        parameters={"mode": "replace_all"},
        preview={
            "mode": "replace_all",
            "total": 1,
            "valid": 1,
            "invalid": 0,
            "insert": 1,
            "update": 0,
            "skip": 0,
            "unmapped": [],
            "errors": [],
            "rows": [{
                "row": 2,
                "values": {"case_id": new_case_id},
                "is_update": False,
            }],
        },
        total_count=1, error_count=0, created_by=admin.id,
    )
    db.session.add(job)
    db.session.commit()
    case = {
        "project_id": project.id,
        "job_id": job.id,
        "keep_row_id": keep.id,
        "keep_case_id": keep_case_id,
        "new_case_id": new_case_id,
        "editor_id": editor.id,
        "admin_id": admin.id,
        "editor_username": editor.username,
        "admin_username": admin.username,
    }
    try:
        yield case
    finally:
        db.session.rollback()
        AuditLog.query.filter_by(project_id=case["project_id"]).delete()
        DataJob.query.filter_by(project_id=case["project_id"]).delete()
        TestItemRow.query.filter_by(project_id=case["project_id"]).delete()
        FieldDefinition.query.filter_by(project_id=case["project_id"]).delete()
        ProjectMember.query.filter_by(project_id=case["project_id"]).delete()
        project_row = db.session.get(Project, case["project_id"])
        editor_row = db.session.get(LMUser, case["editor_id"])
        admin_row = db.session.get(LMUser, case["admin_id"])
        if project_row is not None:
            db.session.delete(project_row)
        if editor_row is not None:
            db.session.delete(editor_row)
        if admin_row is not None:
            db.session.delete(admin_row)
        db.session.commit()
        db.session.expunge_all()


def _login(client, username, password):
    response = client.post(
        "/api/v1/auth/login",
        json={"username": username, "password": password},
    )
    assert response.status_code == 200, response.get_data(as_text=True)
    token = response.get_json()["data"]["csrf_token"]
    return {"X-CSRF-Token": token}


def _live_case_ids(project_id):
    from app.extensions import db
    from app.models import TestItemRow

    db.session.expire_all()
    rows = (
        TestItemRow.query.filter_by(project_id=project_id, deleted_at=None)
        .order_by(TestItemRow.id.asc())
        .all()
    )
    return [row.case_id for row in rows]


def test_editor_commit_replace_all_is_forbidden(client, replace_case):
    headers = _login(client, replace_case["editor_username"], EDITOR_PASSWORD)
    response = client.post(
        "/api/v1/imports/%s/commit" % replace_case["job_id"],
        headers=headers,
    )
    body = response.get_json()
    assert response.status_code == 403
    assert body["success"] is False
    assert body["error"]["code"] == "PERMISSION_DENIED"
    assert _live_case_ids(replace_case["project_id"]) == [
        replace_case["keep_case_id"],
    ]

    from app.extensions import db
    from app.models import DataJob

    db.session.expire_all()
    job = db.session.get(DataJob, replace_case["job_id"])
    assert job.status == "previewed"


def test_service_blocks_editor_replace_all_without_the_route(app, replace_case):
    from app.extensions import db
    from app.models import DataJob, LMUser, Project, TestItemRow
    from app.services.lanmatrix.excel_service import commit_import
    from app.services.lanmatrix.permissions import PermissionDenied

    editor = db.session.get(LMUser, replace_case["editor_id"])
    project = db.session.get(Project, replace_case["project_id"])
    job = db.session.get(DataJob, replace_case["job_id"])
    with pytest.raises(PermissionDenied) as caught:
        commit_import(editor, project, job)
    assert str(caught.value) == "import.replace"

    db.session.expire_all()
    keep = db.session.get(TestItemRow, replace_case["keep_row_id"])
    assert keep.deleted_at is None
    job = db.session.get(DataJob, replace_case["job_id"])
    assert job.status == "previewed"


def test_route_checks_replace_before_calling_service(
    monkeypatch, client, replace_case,
):
    called = {"n": 0}

    def _must_not_run(*args, **kwargs):
        called["n"] += 1
        raise AssertionError("commit_import must not run")

    monkeypatch.setattr(
        "app.routes.lanmatrix.imports_exports.excel_service.commit_import",
        _must_not_run,
    )
    headers = _login(client, replace_case["editor_username"], EDITOR_PASSWORD)
    response = client.post(
        "/api/v1/imports/%s/commit" % replace_case["job_id"],
        headers=headers,
    )
    assert response.status_code == 403
    assert response.get_json()["error"]["code"] == "PERMISSION_DENIED"
    assert called["n"] == 0
    assert _live_case_ids(replace_case["project_id"]) == [
        replace_case["keep_case_id"],
    ]


def test_project_admin_commit_replace_all_is_allowed(client, replace_case):
    headers = _login(client, replace_case["admin_username"], ADMIN_PASSWORD)
    response = client.post(
        "/api/v1/imports/%s/commit" % replace_case["job_id"],
        headers=headers,
    )
    body = response.get_json()
    assert response.status_code == 200
    assert body["success"] is True
    assert body["error"] is None
    assert body["data"] == {"inserted": 1, "updated": 0}
    assert _live_case_ids(replace_case["project_id"]) == [
        replace_case["new_case_id"],
    ]
