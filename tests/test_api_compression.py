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


def test_static_js_enforces_javascript_mime_even_when_windows_registry_says_text_plain():
    import mimetypes
    import app as app_pkg
    from app.config import Config

    # Simulate Windows HKCR\.js\Content Type = "text/plain" poisoning both
    # before and after create_app().
    mimetypes.add_type("text/plain", ".js", strict=True)
    mimetypes.add_type("text/plain", ".js", strict=False)

    class MimeConfig(Config):
        TESTING = True
        SECRET_KEY = "mime-test"
        GLOBAL_LOGIN_REQUIRED = False
        SQLALCHEMY_DATABASE_URI = "postgresql+psycopg2://invalid:invalid@127.0.0.1:1/never-connect"

    application = app_pkg.create_app(MimeConfig)
    c = application.test_client()

    resp_esm = c.get("/static/vendor/univer/esm/univer-core.js")
    assert resp_esm.status_code == 200
    assert resp_esm.mimetype == "text/javascript"
    assert "text/javascript" in resp_esm.headers.get("Content-Type", "")

    # Even if external code re-poisons mimetypes at runtime, the static handler
    # must still emit text/javascript so <script type="module"> never fails.
    mimetypes.add_type("text/plain", ".js", strict=True)
    resp_js = c.get("/static/js/lanmatrix/grid.js")
    assert resp_js.status_code == 200
    assert resp_js.mimetype == "text/javascript"

    resp_wasm = c.get("/static/vendor/sbs/onig.wasm")
    assert resp_wasm.status_code == 200
    assert resp_wasm.mimetype == "application/wasm"

    # Restore authoritative mapping in process state.
    mimetypes.add_type("text/javascript", ".js", strict=True)
    mimetypes.add_type("text/javascript", ".js", strict=False)


def test_hashed_esm_chunks_receive_immutable_cache_and_gzip_sidecar(client):
    esm_chunks = (
        Path(__file__).resolve().parent.parent
        / "app" / "static" / "vendor" / "univer" / "esm" / "chunks"
    )
    primary_chunks = sorted(esm_chunks.glob("univer-primary-*.js"))
    assert primary_chunks, "expected hashed univer-primary chunk in esm/chunks"
    chunk_name = primary_chunks[0].name

    resp = client.get(f"/static/vendor/univer/esm/chunks/{chunk_name}", headers=GZIP)
    assert resp.status_code == 200
    assert resp.mimetype == "text/javascript"
    assert "immutable" in resp.headers.get("Cache-Control", "")
    assert resp.headers.get("Content-Encoding") == "gzip"
    assert gzip.decompress(resp.data) == primary_chunks[0].read_bytes()

    missing = client.get("/static/vendor/univer/esm/chunks/univer-primary-deadbeef.js")
    assert missing.status_code == 404
    assert "immutable" not in missing.headers.get("Cache-Control", "")
