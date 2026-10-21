"""gzip for JSON API responses (static assets are covered elsewhere)."""

from __future__ import annotations

import gzip
import json
from pathlib import Path

import pytest
from flask import Response, jsonify


@pytest.fixture()
def client(tmp_path):
    import app as app_pkg
    from app.config import Config

    class CompressionConfig(Config):
        TESTING = True
        SECRET_KEY = "compression-test"
        GLOBAL_LOGIN_REQUIRED = False
        INSTANCE_DIR = Path(tmp_path) / "instance"
        SQLALCHEMY_DATABASE_URI = "postgresql+psycopg2://invalid:invalid@127.0.0.1:1/never-connect"

    application = app_pkg.create_app(CompressionConfig)
    rows = [{"id": n, "title": f"row {n}", "steps": "x" * 40} for n in range(200)]
    application.add_url_rule("/api/test/big", "big", lambda: jsonify(rows=rows))
    application.add_url_rule("/api/test/small", "small", lambda: jsonify(ok=True))
    application.add_url_rule("/api/test/conflict", "conflict", lambda: (jsonify(rows=rows), 409))
    application.add_url_rule("/api/test/text", "text", lambda: Response("y" * 5000, mimetype="text/plain"))
    application.add_url_rule("/api/test/stream", "stream", lambda: Response(
        iter([json.dumps({"rows": rows})]), mimetype="application/json", direct_passthrough=True))
    return application.test_client()


GZIP = {"Accept-Encoding": "gzip, deflate"}


def test_large_json_is_gzipped_and_round_trips(client):
    resp = client.get("/api/test/big", headers=GZIP)
    assert resp.headers["Content-Encoding"] == "gzip"
    assert "Accept-Encoding" in resp.headers["Vary"]
    assert int(resp.headers["Content-Length"]) == len(resp.data)
    plain = client.get("/api/test/big")
    assert len(resp.data) < len(plain.data) / 5
    assert json.loads(gzip.decompress(resp.data)) == plain.get_json()


@pytest.mark.parametrize("path,headers", [
    ("/api/test/big", {}),              # client did not ask for gzip
    ("/api/test/small", GZIP),          # below the size threshold
    ("/api/test/conflict", GZIP),       # error envelopes stay plain
    ("/api/test/text", GZIP),           # only JSON
    ("/api/test/stream", GZIP),         # streamed bodies are never buffered
])
def test_other_responses_are_left_plain(client, path, headers):
    resp = client.get(path, headers=headers)
    assert "Content-Encoding" not in resp.headers
