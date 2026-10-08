from __future__ import annotations

import hashlib
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest


@pytest.fixture
def server():
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            self.handle_request()

        def do_POST(self):
            self.handle_request()

        def handle_request(self):
            body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
            requests.append((self.command, self.path, self.headers, body))
            status = 200
            headers = {}
            mime = "application/json"
            payload = json.dumps({"success": True, "data": {"ok": True}, "error": None, "request_id": "req-test"}).encode()
            if self.path == "/api/v1/auth/login":
                headers["Set-Cookie"] = "session=fake-session; Path=/; HttpOnly"
                payload = b'{"success":true,"data":{"csrf_token":"test-csrf"}}'
            elif self.path.startswith("/api/v1/error/"):
                status = int(self.path.rsplit("/", 1)[1])
                payload = b'{"success":false,"error":{"code":"DENIED","message":"no","details":{"reason":"test"}},"request_id":"req-failed"}'
            elif self.path == "/api/v1/false":
                payload = b'{"success":false,"error":{"code":"BAD","message":"bad"}}'
            elif self.path == "/api/v1/redirect":
                status = 302
                headers["Location"] = "/api/v1/never-follow"
            elif self.path == "/api/v1/file":
                mime = "application/zip"
                payload = b"PK\x03\x04binary-test\x00"
            elif self.path == "/api/v1/truncated":
                mime = "application/octet-stream"
                headers["Content-Length"] = "5000"
                payload = b"short"
            elif self.path == "/api/v1/html":
                mime = "text/html"
                payload = b"<html>login page</html>"
            elif self.path == "/api/v1/bad-json":
                payload = b"not json"
            elif self.path == "/api/v1/stream":
                mime = "text/event-stream"
                payload = b': heartbeat\n\nid: 7\nevent: progress\ndata: {"step":1}\n\nevent: end\ndata: {}\n\n'
            self.send_response(status)
            self.send_header("Content-Type", mime)
            for key, value in headers.items():
                self.send_header(key, value)
            self.end_headers()
            self.wfile.write(payload)

    instance = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=instance.serve_forever, daemon=True)
    thread.start()
    yield "http://127.0.0.1:" + str(instance.server_port), requests
    instance.shutdown()
    instance.server_close()
    thread.join(timeout=5)


def test_login_cookie_csrf_json_and_repeat_query(server):
    from silver_cli.transport import Client

    url, requests = server
    client = Client(url)
    client.login("test", "private")
    reply = client.request("POST", "/api/v1/write", query=[("tag", "a"), ("tag", "b")], json_body={"name": "中文"})
    assert reply["request_id"] == "req-test"
    assert requests[-1][1] == "/api/v1/write?tag=a&tag=b"
    assert requests[-1][2]["Cookie"] == "session=fake-session"
    assert requests[-1][2]["X-CSRF-Token"] == "test-csrf"
    assert json.loads(requests[-1][3]) == {"name": "中文"}


@pytest.mark.parametrize("url", ["", "file:///etc/passwd", "ftp://host", "http://user:pw@localhost", "http://example.com", "https://host?q=1", "https://host#x", "http://localhost\\@evil", "http://localhost:bad"])
def test_unsafe_base_urls_rejected(url):
    from silver_cli.transport import Client, CliError

    with pytest.raises(CliError):
        Client(url)


@pytest.mark.parametrize("path", ["//evil/api", "https://evil/api", "/api/v1/../private", "/api/v1/%2e%2e/private", "/other", "/api/v1/x?redirect=evil", "/api/v1/a\\b", "/api/v1/%252fadmin"])
def test_unsafe_paths_rejected_before_http(server, path):
    from silver_cli.transport import Client, CliError

    url, requests = server
    with pytest.raises(CliError):
        Client(url).request("GET", path)
    assert requests == []


@pytest.mark.parametrize("status,exit_code", [(400, 2), (401, 3), (403, 4), (409, 5), (500, 7)])
def test_http_errors_keep_identity_and_code(server, status, exit_code):
    from silver_cli.transport import Client, CliError

    with pytest.raises(CliError) as caught:
        Client(server[0]).request("GET", f"/api/v1/error/{status}")
    assert caught.value.exit_code == exit_code
    assert caught.value.status == status
    assert caught.value.request_id == "req-failed"


def test_no_redirect_or_silent_false(server):
    from silver_cli.transport import Client, CliError

    with pytest.raises(CliError):
        Client(server[0]).request("GET", "/api/v1/redirect")
    assert len(server[1]) == 1
    with pytest.raises(CliError):
        Client(server[0]).request("GET", "/api/v1/false")


def test_multipart_repeated_fields_and_files(server, tmp_path):
    from silver_cli.transport import Client

    upload = tmp_path / "model.dll"
    upload.write_bytes(b"binary\x00\xff")
    Client(server[0]).request("POST", "/api/v1/upload", form=[("paths", "a.dll"), ("paths", "b.dll")], files=[("files", upload), ("files", upload)])
    request = server[1][-1]
    assert "multipart/form-data; boundary=" in request[2]["Content-Type"]
    assert int(request[2]["Content-Length"]) == len(request[3])
    assert request[3].count(b'name="paths"') == 2
    assert request[3].count(b'name="files"') == 2
    assert request[3].count(upload.read_bytes()) == 2


def test_download_hash_and_never_overwrite(server, tmp_path):
    from silver_cli.transport import Client, CliError

    destination = tmp_path / "result.zip"
    reply = Client(server[0]).request("GET", "/api/v1/file", output=destination)
    assert reply["data"]["sha256"] == hashlib.sha256(destination.read_bytes()).hexdigest()
    with pytest.raises(CliError):
        Client(server[0]).request("GET", "/api/v1/file", output=destination)
    assert destination.read_bytes() == b"PK\x03\x04binary-test\x00"


@pytest.mark.parametrize("path", ["/api/v1/truncated", "/api/v1/html", "/api/v1/error/403", "/api/v1/false", "/api/v1/bad-json"])
def test_bad_download_never_leaves_artifact(server, tmp_path, path):
    from silver_cli.transport import Client, CliError

    destination = tmp_path / "artifact"
    with pytest.raises(CliError):
        Client(server[0]).request("GET", path, output=destination)
    assert not destination.exists()


def test_sse_event_stream(server):
    from silver_cli.transport import Client

    events = list(Client(server[0]).request("GET", "/api/v1/stream", stream=True))
    assert events == [{"event": "progress", "id": "7", "data": {"step": 1}}, {"event": "end", "id": "7", "data": {}}]


def test_missing_output_and_invalid_mime(server):
    from silver_cli.transport import Client, CliError

    for path in ("/api/v1/file", "/api/v1/html", "/api/v1/bad-json"):
        with pytest.raises(CliError):
            Client(server[0]).request("GET", path)


@pytest.mark.parametrize("payload", [{"success": True, "data": []}, {"success": True, "data": "bad"},
                                     {"success": True, "data": {"csrf_token": "bad\nheader"}}])
def test_malformed_login_is_structured_error(monkeypatch, payload):
    from silver_cli.transport import Client, CliError

    client = Client("http://localhost")
    monkeypatch.setattr(client, "request", lambda *args, **kwargs: payload)
    with pytest.raises(CliError) as caught:
        client.login("test", "private")
    assert caught.value.exit_code == 3


def test_transport_timeout_and_form_encoding(server, monkeypatch):
    from urllib.error import URLError
    from silver_cli.transport import Client, CliError

    client = Client(server[0])
    client.request("POST", "/api/v1/form", form=[("paths", "a b"), ("paths", "c")])
    assert server[1][-1][3] == b"paths=a+b&paths=c"

    def unavailable(*args, **kwargs):
        raise URLError("a-secret-in-a-network-error")

    monkeypatch.setattr(client.opener, "open", unavailable)
    with pytest.raises(CliError) as caught:
        client.request("GET", "/api/v1/health")
    assert caught.value.exit_code == 6
    assert "a-secret" not in caught.value.message


def test_invalid_transport_inputs_before_open(server, monkeypatch):
    from silver_cli.transport import Client, CliError

    client = Client(server[0])
    for method, kwargs in [("TRACE", {}), ("GET", {"json_body": {}}),
                           ("POST", {"json_body": {}, "form": [("key", "value")]}),
                           ("POST", {"stream": True})]:
        with pytest.raises(CliError):
            client.request(method, "/api/v1/health", **kwargs)
    with pytest.raises(CliError):
        list(client.request("GET", "/api/v1/health", stream=True))
    assert len(server[1]) == 1
