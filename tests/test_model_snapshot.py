
"""Saved model versions and the snapshot stamped onto a task."""

from __future__ import annotations

import importlib
import os
import tempfile
import xml.etree.ElementTree as ET
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


def test_xml_saved_copy_preserves_non_module_script_paths(app, tmp_path):
    from app.extensions import db
    from app.models import Project
    from app.services import project_model_service as models

    project = Project(code="SNAPXML", name="XML snapshot", owner_id=None)
    db.session.add(project)
    db.session.commit()
    source = _remote_model(tmp_path)
    (source.parent / "a2laccess.dll").write_bytes(b"runtime-plugin")
    script = "import ctypes; ctypes.CDLL('a2laccess.dll')"
    configuration = ET.Element("workspace")
    ET.SubElement(configuration, "property", name="script").text = script
    module = ET.SubElement(configuration, "module")
    ET.SubElement(module, "sil-line").text = (
        f"{(source.parent / 'host.dll').as_posix()} -S {(source.parent / 'host.sbs').as_posix()}")
    original = ET.tostring(configuration, encoding="unicode")
    source.write_text(original, encoding="utf-8")

    registered = models.add_path_model(project.id, "engine", str(source), version="v1")
    saved = Path(registered["path"])
    execution = ET.fromstring(saved.read_text(encoding="utf-8"))

    assert execution.find("property").text == script
    assert execution.find("module/sil-line").text == (
        f"{(saved.parent / 'host.dll').resolve().as_posix()} -S {(saved.parent / 'host.sbs').resolve().as_posix()}")
    assert (saved.parent / "host.dll").read_bytes() == b"dll"
    assert (saved.parent / "host.sbs").read_bytes() == b"sbs"
    assert (saved.parent / "host.pdb").read_bytes() == b"pdb"
    assert not (saved.parent / "a2laccess.dll").exists()
    assert source.read_text(encoding="utf-8") == original
    assert saved.read_text(encoding="utf-8") == original.replace(
        f"{(source.parent / 'host.dll').as_posix()} -S {(source.parent / 'host.sbs').as_posix()}",
        f"{(saved.parent / 'host.dll').resolve().as_posix()} -S {(saved.parent / 'host.sbs').resolve().as_posix()}")


@pytest.mark.parametrize("declaration", ["", '<?xml version="1.0" encoding="UTF-8"?>\n'])
def test_xml_module_rewrite_preserves_native_format_and_ignores_cdata(declaration):
    from app.services import project_model_service as models

    original = (
        declaration + '<workspace>\n  <gui-module/>\n'
        '<!-- keep the native layout -->\n'
        '<property name="script"><![CDATA[<sil-line>script.dll</sil-line>]]></property>\n'
        '<property name="label">日本語 &apos;label&apos;</property>\n'
        '<module><sil-line mode="a>b">host.dll -S host.sbs</sil-line></module>\n'
        '<module><sil-line/></module>\n</workspace>')
    paths = []

    def replace(match):
        paths.append(match.group("path"))
        return "saved&model/" + match.group("path")

    rewritten = models.rewrite_model_module_paths(original, replace)

    assert rewritten == original.replace(
        "host.dll -S host.sbs", "saved&amp;model/host.dll -S saved&amp;model/host.sbs")
    assert paths == ["host.dll", "host.sbs"]


def test_malformed_xml_cannot_be_registered_as_a_saved_model(app, tmp_path):
    from app.extensions import db
    from app.models import Project, ProjectModel
    from app.services import project_model_service as models

    project = Project(code="SNAPBADXML", name="Malformed XML", owner_id=None)
    db.session.add(project)
    db.session.commit()
    source = _remote_model(tmp_path)
    source.write_text("<workspace><module>", encoding="utf-8")
    model_root = Path(app.config["MODEL_DIR"]) / project.code
    existing = set(model_root.rglob("*"))

    with pytest.raises(models.ModelError, match="XML"):
        models.add_path_model(project.id, "engine", str(source), version="v1")

    assert ProjectModel.query.filter_by(project_id=project.id).count() == 0
    assert set(model_root.rglob("*")) == existing


def test_database_rejects_duplicate_unversioned_models(app):
    from sqlalchemy.exc import IntegrityError

    from app.extensions import db
    from app.models import Project, ProjectModel

    project = Project(code="SNAPNULL", name="Snapshot null", owner_id=None)
    db.session.add(project)
    db.session.commit()
    db.session.add(ProjectModel(
        project_id=project.id, name="engine", version=None,
        sil_path="/tmp/engine-1.sil", kind="path"))
    db.session.commit()

    db.session.add(ProjectModel(
        project_id=project.id, name="engine", version=None,
        sil_path="/tmp/engine-2.sil", kind="path"))
    with pytest.raises(IntegrityError):
        db.session.commit()
    db.session.rollback()


def test_model_management_targets_the_requested_saved_version(app, tmp_path):
    from app.extensions import db
    from app.models import Project, ProjectModel
    from app.services import project_model_service as pms

    project = Project(code="SNAPOPS", name="Snapshot operations", owner_id=None)
    db.session.add(project)
    db.session.commit()
    sil = _remote_model(tmp_path)
    first = pms.add_path_model(project.id, "engine", str(sil), version="v1")
    second = pms.add_path_model(project.id, "engine", str(sil), version="v2")

    pms.set_current(project.id, "", model_id=first["id"])
    assert db.session.get(ProjectModel, first["id"]).is_current is True
    assert db.session.get(ProjectModel, second["id"]).is_current is False

    pms.update_version(project.id, "", "v2.1", model_id=second["id"])
    pms.set_deprecated(project.id, "", True, model_id=second["id"])
    assert db.session.get(ProjectModel, second["id"]).deprecated_at is not None
    assert pms.remove_model(project.id, "", model_id=second["id"]) is True
    assert db.session.get(ProjectModel, second["id"]) is None


def test_sbs_reads_the_requested_saved_version_by_id(app, tmp_path):
    from app.extensions import db
    from app.models import Project, ProjectModel
    from app.services.lanmatrix import sbs_service

    project = Project(code="SNAPSBS", name="Snapshot SBS", owner_id=None)
    db.session.add(project)
    db.session.commit()
    model_dirs = []
    for version, content in (("v1", "module-v1"), ("v2", "module-v2")):
        bundle_dir = tmp_path / version
        bundle_dir.mkdir()
        (bundle_dir / "model.sbs").write_text(content, encoding="utf-8")
        model = ProjectModel(
            project_id=project.id, name="engine", version=version,
            kind="bundle", bundle_dir=str(bundle_dir),
            sil_path=str(bundle_dir / "model.sil"))
        db.session.add(model)
        model_dirs.append((model, content))
    db.session.commit()

    result = sbs_service.read_sbs(
        project.id, "", model_id=model_dirs[1][0].id)

    assert result["model_id"] == model_dirs[1][0].id
    assert result["content"] == "module-v2"


def test_default_resolution_uses_the_selected_default_row(app, tmp_path):
    from app.extensions import db
    from app.models import Project, ProjectModel
    from app.services import project_model_service as pms

    project = Project(code="SNAPDEF", name="Snapshot default", owner_id=None)
    db.session.add(project)
    db.session.commit()
    sil = _remote_model(tmp_path)
    first = pms.add_path_model(project.id, "engine", str(sil), version="v1")
    pms.add_path_model(project.id, "engine", str(sil), version="v2")
    db.session.get(ProjectModel, first["id"]).is_current = False
    db.session.commit()

    model_id, _, version, _ = pms.resolve_ref(project.id, "")

    assert model_id == first["id"]
    assert version == "v1"


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
