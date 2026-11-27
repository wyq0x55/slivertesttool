"""A book pins one model registration and a retest keeps its captured model."""

import pytest

from test_silver_books import _import_book, _item, _setup


@pytest.fixture
def binding(app_ctx, tmp_path):
    from app.extensions import db
    from app.models import Project, ProjectModel

    with app_ctx.app_context():
        user, project, original = _setup(app_ctx, tmp_path)
        next_path = tmp_path / "plant-v2.sil"
        next_path.write_text("# second build\n", encoding="utf-8")
        newer = ProjectModel(project_id=project.id, name=original.name, version="v2",
                             kind="path", sil_path=str(next_path), is_current=True)
        other_project = Project(code="OTHER-MODEL", name="Other project", owner_id=user.id)
        db.session.add_all([newer, other_project])
        original.is_current = False
        db.session.flush()
        foreign = ProjectModel(project_id=other_project.id, name=original.name, version="v3",
                               kind="path", sil_path=str(next_path))
        db.session.add(foreign)
        db.session.commit()
        book_id = _import_book(
            user, project, original, filename="spec.xlsx", book_name="Specification",
            items=[_item("QTS-001", "Book case")],
        )["book_id"]
        project_id, user_id = project.id, user.id
        model_ids = original.id, newer.id, foreign.id

    client = app_ctx.test_client()
    with client.session_transaction() as session:
        session["lm_user_id"] = user_id
        session["csrf_token"] = "binding-csrf"
    client.environ_base["HTTP_X_CSRF_TOKEN"] = "binding-csrf"
    return client, f"/api/v1/projects/{project_id}", book_id, model_ids


def test_book_model_is_fixed_at_creation_and_import(binding):
    client, base, book_id, (original_id, newer_id, foreign_id) = binding
    book_url = f"{base}/books/{book_id}"
    for model_id in (newer_id, foreign_id, None):
        response = client.patch(book_url, json={"model_id": model_id, "name": "Rejected"})
        assert response.status_code == 400
        assert response.json["error"]["code"] == "VALIDATION_ERROR"
    original = client.get(f"{base}/books").json["data"]["books"][0]
    assert original["name"] == "Specification"
    assert original["model"]["id"] == original_id
    assert original["model"]["version"] == "v1"
    response = client.patch(book_url, json={"model_id": str(original_id), "name": "Renamed"})
    assert response.status_code == 200
    assert response.json["data"]["book"]["name"] == "Renamed"

    response = client.post(f"{base}/books", json={"name": "Second", "model_id": newer_id})
    assert response.status_code == 201
    assert response.json["data"]["book"]["model_id"] == newer_id
    response = client.post(f"{base}/books", json={"name": "Foreign", "model_id": foreign_id})
    assert response.status_code == 400


def test_bound_model_label_and_registration_are_protected(binding):
    client, base, _, (original_id, newer_id, _) = binding
    response = client.patch(f"{base}/models/version", json={"model_id": original_id, "version": "v1.1"})
    assert response.status_code == 400
    response = client.patch(
        f"{base}/models/version",
        json={"model_id": original_id, "version": "v1", "version_note": "Release note"},
    )
    assert response.status_code == 200
    assert response.json["data"]["model"]["version_note"] == "Release note"
    response = client.delete(f"{base}/models", json={"model_id": original_id})
    assert response.status_code == 409
    response = client.patch(f"{base}/models/version", json={"model_id": newer_id, "version": "v2.1"})
    assert response.status_code == 200


@pytest.mark.parametrize("capture", ["intact", "legacy", "missing", "foreign"])
def test_book_retest_uses_the_captured_model(app_ctx, binding, monkeypatch, capture):
    from pathlib import Path

    from app.extensions import db
    from app.models import Task, TestBook, TestItemRow
    from app.routes.lanmatrix import tasks
    from app.services import run_evidence_service

    client, base, book_id, (original_id, newer_id, foreign_id) = binding
    calls = []
    monkeypatch.setattr(tasks, "_enqueue_task", lambda task: calls.append(task.task_key))
    with app_ctx.app_context():
        row_uuid = TestItemRow.query.filter_by(book_id=book_id, sheet="test").one().uuid

    response = client.post(f"{base}/tasks/run-selected", json={"items": [{"uuid": row_uuid}]})
    assert response.status_code == 201
    assert len(response.json["data"]["created"]) == 1, response.json
    task_key = calls[0]
    with app_ctx.app_context():
        task = Task.query.filter_by(task_key=task_key).one()
        original = run_evidence_service.read_evidence(task)["model"]
        assert original["id"] == original_id
        assert original["version"] == "v1"
        task.status = "passed"
        db.session.get(TestBook, book_id).model_id = newer_id
        if capture == "legacy":
            task.sil_relpath = original["source_path"]
            task.sil_model_id = None
            task.row_uuid = None
        if capture == "missing":
            Path(task.sil_relpath).unlink()
        if capture == "foreign":
            task.sil_model_id = foreign_id
        db.session.commit()

    response = client.post(f"{base}/tasks/rerun-selected", json={"task_keys": [task_key]})
    assert response.status_code == 201
    result = response.json["data"]
    with app_ctx.app_context():
        task = Task.query.filter_by(task_key=task_key).one()
        if capture in ("intact", "legacy"):
            assert len(result["created"]) == 1, result
            assert calls == [task_key, task_key]
            assert task.run_count == 2
            assert task.sil_model_id == (original_id if capture == "intact" else None)
            model = run_evidence_service.read_evidence(task)["model"]
            for field in ("name", "version", "source_path", "sha256"):
                assert model[field] == original[field]
        else:
            assert result["created"] == []
            assert len(result["errors"]) == 1
            assert calls == [task_key]
            assert task.run_count == 1
            assert task.status == "passed"
