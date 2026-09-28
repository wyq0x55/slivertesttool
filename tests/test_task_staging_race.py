
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
