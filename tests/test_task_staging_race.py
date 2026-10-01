
"""An active task must keep the staging directory it already owns."""

from __future__ import annotations

import importlib
import os
import tempfile
from pathlib import Path

import pytest


@pytest.fixture()
def app():
    root = Path(tempfile.mkdtemp(prefix="staging_race_"))
    old = {key: os.environ.get(key) for key in (
        "DATABASE_URL", "SECRET_KEY", "WORKSPACE_DIR", "RUNNER_BACKEND",
        "GLOBAL_LOGIN_REQUIRED", "HUEY_IMMEDIATE")}
    os.environ["DATABASE_URL"] = f"sqlite:///{root / 'race.db'}"
    os.environ["SECRET_KEY"] = "staging-test"
    os.environ["WORKSPACE_DIR"] = str(root / "ws")
    os.environ["RUNNER_BACKEND"] = "mock"
    os.environ["GLOBAL_LOGIN_REQUIRED"] = "0"
    os.environ["HUEY_IMMEDIATE"] = "1"
    import app.config as config_mod
    import app as app_pkg
    importlib.reload(config_mod)
    importlib.reload(app_pkg)
    application = app_pkg.create_app()
    from app.bootstrap import bootstrap_app
    bootstrap_app(application)
    application._race_root = root
    with application.app_context():
        yield application
    for key, value in old.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value


def _seed(app):
    from app.extensions import db
    from app.models import LMUser, Project, ProjectMember, ProjectModel, Task, TestItemRow
    from app.services import task_service

    user = LMUser(username="race", display_name="Race", password_hash="x")
    user.set_password("secret")
    db.session.add(user)
    db.session.commit()
    project = Project(code="RACE1", name="Race", owner_id=user.id)
    db.session.add(project)
    db.session.commit()
    db.session.add(ProjectMember(project_id=project.id, user_id=user.id, role="project_admin"))
    db.session.add(ProjectModel(project_id=project.id, name="engine", version="v1",
                                sil_path="/models/v1.sil", kind="path", is_current=True))
    db.session.add(TestItemRow(project_id=project.id, sheet="test", uuid="row-race",
                               case_id="TC-1", title="t", result="Not Tested"))
    db.session.commit()
    task, started = task_service.upsert_task(
        task_name="TC-1", file_name="(json runner)", submitter=user.username,
        test_id="TC-1", sil_relpath="/models/v1.sil", sil_name="engine",
        sil_version="v1", workspace=str(app._race_root / "ws" / "RACE1"),
        project_id=project.id, submitter_id=user.id)
    assert started is True
    return user, project, task


def test_active_upsert_does_not_start_another_run(app):
    from app.services import task_service

    _user, project, task = _seed(app)
    again, started = task_service.upsert_task(
        task_name="TC-1", file_name="(json runner)", submitter="race",
        test_id="TC-1", sil_relpath="/models/v2.sil", sil_name="engine",
        sil_version="v2", workspace=task.workspace, project_id=project.id,
        submitter_id=task.submitter_id)
    assert started is False
    assert again.id == task.id
    assert again.sil_relpath == "/models/v1.sil"
    assert again.sil_version == "v1"


def test_run_selected_keeps_the_active_staging_directory(app, monkeypatch):
    from app.runners import run_layout

    user, project, task = _seed(app)
    staging = run_layout.staging_dir(task.workspace, task.test_id) / task.test_id
    staging.mkdir(parents=True)
    marker = staging / "marker.txt"
    marker.write_text("OLD", encoding="utf-8")
    calls = {"enqueue": 0, "materialise": 0}

    def _enqueue(_task):
        calls["enqueue"] += 1

    def _materialise(*_args, **_kwargs):
        calls["materialise"] += 1
        marker.write_text("NEW", encoding="utf-8")

    monkeypatch.setattr("app.routes.lanmatrix.tasks._enqueue_task", _enqueue)
    monkeypatch.setattr(
        "app.services.lanmatrix.silver_json_export.materialise_run_dir", _materialise)
    client = app.test_client()
    with client.session_transaction() as sess:
        sess["lm_user_id"] = user.id
        sess["csrf_token"] = "test-csrf"
    response = client.post(
        f"/api/v1/projects/{project.id}/tasks/run-selected",
        json={"test_ids": ["TC-1"], "model": "engine@v1"},
        headers={"X-CSRF-Token": "test-csrf"})
    assert response.status_code == 201, response.get_data(as_text=True)
    assert marker.read_text(encoding="utf-8") == "OLD"
    assert calls == {"enqueue": 0, "materialise": 0}


@pytest.mark.parametrize("phase", ["materialise", "enqueue"])
@pytest.mark.parametrize("scenario", ["new", "existing", "retest"])
def test_failed_submission_is_retryable(app, monkeypatch, phase, scenario):
    from app.extensions import db
    from app.models import Task, TaskStatus

    user, project, task = _seed(app)
    existing = scenario != "new"
    task_key = task.task_key
    if scenario == "retest":
        saved_model = app._race_root / "saved.sil"
        saved_model.write_text("mock", encoding="utf-8")
        task.sil_relpath = str(saved_model)
    if existing:
        task.status = TaskStatus.PASSED.value
        task.result = "PASS"
        task.report_path = "old-report"
    else:
        db.session.delete(task)
    db.session.commit()

    def fail(*_args, **_kwargs):
        raise RuntimeError("submission failed")

    monkeypatch.setattr("app.routes.lanmatrix.tasks._enqueue_task", fail if phase == "enqueue" else lambda _task: None)
    monkeypatch.setattr("app.services.lanmatrix.silver_json_export.materialise_run_dir", fail if phase == "materialise" else lambda *_args: None)
    client = app.test_client()
    with client.session_transaction() as session:
        session["lm_user_id"] = user.id
        session["csrf_token"] = "test-csrf"
    url = f"/api/v1/projects/{project.id}/tasks/run-selected"
    project_id = project.id
    payload = {"test_ids": ["TC-1"], "model": "engine@v1"}
    if scenario == "retest":
        url = f"/api/v1/projects/{project_id}/tasks/rerun-selected"
        payload = {"task_keys": [task_key]}
    headers = {"X-CSRF-Token": "test-csrf"}
    response = client.post(url, json=payload, headers=headers)
    assert response.json["data"]["errors"]
    db.session.expire_all()
    stored = Task.query.filter_by(project_id=project_id, test_id="TC-1").first()
    assert stored is None or stored.status not in (TaskStatus.QUEUED.value, TaskStatus.RUNNING.value)
    if existing and phase == "materialise":
        assert stored.result == "PASS"
        assert stored.report_path == "old-report"
    monkeypatch.setattr("app.routes.lanmatrix.tasks._enqueue_task", lambda _task: None)
    monkeypatch.setattr("app.services.lanmatrix.silver_json_export.materialise_run_dir", lambda *_args: None)
    retry = client.post(url, json=payload, headers=headers)
    assert len(retry.json["data"]["created"]) == 1


@pytest.mark.parametrize("existing", [False, True])
@pytest.mark.parametrize("test_ids", [("TC-1", "TC-1"), ("TC/1", "TC_1"), ("TC-1", "tc-1")])
def test_postgresql_concurrent_upserts_claim_only_once(app_ctx, tmp_path, monkeypatch, existing, test_ids):
    import threading
    import time
    from concurrent.futures import ThreadPoolExecutor
    from app.extensions import db
    from app.models import Task, TaskStatus
    from app.services import task_service

    if test_ids == ("TC-1", "tc-1") and os.name != "nt":
        pytest.skip("Case-insensitive staging aliases require Windows")
    with app_ctx.app_context():
        if db.engine.dialect.name != "postgresql":
            pytest.skip("PostgreSQL claim locking requires PostgreSQL")
        app_ctx._race_root = tmp_path
        user, project, task = _seed(app_ctx)
        project_id, user_id = project.id, user.id
        if existing:
            task.test_id = test_ids[0]
            task.status = TaskStatus.PASSED.value
        else:
            db.session.delete(task)
        db.session.commit()

    original = task_service.find_task_by_test_id
    def delayed_find(*args, **kwargs):
        found = original(*args, **kwargs)
        time.sleep(0.15)
        return found
    monkeypatch.setattr(task_service, "find_task_by_test_id", delayed_find)
    start = threading.Barrier(2)
    def submit(test_id):
        with app_ctx.app_context():
            start.wait(timeout=5)
            claimed, started = task_service.upsert_task(
                task_name="TC-1", file_name="runner", submitter="race",
                test_id=test_id, sil_relpath="/models/v1.sil", workspace="",
                project_id=project_id, submitter_id=user_id)
            return claimed.id, started
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(submit, test_ids))
    assert sorted(started for _task_id, started in results) == [False, True]
    assert len({task_id for task_id, _started in results}) == 1


def test_old_submission_failure_does_not_fail_a_new_attempt(app):
    from app.extensions import db
    from app.models import TaskStatus
    from app.services import task_service

    _user, project, task = _seed(app)
    task_id = task.id
    old_run_count = task.run_count
    task.status = TaskStatus.CANCELLED.value
    db.session.commit()
    again, started = task_service.upsert_task(
        task_name="TC-1", file_name="runner", submitter="race", test_id="TC-1",
        sil_relpath="/models/v1.sil", workspace="", project_id=project.id)
    assert started
    task_service.fail_submission(task_id, old_run_count, "failure from the previous attempt")
    db.session.refresh(again)
    assert again.status == TaskStatus.QUEUED.value


def test_staging_alias_does_not_start_another_active_task(app):
    from app.services import task_service

    _user, project, task = _seed(app)
    again, started = task_service.upsert_task(
        task_name="TC_1", file_name="runner", submitter="race", test_id="TC/1",
        sil_relpath="/models/v1.sil", workspace="", project_id=project.id)
    assert started
    alias, started = task_service.upsert_task(
        task_name="TC_1", file_name="runner", submitter="race", test_id="TC_1",
        sil_relpath="/models/v2.sil", workspace="", project_id=project.id)
    assert not started
    assert alias.id == again.id
