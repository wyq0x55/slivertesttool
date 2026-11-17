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


def test_static_wasm_is_gzipped(client):
    resp = client.get("/static/vendor/sbs/onig.wasm", headers=GZIP)
    assert resp.status_code == 200
    assert resp.headers.get("Content-Encoding") == "gzip"
    raw = client.get("/static/vendor/sbs/onig.wasm").data
    assert len(resp.data) < len(raw) * 0.5
    assert gzip.decompress(resp.data) == raw


def test_static_sidecar_gz_is_served_when_fresh_and_ignored_when_stale(tmp_path):
    import os
    import app as app_pkg
    from app.config import Config

    static_dir = tmp_path / "static"
    static_dir.mkdir()
    asset = static_dir / "bundle.js"
    payload_v1 = ("const alpha = 'hello world '; \n" * 200).encode("utf-8")
    asset.write_bytes(payload_v1)
    sidecar = static_dir / "bundle.js.gz"
    precompressed = gzip.compress(payload_v1, compresslevel=9)
    sidecar.write_bytes(precompressed)
    mtime = asset.stat().st_mtime
    os.utime(sidecar, (mtime + 5, mtime + 5))

    class SidecarConfig(Config):
        TESTING = True
        SECRET_KEY = "sidecar-test"
        GLOBAL_LOGIN_REQUIRED = False
        INSTANCE_DIR = tmp_path / "instance"
        SQLALCHEMY_DATABASE_URI = "postgresql+psycopg2://invalid:invalid@127.0.0.1:1/never-connect"

    application = app_pkg.create_app(SidecarConfig)
    application.static_folder = str(static_dir)
    c = application.test_client()
    resp = c.get("/static/bundle.js", headers=GZIP)
    assert resp.status_code == 200
    assert resp.headers.get("Content-Encoding") == "gzip"
    assert resp.data == precompressed
    assert gzip.decompress(resp.data) == payload_v1

    # Make source newer than sidecar: stale sidecar must not serve old bytes
    payload_v2 = ("const beta = 'updated bundle '; \n" * 220).encode("utf-8")
    asset.write_bytes(payload_v2)
    os.utime(asset, (mtime + 20, mtime + 20))
    resp2 = c.get("/static/bundle.js", headers=GZIP)
    assert gzip.decompress(resp2.data) == payload_v2
