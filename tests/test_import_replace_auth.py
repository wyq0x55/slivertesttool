"""replace_all import commit must require import.replace again.

Editor has import.run but not import.replace. A project admin can create a
replace_all preview; commit must still reject the editor and leave rows intact.
"""
from __future__ import annotations

import importlib
import os
import tempfile
import uuid
from datetime import datetime, timedelta, timezone

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
    from app.services.lanmatrix import import_job_service

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
        parameters={
            "mode": "replace_all",
            "_snapshot": import_job_service.capture_snapshot(
                project.id, "test",
            ),
        },
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
        expires_at=datetime.now(timezone.utc) + timedelta(days=1),
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


def test_generic_import_rejects_missing_preview_expiry(app, replace_case):
    from app.extensions import db
    from app.models import DataJob, LMUser, Project
    from app.services.lanmatrix.errors import ServiceError
    from app.services.lanmatrix.excel_service import commit_import

    admin = db.session.get(LMUser, replace_case["admin_id"])
    project = db.session.get(Project, replace_case["project_id"])
    job = db.session.get(DataJob, replace_case["job_id"])
    job.expires_at = None
    db.session.commit()

    with pytest.raises(ServiceError) as caught:
        commit_import(admin, project, job)

    assert caught.value.code == "IMPORT_PREVIEW_INVALID"
    assert _live_case_ids(replace_case["project_id"]) == [
        replace_case["keep_case_id"],
    ]


def test_generic_import_rejects_missing_preview_snapshot(app, replace_case):
    from app.extensions import db
    from app.models import DataJob, LMUser, Project
    from app.services.lanmatrix.errors import ServiceError
    from app.services.lanmatrix.excel_service import commit_import

    admin = db.session.get(LMUser, replace_case["admin_id"])
    project = db.session.get(Project, replace_case["project_id"])
    job = db.session.get(DataJob, replace_case["job_id"])
    job.parameters = {"mode": "replace_all"}
    db.session.commit()

    with pytest.raises(ServiceError) as caught:
        commit_import(admin, project, job)

    assert caught.value.code == "IMPORT_PREVIEW_INVALID"
    assert _live_case_ids(replace_case["project_id"]) == [
        replace_case["keep_case_id"],
    ]


def test_replace_all_with_invalid_rows_is_rejected_without_deleting_existing_rows(
        app, replace_case):
    from app.extensions import db
    from app.models import DataJob, LMUser, Project
    from app.services.lanmatrix.errors import ServiceError
    from app.services.lanmatrix.excel_service import commit_import

    admin = db.session.get(LMUser, replace_case["admin_id"])
    project = db.session.get(Project, replace_case["project_id"])
    job = db.session.get(DataJob, replace_case["job_id"])
    job.preview = dict(job.preview, invalid=1, errors=[{"message": "bad row"}])
    db.session.commit()

    with pytest.raises(ServiceError) as caught:
        commit_import(admin, project, job)

    assert caught.value.code == "IMPORT_HAS_ERRORS"
    assert _live_case_ids(replace_case["project_id"]) == [
        replace_case["keep_case_id"],
    ]


def _testmatrix_workbook(test_name, *, duplicate_identity=False, id_prefix=None):
    from io import BytesIO

    from app.services.lanmatrix import matrix_excel

    items = [{
        "category": 1,
        "test_no": 1,
        "test_name": test_name,
        "priority": "低",
        "result": "-",
        "steps": {
            "input_signals": [],
            "expected_signals": [],
            "steps": [],
        },
    }]
    if duplicate_identity:
        items.append({**items[0], "test_name": f"{test_name} duplicate"})
    workbook = matrix_excel.build_workbook({
        "name": "unit",
        "source_filename": "matrix.xlsx",
        "summary_sheet": matrix_excel.DEFAULT_SUMMARY_SHEET,
        "id_prefix": id_prefix or matrix_excel.DEFAULT_ID_PREFIX,
        "items": items,
    })
    source = BytesIO()
    workbook.save(source)
    source.seek(0)
    return source


def _special_import_workbook(import_format):
    from io import BytesIO

    if import_format == "test_matrix":
        return _testmatrix_workbook("preview only")
    if import_format == "libfunc":
        from openpyxl import Workbook

        workbook = Workbook()
        sheet = workbook.active
        sheet.append(["lib_func", "lib_name", "lib_value"])
        sheet.append(["FUNC-PREVIEW", "Function", "Purpose"])
    elif import_format == "const":
        from app.services.lanmatrix import const_excel

        workbook = const_excel.build_workbook({"items": [{
            "const_name": "CONST-PREVIEW",
            "const_jname": "Preview",
            "const_value": "1",
        }]})
    else:
        from app.services.lanmatrix import io_excel

        workbook = io_excel.build_workbook({"items": [{
            "io_name": "IO-PREVIEW",
            "io_path": "ecu.preview",
            "io_note": "preview",
        }]})
    source = BytesIO()
    workbook.save(source)
    source.seek(0)
    return source


def test_testmatrix_replace_all_only_replaces_test_sheet(app, replace_case):
    from app.extensions import db
    from app.models import LMUser, Project, TestItemRow
    from app.services.lanmatrix import testmatrix_bridge

    project = db.session.get(Project, replace_case["project_id"])
    admin = db.session.get(LMUser, replace_case["admin_id"])
    other_rows = [
        TestItemRow(
            project_id=project.id, sheet=sheet, case_id=f"{sheet}-keep",
            title=sheet, row_order=index, created_by=admin.id,
            updated_by=admin.id,
        )
        for index, sheet in enumerate(("lib", "const", "io"), start=2)
    ]
    db.session.add_all(other_rows)
    db.session.commit()

    summary = testmatrix_bridge.import_workbook(
        admin, project, _testmatrix_workbook("replacement"),
        mode="replace_all", original_filename="matrix.xlsx",
    )

    db.session.expire_all()
    assert summary["deleted"] == 1
    assert all(db.session.get(TestItemRow, row.id).deleted_at is None
               for row in other_rows)
    assert TestItemRow.query.filter_by(
        project_id=project.id, sheet="test", deleted_at=None,
    ).count() == 1


def test_testmatrix_upsert_does_not_match_rows_on_other_sheets(app, replace_case):
    from app.extensions import db
    from app.models import LMUser, Project, TestItemRow
    from app.services.lanmatrix import matrix_excel, testmatrix_bridge

    project = db.session.get(Project, replace_case["project_id"])
    admin = db.session.get(LMUser, replace_case["admin_id"])
    case_id = testmatrix_bridge.reconstruct_case_id(
        matrix_excel.DEFAULT_ID_PREFIX, {"category": 1, "test_no": 1},
    )
    library_row = TestItemRow(
        project_id=project.id, sheet="lib", case_id=case_id,
        title="library row", row_order=2, created_by=admin.id,
        updated_by=admin.id,
    )
    db.session.add(library_row)
    db.session.commit()

    summary = testmatrix_bridge.import_workbook(
        admin, project, _testmatrix_workbook("new test row"),
        mode="upsert", original_filename="matrix.xlsx",
    )

    db.session.expire_all()
    assert summary["created"] == 1
    assert summary["updated"] == 0
    assert db.session.get(TestItemRow, library_row.id).title == "library row"
    assert TestItemRow.query.filter_by(
        project_id=project.id, sheet="test", case_id=case_id,
        deleted_at=None,
    ).count() == 1


def test_testmatrix_import_preview_does_not_mutate_project(client, replace_case):
    from app.extensions import db
    from app.models import FieldDefinition, Project

    headers = _login(
        client, replace_case["admin_username"], ADMIN_PASSWORD,
    )
    project_id = replace_case["project_id"]
    before_rows = _live_case_ids(project_id)
    before_fields = FieldDefinition.query.filter_by(project_id=project_id).count()
    project = db.session.get(Project, project_id)
    before_meta = (project.tm_id_prefix, project.tm_summary_sheet)

    response = client.post(
        f"/api/v1/projects/{project_id}/testmatrix/import",
        data={"file": (_testmatrix_workbook("preview only"), "matrix.xlsx"),
              "mode": "upsert"},
        headers=headers,
        content_type="multipart/form-data",
    )

    assert response.status_code == 201
    assert _live_case_ids(project_id) == before_rows
    assert FieldDefinition.query.filter_by(project_id=project_id).count() == before_fields
    db.session.expire(project)
    assert (project.tm_id_prefix, project.tm_summary_sheet) == before_meta
    job = response.get_json()["data"]["job"]
    assert job["job_type"] == "import"
    assert job["status"] == "previewed"
    assert job["parameters"]["format"] == "test_matrix"
    assert job["preview"]["total"] == 1
    assert job["preview"]["invalid"] == 0


def test_special_import_commit_route_accepts_preview_job(client, replace_case):
    headers = _login(
        client, replace_case["admin_username"], ADMIN_PASSWORD,
    )
    project_id = replace_case["project_id"]
    preview_response = client.post(
        f"/api/v1/projects/{project_id}/testmatrix/import",
        data={"file": (_testmatrix_workbook("route commit"), "matrix.xlsx"),
              "mode": "upsert"},
        headers=headers,
        content_type="multipart/form-data",
    )

    assert preview_response.status_code == 201
    job = preview_response.get_json()["data"]["job"]
    response = client.post(
        f"/api/v1/imports/{job['id']}/commit", headers=headers,
    )

    assert response.status_code == 200
    assert response.get_json()["data"] == {
        "inserted": 1,
        "updated": 0,
        "deleted": 0,
        "mode": "upsert",
        "format": "test_matrix",
    }
    assert set(_live_case_ids(project_id)) == {
        replace_case["keep_case_id"], job["preview"]["rows"][0]["identity"],
    }


@pytest.mark.parametrize("import_format", ("libfunc", "const", "io"))
def test_other_special_import_previews_do_not_mutate_project(
        client, replace_case, import_format):
    from app.extensions import db
    from app.models import DataJob, FieldDefinition, Project, TestItemRow

    headers = _login(
        client, replace_case["admin_username"], ADMIN_PASSWORD,
    )
    project_id = replace_case["project_id"]
    before_items = TestItemRow.query.filter_by(project_id=project_id).count()
    before_fields = FieldDefinition.query.filter_by(project_id=project_id).count()
    project = db.session.get(Project, project_id)
    before_meta = (project.tm_id_prefix, project.tm_summary_sheet)
    route_format = {"libfunc": "libfunc", "const": "const", "io": "io"}[
        import_format
    ]

    response = client.post(
        f"/api/v1/projects/{project_id}/{route_format}/import",
        data={"file": (_special_import_workbook(import_format), "data.xlsx"),
              "mode": "upsert"},
        headers=headers,
        content_type="multipart/form-data",
    )

    assert response.status_code == 201
    job = response.get_json()["data"]["job"]
    assert job["job_type"] == "import"
    assert job["status"] == "previewed"
    assert job["parameters"]["format"] == import_format
    assert job["preview"]["total"] == 1
    assert job["preview"]["invalid"] == 0
    assert "_snapshot" not in job["parameters"]
    assert TestItemRow.query.filter_by(project_id=project_id).count() == before_items
    assert FieldDefinition.query.filter_by(project_id=project_id).count() == before_fields
    db.session.expire(project)
    assert (project.tm_id_prefix, project.tm_summary_sheet) == before_meta
    assert DataJob.query.filter_by(
        project_id=project_id, job_type="import", status="previewed",
    ).count() == 2


def test_special_replace_all_rechecks_permission_at_commit(app, replace_case):
    from app.extensions import db
    from app.models import DataJob, LMUser, Project, ProjectMember
    from app.services.lanmatrix import import_job_service
    from app.services.lanmatrix.permissions import PermissionDenied

    admin = db.session.get(LMUser, replace_case["admin_id"])
    project = db.session.get(Project, replace_case["project_id"])
    job = import_job_service.create_special_preview(
        admin, project, _testmatrix_workbook("replacement"),
        import_format="test_matrix", mode="replace_all",
    )
    membership = ProjectMember.query.filter_by(
        project_id=project.id, user_id=admin.id,
    ).one()
    membership.role = "editor"
    db.session.commit()

    with pytest.raises(PermissionDenied):
        import_job_service.commit_special_import(admin, project, job)

    assert _live_case_ids(project.id) == [replace_case["keep_case_id"]]
    db.session.expire_all()
    assert db.session.get(DataJob, job.id).status == "previewed"


def test_special_import_blocks_invalid_rows_before_replace_all(app, replace_case):
    from app.extensions import db
    from app.models import LMUser, Project
    from app.services.lanmatrix import import_job_service
    from app.services.lanmatrix.errors import ServiceError

    admin = db.session.get(LMUser, replace_case["admin_id"])
    project = db.session.get(Project, replace_case["project_id"])
    job = import_job_service.create_special_preview(
        admin, project,
        _testmatrix_workbook("duplicate", duplicate_identity=True),
        import_format="test_matrix", mode="replace_all",
    )
    assert job.preview["invalid"] > 0

    with pytest.raises(ServiceError) as caught:
        import_job_service.commit_special_import(admin, project, job)

    assert caught.value.code == "IMPORT_HAS_ERRORS"
    assert _live_case_ids(project.id) == [replace_case["keep_case_id"]]


def test_special_import_rejects_expired_preview(app, replace_case):
    from datetime import datetime, timedelta, timezone

    from app.extensions import db
    from app.models import LMUser, Project
    from app.services.lanmatrix import import_job_service
    from app.services.lanmatrix.errors import ServiceError

    admin = db.session.get(LMUser, replace_case["admin_id"])
    project = db.session.get(Project, replace_case["project_id"])
    job = import_job_service.create_special_preview(
        admin, project, _testmatrix_workbook("preview"),
        import_format="test_matrix", mode="upsert",
    )
    job.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    db.session.commit()

    with pytest.raises(ServiceError) as caught:
        import_job_service.commit_special_import(admin, project, job)

    assert caught.value.code == "IMPORT_PREVIEW_EXPIRED"
    assert _live_case_ids(project.id) == [replace_case["keep_case_id"]]


def test_special_import_rejects_snapshot_changed_before_commit(app, replace_case):
    from app.extensions import db
    from app.models import LMUser, Project, TestItemRow
    from app.services.lanmatrix import import_job_service
    from app.services.lanmatrix.errors import ServiceError

    admin = db.session.get(LMUser, replace_case["admin_id"])
    project = db.session.get(Project, replace_case["project_id"])
    job = import_job_service.create_special_preview(
        admin, project, _testmatrix_workbook("preview"),
        import_format="test_matrix", mode="upsert",
    )
    keep = db.session.get(TestItemRow, replace_case["keep_row_id"])
    keep.title = "changed after preview"
    keep.version += 1
    db.session.commit()

    with pytest.raises(ServiceError) as caught:
        import_job_service.commit_special_import(admin, project, job)

    assert caught.value.code == "IMPORT_PREVIEW_STALE"
    db.session.expire_all()
    assert db.session.get(TestItemRow, keep.id).title == "changed after preview"


def _io_workbook(io_name, io_path, io_note):
    from io import BytesIO

    from app.services.lanmatrix import io_excel

    workbook = io_excel.build_workbook({"items": [{
        "io_name": io_name,
        "io_path": io_path,
        "io_note": io_note,
    }]})
    source = BytesIO()
    workbook.save(source)
    source.seek(0)
    return source


def test_io_upsert_commits_case_insensitive_name_match(app, replace_case):
    from app.extensions import db
    from app.models import LMUser, Project, TestItemRow
    from app.services.lanmatrix import import_job_service

    admin = db.session.get(LMUser, replace_case["admin_id"])
    project = db.session.get(Project, replace_case["project_id"])
    existing = TestItemRow(
        project_id=project.id, sheet="io", case_id="Sig_A",
        row_order=1, created_by=admin.id, updated_by=admin.id,
    )
    existing.set_field("io_name", "Sig_A")
    existing.set_field("io_path", "ECU.A")
    existing.set_field("io_note", "old note")
    db.session.add(existing)
    db.session.commit()

    job = import_job_service.create_special_preview(
        admin, project, _io_workbook("sig_a", "ecu.a", "new note"),
        import_format="io", mode="upsert",
    )
    assert job.preview["invalid"] == 0
    assert job.preview["update"] == 1

    result = import_job_service.commit_special_import(admin, project, job)

    db.session.expire_all()
    existing = db.session.get(TestItemRow, existing.id)
    assert result == {
        "inserted": 0, "updated": 1, "deleted": 0,
        "mode": "upsert", "format": "io",
    }
    assert existing.case_id == "Sig_A"
    assert existing.get_field("io_name") == "sig_a"
    assert existing.get_field("io_path") == "ecu.a"
    assert existing.get_field("io_note") == "new note"


def test_io_preview_rejects_case_insensitive_path_collision(app, replace_case):
    from app.extensions import db
    from app.models import LMUser, Project, TestItemRow
    from app.services.lanmatrix import import_job_service

    admin = db.session.get(LMUser, replace_case["admin_id"])
    project = db.session.get(Project, replace_case["project_id"])
    existing = TestItemRow(
        project_id=project.id, sheet="io", case_id="Sig_A",
        row_order=1, created_by=admin.id, updated_by=admin.id,
    )
    existing.set_field("io_name", "Sig_A")
    existing.set_field("io_path", "ECU.A")
    db.session.add(existing)
    db.session.commit()

    job = import_job_service.create_special_preview(
        admin, project, _io_workbook("Sig_B", "ecu.a", "collision"),
        import_format="io", mode="upsert",
    )

    assert job.preview["invalid"] == 1
    assert job.preview["errors"][0]["field"] == "io_path"


@pytest.mark.parametrize("missing_guard", ("expires_at", "snapshot"))
def test_special_import_rejects_incomplete_preview_guard(
        app, replace_case, missing_guard):
    from app.extensions import db
    from app.models import LMUser, Project
    from app.services.lanmatrix import import_job_service
    from app.services.lanmatrix.errors import ServiceError

    admin = db.session.get(LMUser, replace_case["admin_id"])
    project = db.session.get(Project, replace_case["project_id"])
    job = import_job_service.create_special_preview(
        admin, project, _testmatrix_workbook("preview"),
        import_format="test_matrix", mode="upsert",
    )
    if missing_guard == "expires_at":
        job.expires_at = None
    else:
        parameters = dict(job.parameters)
        parameters.pop("_snapshot")
        job.parameters = parameters
    db.session.commit()

    with pytest.raises(ServiceError) as caught:
        import_job_service.commit_special_import(admin, project, job)

    assert caught.value.code == "IMPORT_PREVIEW_INVALID"
    assert _live_case_ids(project.id) == [replace_case["keep_case_id"]]


def test_special_replace_all_rejects_rows_added_after_snapshot_check(
        monkeypatch, app, replace_case):
    from app.extensions import db
    from app.models import DataJob, LMUser, Project, TestItemRow
    from app.services.lanmatrix import import_job_service
    from app.services.lanmatrix.errors import ServiceError

    admin = db.session.get(LMUser, replace_case["admin_id"])
    project = db.session.get(Project, replace_case["project_id"])
    job = import_job_service.create_special_preview(
        admin, project, _testmatrix_workbook("replacement"),
        import_format="test_matrix", mode="replace_all",
    )
    ensure_fields = import_job_service.fields_service.ensure_fields

    def insert_concurrent_row(user, current_project, definitions, *, commit=True):
        ensure_fields(user, current_project, definitions, commit=False)
        db.session.commit()
        db.session.add(TestItemRow(
            project_id=project.id, sheet="test", case_id="RACE-INSERT",
            title="concurrent insert", row_order=99,
            created_by=admin.id, updated_by=admin.id,
        ))
        db.session.commit()

    monkeypatch.setattr(
        import_job_service.fields_service, "ensure_fields", insert_concurrent_row,
    )
    with pytest.raises(ServiceError) as caught:
        import_job_service.commit_special_import(admin, project, job)

    assert caught.value.code == "IMPORT_PREVIEW_STALE"
    assert _live_case_ids(project.id) == [
        replace_case["keep_case_id"], "RACE-INSERT",
    ]
    db.session.expire_all()
    assert db.session.get(DataJob, job.id).status == "previewed"


def test_postgresql_special_import_locks_item_and_field_tables(
        monkeypatch, app):
    from types import SimpleNamespace
    from unittest.mock import Mock

    from app.extensions import db
    from app.services.lanmatrix import import_job_service

    statement = Mock()
    statement.format_table.side_effect = lambda table: table.name
    connection = SimpleNamespace(
        dialect=SimpleNamespace(
            name="postgresql", identifier_preparer=statement,
        ),
    )
    executed = []
    session = db.session()
    monkeypatch.setattr(
        session, "connection", lambda *args, **kwargs: connection,
    )
    monkeypatch.setattr(
        session, "execute",
        lambda query, *args, **kwargs: executed.append(str(query)),
    )

    import_job_service._lock_special_import_tables()

    assert executed == [
        "LOCK TABLE lm_test_items, lm_field_definitions IN SHARE ROW EXCLUSIVE MODE",
    ]


def test_special_replace_all_rolls_back_all_changes_on_commit_error(
        monkeypatch, app, replace_case):
    from app.extensions import db
    from app.models import AuditLog, FieldDefinition, LMUser, Project
    from app.services.lanmatrix import import_job_service
    from app.services.lanmatrix.errors import ServiceError

    admin = db.session.get(LMUser, replace_case["admin_id"])
    project = db.session.get(Project, replace_case["project_id"])
    job = import_job_service.create_special_preview(
        admin, project, _testmatrix_workbook("replacement", id_prefix="ROLLBACK"),
        import_format="test_matrix", mode="replace_all",
    )
    fields_before = FieldDefinition.query.filter_by(project_id=project.id).count()
    audit_before = AuditLog.query.filter_by(project_id=project.id).count()
    metadata_before = (project.tm_id_prefix, project.tm_summary_sheet)

    def fail_commit():
        raise RuntimeError("simulated commit failure")

    with monkeypatch.context() as patcher:
        patcher.setattr(db.session(), "commit", fail_commit)
        with pytest.raises(ServiceError) as caught:
            import_job_service.commit_special_import(admin, project, job)

    assert caught.value.code == "IMPORT_FAILED"
    assert _live_case_ids(project.id) == [replace_case["keep_case_id"]]
    db.session.expire_all()
    assert db.session.get(type(project), project.id).tm_id_prefix == metadata_before[0]
    assert db.session.get(type(job), job.id).status == "previewed"
    assert FieldDefinition.query.filter_by(project_id=project.id).count() == fields_before
    assert AuditLog.query.filter_by(project_id=project.id).count() == audit_before
