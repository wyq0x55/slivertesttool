"""Runs reject collaborative edits that have not reached the database."""

import json

import pytest

from test_run_evidence import prepared


def _project(app, tmp_path, steps):
    from app.extensions import db
    from app.models import LMUser, Project, ProjectMember, ProjectModel, TestItemRow

    with app.app_context():
        user = LMUser(username="collab-runner", display_name="Runner", password_hash="x")
        db.session.add(user)
        db.session.commit()
        project = Project(code="COLLAB-RUN", name="Collab run", owner_id=user.id)
        db.session.add(project)
        db.session.commit()
        _prototype, _source, model = prepared(tmp_path)
        db.session.add(ProjectMember(project_id=project.id, user_id=user.id, role="project_admin"))
        db.session.add(ProjectModel(
            project_id=project.id, name="plant", version="v1",
            sil_path=str(model), is_current=True, kind="path"))
        db.session.add(TestItemRow(
            project_id=project.id, case_id="TC-1", sheet="test", uuid="test-1",
            custom_values={"test_id": "TC-1", "steps": steps}))
        db.session.add(TestItemRow(
            project_id=project.id, case_id="INIT", sheet="lib", uuid="lib-1",
            custom_values={"lib_func": "Init", "lib_stb": {"steps": []}, "lib_para": ""}))
        db.session.add(TestItemRow(
            project_id=project.id, case_id="LIMIT", sheet="const", uuid="const-1",
            custom_values={"const_name": "LIMIT", "const_value": "1"}))
        db.session.commit()
        return project.id, user.id


def _snapshot(steps):
    return {
        "tests": [{"uuid": "test-1", "test_id": "TC-1", "steps": steps}],
        "lib": [{"uuid": "lib-1", "name": "Init", "steps": {"steps": []}, "params": "", "init": ""}],
        "const": [{"uuid": "const-1", "name": "LIMIT", "value": "1"}],
    }


def _client(app, user_id):
    client = app.test_client()
    with client.session_transaction() as session:
        session["lm_user_id"] = user_id
        session["csrf_token"] = "collab-csrf"
    return client


def test_unmaterialized_edit_does_not_enqueue(app_ctx, tmp_path, monkeypatch):
    from app.extensions import db
    from app.models import Task

    project_id, user_id = _project(app_ctx, tmp_path, {"steps": [{"no": 1}]})
    client = _client(app_ctx, user_id)
    response = client.post(
        f"/api/v1/projects/{project_id}/tasks/run-selected",
        json={"test_ids": ["TC-1"], "model": "plant@v1",
              "expected_inputs": _snapshot({"steps": [{"no": 2}]})},
        headers={"X-CSRF-Token": "collab-csrf"})
    assert response.status_code == 409
    assert response.json["error"]["code"] == "COLLAB_INPUTS_NOT_MATERIALIZED"
    assert {"sheet": "test", "uuid": "test-1"} in response.json["error"]["details"]["conflicts"]
    with app_ctx.app_context():
        assert Task.query.filter_by(project_id=project_id).count() == 0


def test_materialized_snapshot_is_captured(app_ctx, tmp_path, monkeypatch):
    from app.extensions import db
    from app.jobqueue import tasks as jobs
    from app.models import Task
    from test_run_evidence import service

    steps = {"steps": [{"no": 2, "purpose": "new"}]}
    project_id, user_id = _project(app_ctx, tmp_path, steps)
    calls = []
    monkeypatch.setattr(jobs, "run_task", lambda *args: calls.append(args))
    client = _client(app_ctx, user_id)
    response = client.post(
        f"/api/v1/projects/{project_id}/tasks/run-selected",
        json={"test_ids": ["TC-1"], "model": "plant@v1", "expected_inputs": _snapshot(steps)},
        headers={"X-CSRF-Token": "collab-csrf"})
    assert response.status_code == 201, response.json
    with app_ctx.app_context():
        task = db.session.get(Task, calls[0][0])
        pinned = service().read_evidence(task)
        text = json.dumps(pinned)
        assert "new" in text


def test_changed_constant_blocks_retest_without_reset(app_ctx, tmp_path, monkeypatch):
    from app.extensions import db
    from app.jobqueue import tasks as jobs
    from app.models import Task

    project_id, user_id = _project(app_ctx, tmp_path, {"steps": [{"no": 1}]})
    monkeypatch.setattr(jobs, "run_task", lambda *args: None)
    client = _client(app_ctx, user_id)
    headers = {"X-CSRF-Token": "collab-csrf"}
    created = client.post(
        f"/api/v1/projects/{project_id}/tasks/run-selected",
        json={"test_ids": ["TC-1"], "model": "plant@v1"}, headers=headers)
    assert created.status_code == 201, created.json
    with app_ctx.app_context():
        task = Task.query.filter_by(project_id=project_id).one()
        task.status = "passed"
        task_key = task.task_key
        db.session.commit()
    stale = _snapshot({"steps": [{"no": 1}]})
    stale["const"][0]["value"] = "2"
    response = client.post(
        f"/api/v1/projects/{project_id}/tasks/rerun-selected",
        json={"task_keys": [task_key], "expected_inputs": stale}, headers=headers)
    assert response.status_code == 409
    with app_ctx.app_context():
        task = Task.query.filter_by(project_id=project_id).one()
        assert task.status == "passed"
        assert task.run_count == 1


def test_reordered_library_is_a_conflict(app_ctx, tmp_path):
    from app.extensions import db
    from app.models import Task, TestItemRow

    project_id, user_id = _project(app_ctx, tmp_path, {"steps": [{"no": 1}]})
    with app_ctx.app_context():
        db.session.add(TestItemRow(
            project_id=project_id, case_id="NEXT", sheet="lib", uuid="lib-2", row_order=2,
            custom_values={"lib_func": "Next", "lib_stb": {"steps": []}, "lib_para": ""}))
        db.session.commit()
    snapshot = _snapshot({"steps": [{"no": 1}]})
    snapshot["lib"] = list(reversed(snapshot["lib"] + [{
        "uuid": "lib-2", "name": "Next", "steps": {"steps": []}, "params": "", "init": ""}]))
    client = _client(app_ctx, user_id)
    response = client.post(
        f"/api/v1/projects/{project_id}/tasks/run-selected",
        json={"test_ids": ["TC-1"], "model": "plant@v1", "expected_inputs": snapshot},
        headers={"X-CSRF-Token": "collab-csrf"})
    assert response.status_code == 409
    with app_ctx.app_context():
        assert Task.query.filter_by(project_id=project_id).count() == 0


def test_duplicate_logical_test_id_is_rejected_before_enqueue(app_ctx, tmp_path):
    from app.extensions import db
    from app.models import Task, TestItemRow

    project_id, user_id = _project(app_ctx, tmp_path, {"steps": [{"no": 1}]})
    with app_ctx.app_context():
        db.session.add(TestItemRow(
            project_id=project_id, case_id="TC-1B", sheet="test", uuid="test-2",
            custom_values={"test_id": "TC-1", "steps": {"steps": [{"no": 9}]}}))
        db.session.commit()
    client = _client(app_ctx, user_id)
    response = client.post(
        f"/api/v1/projects/{project_id}/tasks/run-selected",
        json={"test_ids": ["TC-1", " TC-1 "], "model": "plant@v1"},
        headers={"X-CSRF-Token": "collab-csrf"})
    assert response.status_code == 409
    assert response.json["error"]["code"] == "AMBIGUOUS_TEST_ID"
    with app_ctx.app_context():
        assert Task.query.filter_by(project_id=project_id).count() == 0
