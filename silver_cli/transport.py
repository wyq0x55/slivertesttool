"""Session-authenticated stdlib HTTP transport; no database or worker imports."""

from __future__ import annotations

import hashlib
import http.client
import http.cookiejar
import ipaddress
import json
import math
import mimetypes
import secrets
from contextlib import ExitStack
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import unquote, urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, HTTPCookieProcessor, ProxyHandler, Request, build_opener


JSON_LIMIT = 16 * 1024 * 1024
CHUNK_SIZE = 64 * 1024


class CliError(Exception):
    def __init__(self, code, message, exit_code, status=None, details=None, request_id=None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.exit_code = exit_code
        self.status = status
        self.details = details
        self.request_id = request_id


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, response, code, message, headers, new_url):
        return None


def _safe_segments(path):
    for segment in path.split("/"):
        decoded = unquote(segment)
        if decoded in {".", ".."} or any(char in decoded for char in "/\\%") or any(ord(char) < 32 or ord(char) == 127 for char in decoded):
            raise CliError("INVALID_URL", "Unsafe URL path.", 2)


def _header_value(value):
    if not value or any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise CliError("INVALID_FIELD", "Invalid multipart field or filename.", 2)
    return value.replace("\\", "\\\\").replace('"', '\\"')


class Multipart:
    def __init__(self, form, files, stack):
        self.boundary = "silver-cli-" + secrets.token_hex(24)
        self.parts = []
        self.length = 0
        for name, value in form:
            self._bytes((f'--{self.boundary}\r\nContent-Disposition: form-data; name="{_header_value(name)}"\r\n\r\n{value}\r\n').encode("utf-8"))
        for name, path in files:
            path = Path(path)
            if not path.is_file():
                raise CliError("INPUT_FILE", "Upload must reference a regular file.", 2)
            handle = stack.enter_context(path.open("rb"))
            size = path.stat().st_size
            mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
            header = (f'--{self.boundary}\r\nContent-Disposition: form-data; name="{_header_value(name)}"; filename="{_header_value(path.name)}"\r\nContent-Type: {mime}\r\n\r\n').encode("utf-8")
            self._bytes(header)
            self.parts.append((handle, size))
            self.length += size
            self._bytes(b"\r\n")
        self._bytes(f"--{self.boundary}--\r\n".encode("ascii"))

    def _bytes(self, data):
        self.parts.append(data)
        self.length += len(data)

    def __iter__(self):
        for part in self.parts:
            if isinstance(part, bytes):
                yield part
                continue
            handle, remaining = part
            while remaining:
                chunk = handle.read(min(CHUNK_SIZE, remaining))
                if not chunk:
                    raise OSError("Upload changed during transfer.")
                remaining -= len(chunk)
                yield chunk
            if handle.read(1):
                raise OSError("Upload changed during transfer.")


def _json(response):
    raw = response.read(JSON_LIMIT + 1)
    if len(raw) > JSON_LIMIT:
        raise CliError("RESPONSE_TOO_LARGE", "JSON response exceeds the safety limit; use pagination.", 7)
    try:
        value = json.loads(raw)
        if not isinstance(value, dict) or not isinstance(value.get("success"), bool):
            raise ValueError("Missing API envelope.")
        json.dumps(value, allow_nan=False)
        return value
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise CliError("INVALID_RESPONSE", "Server returned an invalid API JSON envelope.", 7) from exc


def _failure(status, payload=None):
    error = (payload or {}).get("error")
    error = error if isinstance(error, dict) else {}
    code = {401: 3, 403: 4, 409: 5, 423: 5}.get(status, 7 if status >= 500 or status < 400 else 2)
    return CliError(error.get("code") or "HTTP_ERROR", error.get("message") or "Server rejected the request.",
                    code, status, error.get("details"), (payload or {}).get("request_id"))


class Client:
    def __init__(self, base_url, timeout=30, allow_http=False):
        try:
            if not isinstance(base_url, str) or any(char.isspace() or char == "\\" for char in base_url):
                raise ValueError("Malformed base URL.")
            parsed = urlsplit(base_url)
            if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username is not None or parsed.password is not None or parsed.query or parsed.fragment:
                raise ValueError("Explicit HTTP(S) base URL required.")
            parsed.port
            _safe_segments(parsed.path)
            try:
                loopback = ipaddress.ip_address(parsed.hostname).is_loopback
            except ValueError:
                loopback = parsed.hostname == "localhost"
            if parsed.scheme == "http" and not loopback and not allow_http:
                raise ValueError("Remote HTTP requires explicit opt-in.")
            if not math.isfinite(timeout) or timeout <= 0:
                raise ValueError("Invalid timeout.")
        except (ValueError, TypeError) as exc:
            raise CliError("INVALID_URL", "Use an explicit HTTPS server URL, or --allow-http for trusted LAN HTTP; timeout must be positive.", 2) from exc
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.csrf_token = None
        self.opener = build_opener(ProxyHandler({}), HTTPCookieProcessor(http.cookiejar.CookieJar()), NoRedirect())

    def login(self, username, password):
        payload = self.request("POST", "/api/v1/auth/login", json_body={"username": username, "password": password})
        data = payload.get("data")
        token = data.get("csrf_token") if isinstance(data, dict) else None
        if not isinstance(token, str) or not token or any(ord(char) < 32 for char in token):
            raise CliError("INVALID_SESSION", "Login returned no usable CSRF token.", 3)
        self.csrf_token = token

    def _url(self, path, query):
        if not path.startswith("/api/v1/") or any(char in path for char in "?#\\") or "//" in path:
            raise CliError("INVALID_PATH", "Only catalogue API paths are supported.", 2)
        _safe_segments(path)
        suffix = urlencode(query)
        return self.base_url + path + ("?" + suffix if suffix else "")

    def _open(self, request):
        try:
            return self.opener.open(request, timeout=self.timeout)
        except HTTPError as exc:
            with exc:
                if 300 <= exc.code < 400:
                    raise CliError("REDIRECT_BLOCKED", "Redirects are disabled; use the final server base URL.", 6, exc.code) from exc
                payload = None
                if exc.headers.get_content_type() == "application/json":
                    try:
                        payload = _json(exc)
                    except CliError:
                        pass
                raise _failure(exc.code, payload) from exc

    def request(self, method, path, *, query=(), json_body=None, form=(), files=(), output=None, stream=False):
        if method not in {"GET", "POST", "PUT", "PATCH", "DELETE"}:
            raise CliError("METHOD", "Unsupported HTTP method.", 2)
        url = self._url(path, query)
        if json_body is not None and (form or files):
            raise CliError("BODY_CONFLICT", "Choose JSON or form/multipart, not both.", 2)
        if method == "GET" and (json_body is not None or form or files):
            raise CliError("READ_BODY", "GET requests cannot carry a body.", 2)
        if stream:
            if method != "GET" or output is not None:
                raise CliError("STREAM_INPUT", "Streams require GET without an output file.", 2)
            return self._stream(url)
        headers = {"Accept": "application/json, application/octet-stream", "User-Agent": "silver-cli/1"}
        if self.csrf_token and method != "GET":
            headers["X-CSRF-Token"] = self.csrf_token
        try:
            with ExitStack() as stack:
                data = None
                if files:
                    data = Multipart(form, files, stack)
                    headers["Content-Type"] = "multipart/form-data; boundary=" + data.boundary
                    headers["Content-Length"] = str(data.length)
                elif form:
                    data = urlencode(form).encode("utf-8")
                    headers["Content-Type"] = "application/x-www-form-urlencoded"
                elif json_body is not None:
                    data = json.dumps(json_body, ensure_ascii=True, allow_nan=False).encode("utf-8")
                    headers["Content-Type"] = "application/json"
                with self._open(Request(url, data=data, headers=headers, method=method)) as response:
                    mime = response.headers.get_content_type()
                    if mime == "application/json":
                        payload = _json(response)
                        if payload["success"] is not True:
                            raise _failure(response.status, payload)
                        if output is not None:
                            raise CliError("EXPECTED_DOWNLOAD", "Server returned JSON rather than a downloadable artifact.", 7)
                        return payload
                    if mime in {"text/html", "text/event-stream"}:
                        raise CliError("INVALID_RESPONSE", "Unexpected HTML or event stream response.", 7)
                    if output is None:
                        raise CliError("OUTPUT_REQUIRED", "Binary responses require an explicit output path.", 2)
                    return self._download(response, Path(output))
        except (URLError, OSError, http.client.HTTPException) as exc:
            raise CliError("TRANSPORT_ERROR", "Request failed; writes may have reached the server. Inspect state before retrying.", 6) from exc

    def _download(self, response, output):
        digest = hashlib.sha256()
        total = 0
        created = False
        identity = None
        try:
            try:
                handle = output.open("xb")
            except OSError as exc:
                raise CliError("OUTPUT_PATH", "Output must be a new writable file; existing files are never overwritten.", 2) from exc
            created = True
            identity = output.stat()
            with handle:
                while True:
                    chunk = response.read(CHUNK_SIZE)
                    if not chunk:
                        break
                    handle.write(chunk)
                    digest.update(chunk)
                    total += len(chunk)
                length = response.headers.get("Content-Length")
                if length is not None and (not length.isdecimal() or total != int(length)):
                    raise CliError("INCOMPLETE_DOWNLOAD", "Download length does not match the server response.", 6)
            return {"success": True, "data": {"path": str(output.absolute()), "bytes": total,
                    "sha256": digest.hexdigest()}, "error": None, "request_id": None}
        except BaseException:
            if created and identity is not None:
                try:
                    if output.stat().st_ino == identity.st_ino:
                        output.unlink()
                except OSError:
                    pass
            raise

    def _stream(self, url):
        try:
            with self._open(Request(url, headers={"Accept": "text/event-stream"})) as response:
                if response.headers.get_content_type() != "text/event-stream":
                    raise CliError("INVALID_STREAM", "Expected an event stream.", 7)
                event_id = None
                event_name = "message"
                data = []
                size = 0
                while True:
                    raw = response.readline(1024 * 1024 + 1)
                    if not raw:
                        return
                    size += len(raw)
                    if len(raw) > 1024 * 1024 or size > JSON_LIMIT:
                        raise CliError("STREAM_TOO_LARGE", "Stream event exceeds the safety limit.", 7)
                    line = raw.decode("utf-8").rstrip("\r\n")
                    if not line:
                        if data:
                            content = "\n".join(data)
                            try:
                                content = json.loads(content)
                                json.dumps(content, allow_nan=False)
                            except ValueError:
                                content = "\n".join(data)
                            yield {"event": event_name, "id": event_id, "data": content}
                            if event_name == "end":
                                return
                        event_name, data, size = "message", [], 0
                        continue
                    field, separator, value = line.partition(":")
                    if value.startswith(" "):
                        value = value[1:]
                    if field == "event":
                        event_name = value
                    elif field == "id" and "\x00" not in value:
                        event_id = value
                    elif field == "data":
                        data.append(value)
        except (URLError, OSError, http.client.HTTPException, UnicodeError) as exc:
            raise CliError("TRANSPORT_ERROR", "Stream interrupted; resume with the last received event id.", 6) from exc
