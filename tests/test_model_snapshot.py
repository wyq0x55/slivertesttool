
"""Saved model versions and the snapshot stamped onto a task."""

from __future__ import annotations

import importlib
import os
import tempfile
from pathlib import Path

import pytest


@pytest.fixture()
def app():
    root = Path(tempfile.mkdtemp(prefix="model_snapshot_"))
    old = {key: os.environ.get(key) for key in (
        "DATABASE_URL", "SECRET_KEY", "MODEL_DIR", "WORKSPACE_DIR", "RUNNER_BACKEND")}
    os.environ["DATABASE_URL"] = f"sqlite:///{root / 'snap.db'}"
    os.environ["SECRET_KEY"] = "snapshot-test"
    os.environ["MODEL_DIR"] = str(root / "models")
    os.environ["WORKSPACE_DIR"] = str(root / "ws")
    os.environ["RUNNER_BACKEND"] = "mock"

    import app.config as config_mod
    import app as app_pkg
    importlib.reload(config_mod)
    importlib.reload(app_pkg)
    application = app_pkg.create_app()
    from app.bootstrap import bootstrap_app
    bootstrap_app(application)
    with application.app_context():
        yield application
    for key, value in old.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value


def _remote_model(tmp: Path) -> Path:
    remote = tmp / "remote"
    remote.mkdir()
    (remote / "host.dll").write_bytes(b"dll")
    (remote / "host.sbs").write_bytes(b"sbs")
    (remote / "host.pdb").write_bytes(b"pdb")
    sil = remote / "host.sil"
    sil.write_text(f"{remote / 'host.dll'} -S {remote / 'host.sbs'}\n", encoding="utf-8")
    return sil


def test_registering_another_version_keeps_the_saved_copy(app, tmp_path):
    from app.extensions import db
    from app.models import LMUser, Project, ProjectModel
    from app.services import project_model_service as pms

    user = LMUser(username="snap", display_name="Snap", password_hash="x")
    project = Project(code="SNAP1", name="Snap", owner_id=None)
    db.session.add_all([user, project])
    db.session.commit()
    sil = _remote_model(tmp_path)

    first = pms.add_path_model(project.id, "engine", str(sil), version="v1")
    second = pms.add_path_model(project.id, "engine", str(sil), version="v2")

    assert first["version"] == "v1"
    assert second["version"] == "v2"
    assert Path(first["path"]).is_file()
    assert Path(second["path"]).is_file()
    assert Path(first["path"]) != Path(second["path"])
    saved = Path(first["path"]).read_text(encoding="utf-8")
    assert "remote" not in saved.replace("\\", "/").lower()
    assert (Path(first["path"]).parent / "host.dll").is_file()
    assert (Path(first["path"]).parent / "host.pdb").is_file()
    with pytest.raises(pms.ModelError):
        pms.add_path_model(project.id, "engine", str(sil), version="v1")
    assert ProjectModel.query.filter_by(project_id=project.id, name="engine").count() == 2


def test_writeback_uses_the_task_snapshot_not_the_registry(app):
    from datetime import datetime, timezone

    from app.extensions import db
    from app.models import LMUser, Project, ProjectModel, Task, TestItemRow, TestRunRecord
    from app.services.lanmatrix import run_writeback_service as rws

    user = LMUser(username="snap2", display_name="実施者A", password_hash="x")
    db.session.add(user)
    db.session.commit()
    project = Project(code="SNAP2", name="Snap", owner_id=user.id)
    db.session.add(project)
    db.session.commit()
    model = ProjectModel(project_id=project.id, name="engine", version="v2",
                         sil_path="/tmp/v2.sil", kind="path", is_current=True)
    db.session.add(model)
    db.session.add(TestItemRow(project_id=project.id, sheet="test", uuid="row-1",
                               case_id="TC-1", title="t", result="Not Tested"))
    task = Task(project_id=project.id, test_id="TC-1", task_key="T000099",
                status="passed", submitter_id=user.id, sil_name="engine",
                sil_version="v1", sil_model_id=None, sil_relpath="/tmp/v1.sil",
                finished_at=datetime(2026, 8, 10, 1, 30, tzinfo=timezone.utc))
    db.session.add(task)
    db.session.commit()

    assert rws.record_run(task, "PASS") == 1
    record = TestRunRecord.query.filter_by(project_id=project.id).one()
    assert record.model_version == "v1"
    model.version = "v9"
    db.session.commit()
    assert rws._model_identity(task) == ("engine", "v1")
