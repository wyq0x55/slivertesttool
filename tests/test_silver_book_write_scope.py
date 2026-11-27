"""Write boundaries for multi-book Silver projects."""

from datetime import datetime, timedelta, timezone
from io import BytesIO
from uuid import uuid4

import pytest


@pytest.fixture
def book_project(app_ctx, client):
    from app.extensions import db
    from app.models import LMUser, Project, TestBook

    with app_ctx.app_context():
        user = LMUser.query.filter_by(username="admin").one()
        project = Project(code="MULTI", name="Multi-book", project_type="silver_books")
        other = Project(code="OTHER", name="Other", project_type="silver_books")
        db.session.add_all([project, other])
        db.session.flush()
        first = TestBook(project_id=project.id, name="First")
        second = TestBook(project_id=project.id, name="Second")
        foreign = TestBook(project_id=other.id, name="Foreign")
        db.session.add_all([first, second, foreign])
        db.session.commit()
        with client.session_transaction() as session:
            session["lm_user_id"] = user.id
            session["csrf_token"] = "book-write-scope"
        yield user, project, first.id, second.id, foreign.id


def _post(client, path, **kwargs):
    return client.post(f"/api/v1{path}", headers={"X-CSRF-Token": "book-write-scope"},
                       **kwargs)


def test_legacy_imports_reject_preview_and_prior_jobs(client, book_project):
    from app.extensions import db
    from app.models import DataJob, TestItemRow
    from app.services.lanmatrix import import_job_service

    user, project, first, second, _ = book_project
    for book_id in (first, second):
        response = _post(client, f"/projects/{project.id}/items", json={
            "book_id": book_id, "values": {"case_id": "QTS-001"}, "draft": True,
        })
        assert response.status_code == 201, response.get_json()

    snapshot = import_job_service.capture_snapshot(project.id, "test")
    assert [row["case_id"] for row in snapshot["rows"]] == ["QTS-001", "QTS-001"]
    for route, import_format in (("imports", None), ("testmatrix/import", "test_matrix")):
        preview = _post(client, f"/projects/{project.id}/{route}", data={
            "file": (BytesIO(b"unparsed"), "legacy.xlsx"), "mode": "replace_all",
        })
        assert preview.status_code == 400
        assert "导入式样书" in preview.get_json()["error"]["message"]

        parameters = {"mode": "replace_all", "_snapshot": snapshot}
        if import_format:
            parameters["format"] = import_format
        job = DataJob(
            project_id=project.id, job_type="import", status="previewed",
            original_filename="legacy.xlsx", parameters=parameters,
            preview={"invalid": 0, "rows": []}, created_by=user.id,
            expires_at=datetime.now(timezone.utc) + timedelta(days=1),
        )
        db.session.add(job)
        db.session.commit()
        commit = _post(client, f"/imports/{job.id}/commit")
        assert commit.status_code == 400
        assert "导入式样书" in commit.get_json()["error"]["message"]
        assert job.status == "previewed"
        assert import_job_service.capture_snapshot(project.id, "test") == snapshot
    assert {row.book_id for row in TestItemRow.query.filter_by(
        project_id=project.id, deleted_at=None).all()} == {first, second}


def test_rest_requires_book_scope_on_test_and_io_only(client, book_project):
    from app.models import FieldDefinition, TestItemRow

    _, project, first, second, foreign = book_project
    items = f"/projects/{project.id}/items"
    io_pool = f"/projects/{project.id}/pool/io/entries"
    const_pool = f"/projects/{project.id}/pool/const/entries"
    for path, body, status in (
        (items, {"draft": True}, 400),
        (items, {"count": 2, "book_id": foreign, "draft": True}, 404),
        (items, {"sheet": "test", "book_id": first + 0.5, "draft": True}, 404),
        (items, {"sheet": "io", "count": 2, "draft": True}, 400),
        (items, {"sheet": "lib", "book_id": first, "draft": True}, 400),
        (items, {"sheet": "const", "count": 2, "book_id": first, "draft": True}, 400),
        (io_pool, {"values": {"io_name": "SPEED"}}, 400),
        (io_pool, {"values": {"io_name": "SPEED", "book_id": foreign}}, 404),
        (const_pool, {"book_id": first, "values": {"const_name": "LIMIT"}}, 400),
    ):
        response = _post(client, path, json=body)
        assert response.status_code == status, response.get_json()

    assert TestItemRow.query.filter_by(project_id=project.id).count() == 0
    assert FieldDefinition.query.filter_by(project_id=project.id).count() == 0

    bulk = _post(client, items, json={"count": 2, "draft": True, "book_id": str(first)})
    assert bulk.status_code == 201, bulk.get_json()
    assert [row["book_id"] for row in bulk.get_json()["data"]["items"]] == [first, first]
    for book_id in (first, second):
        signal = _post(client, io_pool, json={
            "values": {"io_name": "SPEED", "io_path": "/speed", "book_id": book_id},
        })
        assert signal.status_code == 201, signal.get_json()
        assert signal.get_json()["data"]["item"]["book_id"] == book_id
    const = _post(client, const_pool, json={"values": {"const_name": "LIMIT"}})
    assert const.status_code == 201, const.get_json()
    assert const.get_json()["data"]["item"]["book_id"] is None


def test_io_replace_commit_requires_and_respects_book_scope(client, book_project):
    from app.extensions import db
    from app.models import DataJob, TestItemRow
    from app.services.lanmatrix import import_job_service, io_excel

    user, project, first, second, _ = book_project
    for book_id in (first, second):
        response = _post(client, f"/projects/{project.id}/pool/io/entries", json={
            "book_id": book_id, "values": {"io_name": "SPEED"},
        })
        assert response.status_code == 201, response.get_json()

    snapshot = import_job_service.capture_snapshot(project.id, "io")
    job = DataJob(
        project_id=project.id, job_type="import", status="previewed",
        original_filename="io.xlsx", created_by=user.id,
        parameters={"format": "io", "mode": "replace_all", "_snapshot": snapshot},
        preview={"invalid": 0, "rows": []},
        expires_at=datetime.now(timezone.utc) + timedelta(days=1),
    )
    db.session.add(job)
    db.session.commit()
    commit = _post(client, f"/imports/{job.id}/commit")
    assert commit.status_code == 400
    assert import_job_service.capture_snapshot(project.id, "io") == snapshot

    source = BytesIO()
    io_excel.build_workbook({"items": [{"io_name": "BRAKE"}]}).save(source)
    source.seek(0)
    preview = _post(client, f"/projects/{project.id}/io/import", data={
        "file": (source, "io.xlsx"), "mode": "replace_all", "book_id": str(first),
    })
    assert preview.status_code == 201, preview.get_json()
    job_id = preview.get_json()["data"]["job"]["id"]
    commit = _post(client, f"/imports/{job_id}/commit")
    assert commit.status_code == 200, commit.get_json()
    rows = TestItemRow.query.filter_by(project_id=project.id, sheet="io", deleted_at=None)
    assert {(row.book_id, row.get_field("io_name")) for row in rows} == {
        (first, "BRAKE"), (second, "SPEED"),
    }


def test_materialization_isolates_invalid_book_scope(book_project):
    from app.models import TestItemRow
    from app.services.lanmatrix import items_service

    user, project, first, second, foreign = book_project
    for sheet, valid_book_ids, invalid_book_ids in (
        ("test", [first, second], [None, foreign, True]),
        ("io", [first, second], [None, foreign]),
        ("lib", [None], [first]),
        ("const", [None], [first]),
    ):
        valid = [{"uuid": str(uuid4()), "case_id": f"{sheet}-shared-id", "book_id": scope}
                 for scope in valid_book_ids]
        invalid = [{"uuid": str(uuid4()), "book_id": scope}
                   for scope in invalid_book_ids]
        result = items_service.materialize_sheet(user, project, sheet, invalid + valid)
        assert result["created"] == len(valid)
        assert result["failed"] == len(invalid)
        assert result["errors"].keys() == {row["uuid"] for row in invalid}
        rows = TestItemRow.query.filter_by(project_id=project.id, sheet=sheet).all()
        assert {(row.uuid, row.book_id, row.case_id) for row in rows} == {
            (row["uuid"], row["book_id"], row["case_id"]) for row in valid
        }
