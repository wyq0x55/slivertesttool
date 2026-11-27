"""Client uploads replace server-path source bindings for unit-test projects."""

from io import BytesIO
import zipfile

import pytest
from werkzeug.datastructures import FileStorage

from test_model_snapshot import app


@pytest.fixture()
def project(app):
    from app.extensions import db
    from app.models import Project

    row = Project(code="UTUP", name="UT upload", owner_id=None, project_type="unit_test")
    db.session.add(row)
    db.session.commit()
    return row


def _file(name, payload):
    return FileStorage(stream=BytesIO(payload), filename=name)


def _zip(entries):
    buffer = BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, payload in entries.items():
            archive.writestr(name, payload)
    return buffer.getvalue()


def test_source_zip_and_index_are_stored_for_the_project(app, project):
    from pathlib import Path

    from app.services.ut_pipeline import pipeline, source_upload

    saved = source_upload.store_sources(
        project.id,
        source=_file("Soft.zip", _zip({"Soft/a.c": b"int a;", "readme.txt": b"hi"})),
        index=_file("funcs.csv", b"function,file\nfoo,Soft/a.c\n"),
        user_id=1,
    )
    root = Path(saved["product_root"])
    index = Path(saved["index_csv"])
    assert (root / "Soft" / "a.c").read_bytes() == b"int a;"
    assert index.read_bytes().startswith(b"function,file")
    assert saved["source_upload"]["source_name"] == "Soft.zip"
    assert saved["source_upload"]["source_files"] == 2
    assert saved["source_upload"]["index_name"] == "funcs.csv"
    config = pipeline.get_config(project.id)
    assert config.product_root == saved["product_root"]
    assert config.index_csv == saved["index_csv"]
    assert pipeline.config_document(config)["source_upload"]["source_name"] == "Soft.zip"

    replaced = source_upload.store_sources(
        project.id, source=None,
        index=_file("next.csv", b"function,file\nbar,Soft/a.c\n"),
        user_id=1,
    )
    assert replaced["product_root"] == saved["product_root"]
    assert Path(replaced["index_csv"]).read_bytes().startswith(b"function,file\nbar")
    assert (root / "Soft" / "a.c").is_file()
    assert replaced["source_upload"]["source_name"] == "Soft.zip"
    assert replaced["source_upload"]["index_name"] == "next.csv"


@pytest.mark.parametrize("name", ["../secret.txt", "/etc/passwd", "C:/secret.txt", "nested/../../outside.c"])
def test_zip_slip_is_rejected_without_a_binding(app, project, name):
    from app.models import UtPipelineConfig
    from app.services.lanmatrix.errors import ServiceError
    from app.services.ut_pipeline import source_upload

    with pytest.raises(ServiceError):
        source_upload.store_sources(
            project.id,
            source=_file("bad.zip", _zip({name: b"nope"})),
            index=None,
            user_id=1,
        )
    assert UtPipelineConfig.query.filter_by(project_id=project.id).first() is None
