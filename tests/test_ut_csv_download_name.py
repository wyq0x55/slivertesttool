"""CSV download names come from the bound target, not internal attempt paths."""
import hashlib
from types import SimpleNamespace

import pytest
from flask import Flask

from app.routes.lanmatrix import ut_pipeline as routes
from app.services.lanmatrix.errors import ServiceError
from app.services.ut_pipeline import pipeline


@pytest.mark.parametrize("function", ["subject", "d_vol_dapl_jdg_adu"])
@pytest.mark.parametrize("stored_name", ["a01-generator.csv", "a02-llm.csv", "uploaded.csv"])
def test_csv_download_and_preview_use_the_bound_function(monkeypatch, tmp_path, function, stored_name):
    data = b'mod,"unit.c/subject","subject",1,1\r\n#COMMENT,x,ret\r\n,0,1\r\n'
    path = tmp_path / stored_name
    path.write_bytes(data)
    attempt = SimpleNamespace(csv_path=str(path), csv_sha256=hashlib.sha256(data).hexdigest())
    seen = []
    monkeypatch.setattr(pipeline, "_csv_attempt", lambda *ids: seen.append(ids) or attempt)
    job = SimpleNamespace(target=SimpleNamespace(function=function))
    monkeypatch.setattr(pipeline, "db", SimpleNamespace(session=SimpleNamespace(get=lambda *args: job)))
    monkeypatch.setattr(routes, "_project_and_role", lambda *args: None)
    app = Flask(__name__)
    app.add_url_rule("/csv", view_func=lambda: routes.attempt_csv.__wrapped__(4, 227, 1))
    client = app.test_client()
    response = client.get("/csv")
    assert response.status_code == 200 and response.data == data
    assert response.headers["Content-Disposition"] == f'attachment; filename="{function}.csv"'
    preview = client.get("/csv?format=text").get_json()["data"]
    assert preview == {"name": f"{function}.csv", "text": data.decode("cp932")}
    assert seen == [(4, 227, 1), (4, 227, 1)]
    assert path.read_bytes() == data


@pytest.mark.parametrize("function", ["../subject", 'subject"', "subject\r\nX-Header: value"])
def test_invalid_function_name_cannot_enter_a_download_header(monkeypatch, tmp_path, function):
    path = tmp_path / "a01-generator.csv"
    path.write_bytes(b"csv")
    attempt = SimpleNamespace(csv_path=str(path), csv_sha256=hashlib.sha256(b"csv").hexdigest())
    monkeypatch.setattr(pipeline, "_csv_attempt", lambda *args: attempt)
    job = SimpleNamespace(target=SimpleNamespace(function=function))
    monkeypatch.setattr(pipeline, "db", SimpleNamespace(session=SimpleNamespace(get=lambda *args: job)))
    with pytest.raises(ServiceError, match="Invalid target function name"):
        pipeline.attempt_csv(4, 227, 1)
