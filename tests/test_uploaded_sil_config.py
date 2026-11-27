"""Uploaded Silver .sil files become the project's runnable configuration."""

from io import BytesIO

import pytest
from werkzeug.datastructures import FileStorage

from test_model_snapshot import app


XML = """<workspace>
  <modules>
    <gui-module/>
    <module>
      <uuid>host</uuid>
      <sil-line>C:/client/engine.dll -S C:/client/engine.sbs</sil-line>
      <enabled>true</enabled>
    </module>
    <module>
      <uuid>judge</uuid>
      <sil-line>Python.dll ${JUDGEPY}</sil-line>
      <enabled>true</enabled>
    </module>
  </modules>
  <property name="macroStep" value="0.01"/>
</workspace>
"""


@pytest.fixture()
def project(app):
    from app.extensions import db
    from app.models import Project

    row = Project(code="UPLOADSIL", name="Uploaded sil", owner_id=None)
    db.session.add(row)
    db.session.commit()
    return row


def upload(name, content):
    if isinstance(content, str):
        content = content.encode("utf-8")
    return FileStorage(stream=BytesIO(content), filename=name)


def test_uploaded_sil_is_retargeted_and_summarized(app, project):
    from pathlib import Path

    from app.services import project_model_service as models

    saved = models.add_bundle_model(
        project.id, "plant", upload("host.dll", b"dll"),
        upload("host.sbs", "variable veh_speed;"),
        app.config_obj, pdb=upload("host.pdb", b"pdb"), version="v1",
        sil=upload("plant.sil", XML))

    sil = Path(saved["path"]).read_text(encoding="utf-8")
    assert "C:/client" not in sil
    assert "host.dll" in sil and "host.sbs" in sil
    assert "Python.dll" in sil
    assert 'value="0.01"' in sil
    assert "speedup" not in sil
    config = saved["test_config"]
    assert config["source"] == "uploaded-sil"
    assert config["properties"]["macroStep"] == "0.01"
    assert config["sbs_variables"] == ["veh_speed"]
    assert config["files"] == {"dll": "host.dll", "sbs": "host.sbs", "pdb": "host.pdb"}
    assert any(line == "host.dll -S host.sbs" for line in config["modules"])
    assert any("Python.dll" in line for line in config["modules"])
    assert models.list_models(project.id)[0]["test_config"]["sbs_variables"] == ["veh_speed"]


def test_uploaded_sil_rejects_a_missing_dependency_without_keeping_a_model(app, project):
    from app.models import ProjectModel
    from app.services import project_model_service as models

    body = XML.replace("Python.dll ${JUDGEPY}", "C:/client/extra.dll")
    with pytest.raises(models.ModelError, match="extra.dll"):
        models.add_bundle_model(
            project.id, "plant", upload("host.dll", b"dll"),
            upload("host.sbs", b"variable speed;"),
            app.config_obj, pdb=upload("host.pdb", b"pdb"), version="v1",
            sil=upload("plant.sil", body))
    assert ProjectModel.query.filter_by(project_id=project.id).count() == 0
