"""Application factory for the Silver streaming test platform.

Creates and wires the Flask application without mutating persistent state.
Schema/data/filesystem bootstrap is owned by app.bootstrap and must be run
explicitly before standalone web/worker/collab processes. The all-in-one
run.py entry point performs that bootstrap once before spawning children.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from flask import Flask
    from .config import Config

__version__ = "2.13.0"


def create_app(config_object: type[Config] | None = None) -> Flask:
    from flask import Flask

    from .config import Config as _DefaultConfig
    from .extensions import configure_utc_connections, db

    if config_object is None:
        config_object = _DefaultConfig
    app = Flask(
        __name__,
        instance_path=str(config_object.INSTANCE_DIR),
        template_folder="templates",
        static_folder="static",
    )
    app.config.from_object(config_object)
    # Convenience handle for code that wants the raw config class (e.g. runners).
    app.config_obj = config_object

    # Fail fast on an insecure signing key. SECRET_KEY signs BOTH the session
    # cookie and the collab WebSocket tokens; shipping the known fallback value
    # would let anyone forge admin sessions and project-room tokens. Refuse to
    # boot unless the operator explicitly opts into the insecure key for a
    # throwaway/dev run (LM_ALLOW_INSECURE_SECRET=1) or is running tests.
    if (
        app.config.get("SECRET_KEY") == getattr(
            config_object, "INSECURE_SECRET_SENTINEL", None)
        and not getattr(config_object, "ALLOW_INSECURE_SECRET", False)
        and not app.config.get("TESTING")
    ):
        raise RuntimeError(
            "Refusing to start: SECRET_KEY is unset and is using the insecure "
            "built-in default. Set a strong random SECRET_KEY in the environment "
            "(e.g. `python -c \"import secrets; print(secrets.token_hex(32))\"`). "
            "For a disposable dev run only, set LM_ALLOW_INSECURE_SECRET=1."
        )

    db.init_app(app)
    configure_utc_connections(app)

    from .routes.lanmatrix import BLUEPRINTS as lanmatrix_api_blueprints
    from .routes.lanmatrix_pages import pages_bp as lanmatrix_pages_bp
    from .routes.page_routes import page_bp

    app.register_blueprint(page_bp)
    # LAN Test Matrix online-editing platform — merged into the platform's own
    # model / route / service layers (see app.models.lanmatrix,
    # app.routes.lanmatrix_*, app.services.lanmatrix). The former ``/api/v1``
    # God module was split by business boundary into five blueprints
    # (auth, projects_items, tasks, admin_db, admin_console).
    for _lm_bp in lanmatrix_api_blueprints:
        app.register_blueprint(_lm_bp)
    app.register_blueprint(lanmatrix_pages_bp)

    _install_auth_gate(app)
    _install_api_errors(app)

    @app.context_processor
    def _inject_globals() -> dict:
        return {"app_version": __version__}

    _install_static_compression(app)

    return app


def _install_api_errors(app: Flask) -> None:
    from flask import request
    from werkzeug.exceptions import HTTPException, InternalServerError

    from .routes.lanmatrix._base import _http_error, _unexpected

    @app.errorhandler(HTTPException)
    def api_http_error(exc):
        if request.path == "/api/v1" or request.path.startswith("/api/v1/"):
            return _http_error(exc)
        return exc.get_response()

    @app.errorhandler(Exception)
    def api_internal_error(exc):
        if request.path == "/api/v1" or request.path.startswith("/api/v1/"):
            return _unexpected(exc)
        app.logger.exception("Unhandled request error")
        return InternalServerError().get_response()


def _install_static_compression(app: Flask) -> None:
    """Serve static assets efficiently without stale deployments.

    Assets keep stable filenames (vendor bundles for the offline deployment
    workflow, the app's own JS/CSS for readable templates), so template URLs
    carry a short content revision. Only a URL whose revision matches the
    current file receives one-year immutable caching; any other request is
    revalidated. Changing a file therefore changes its URL without renaming it,
    and a deployment never pairs a new page with a cached old script.

    Large text assets are gzip-compressed on demand. The in-process gzip cache
    is keyed by the file revision rather than content length, so a same-size
    watch rebuild cannot reuse stale compressed bytes.
    """
    import gzip as _gzip
    import hashlib as _hashlib
    from pathlib import Path as _Path

    from flask import request, url_for

    revision_cache: dict[str, tuple[int, int, str]] = {}
    gzip_cache: dict[str, tuple[str, bytes]] = {}
    min_size = 2048

    def _static_root() -> _Path:
        return _Path(app.static_folder).resolve()

    def _revision(filename: str) -> str:
        """Return a short content revision, recomputing after a file change."""
        try:
            static_root = _static_root()
            target = (static_root / filename).resolve()
            target.relative_to(static_root)
            st = target.stat()
        except (OSError, ValueError):
            return ""
        cached = revision_cache.get(filename)
        if cached is not None and cached[0] == st.st_mtime_ns and cached[1] == st.st_size:
            return cached[2]
        digest = _hashlib.blake2b(digest_size=8)
        try:
            with target.open("rb") as fh:
                for chunk in iter(lambda: fh.read(1024 * 1024), b''):
                    digest.update(chunk)
        except OSError:
            return ""
        rev = digest.hexdigest()
        revision_cache[filename] = (st.st_mtime_ns, st.st_size, rev)
        return rev

    @app.template_global("static_asset_url")
    def _static_asset_url(filename: str) -> str:
        rev = _revision(filename)
        if rev:
            return url_for("static", filename=filename, v=rev)
        return url_for("static", filename=filename)

    def _accepts_gzip(resp) -> bool:  # noqa: ANN001
        accept = request.headers.get("Accept-Encoding", "")
        return "gzip" in accept.lower() and not resp.headers.get("Content-Encoding")

    def _send_gzipped(resp, gz: bytes) -> None:  # noqa: ANN001
        resp.set_data(gz)
        resp.headers["Content-Encoding"] = "gzip"
        resp.headers["Content-Length"] = str(len(gz))
        vary = resp.headers.get("Vary")
        if not vary:
            resp.headers["Vary"] = "Accept-Encoding"
        elif "accept-encoding" not in vary.lower():
            resp.headers["Vary"] = vary + ", Accept-Encoding"

    @app.after_request
    def _compress_and_cache(resp):  # noqa: ANN001, ANN202
        try:
            path = request.path or ""
            if path.startswith("/api/"):
                # JSON lists compress 10-20x (the matrix item page is ~1.3 MB raw)
                # and task lists are re-polled every few seconds. Responses are
                # per-request, so compress without caching; SSE streams are
                # direct-passthrough and not JSON, so they are left alone.
                if (resp.status_code == 200 and resp.mimetype == "application/json"
                        and not resp.direct_passthrough and _accepts_gzip(resp)):
                    data = resp.get_data()
                    if len(data) >= min_size:
                        _send_gzipped(resp, _gzip.compress(data, compresslevel=6))
                return resp
            static_prefix = (app.static_url_path or "/static").rstrip("/") + "/"
            if not path.startswith(static_prefix):
                return resp
            filename = path[len(static_prefix):]
            current_rev = _revision(filename)

            requested_rev = (request.args.get("v") or "").strip()
            if current_rev and requested_rev == current_rev:
                resp.headers["Cache-Control"] = "public, max-age=31536000, immutable"
            else:
                # Unversioned (e.g. url() inside a stylesheet) or stale: revalidate.
                resp.headers["Cache-Control"] = "no-cache"

            if not path.endswith((".js", ".css", ".json", ".svg", ".map", ".wasm")):
                return resp
            if resp.status_code != 200:
                return resp

            if not _accepts_gzip(resp):
                return resp

            clen = resp.content_length
            change_key = current_rev or f"len:{clen}"
            hit = gzip_cache.get(path)
            if hit is not None and hit[0] == change_key:
                gz = hit[1]
                closer = getattr(resp.response, "close", None)
                if callable(closer):
                    closer()
                resp.direct_passthrough = False
            else:
                if clen is not None and clen < min_size:
                    return resp
                gz = None
                try:
                    static_root = _static_root()
                    src_file = (static_root / filename).resolve()
                    src_file.relative_to(static_root)
                    sidecar = src_file.parent / (src_file.name + ".gz")
                    if (sidecar.is_file()
                            and not sidecar.is_symlink()
                            and sidecar.stat().st_mtime_ns >= src_file.stat().st_mtime_ns
                            and src_file.stat().st_size >= min_size):
                        gz = sidecar.read_bytes()
                except (OSError, ValueError):
                    gz = None
                if gz is not None:
                    closer = getattr(resp.response, "close", None)
                    if callable(closer):
                        closer()
                    resp.direct_passthrough = False
                else:
                    resp.direct_passthrough = False
                    data = resp.get_data()
                    if len(data) < min_size:
                        return resp
                    gz = _gzip.compress(data, compresslevel=6)
                gzip_cache[path] = (change_key, gz)

            _send_gzipped(resp, gz)
        except Exception:  # noqa: BLE001 - never break a response over compression/cache
            return resp
        return resp

def _install_auth_gate(app: Flask) -> None:
    """Gate the whole site behind the Matrix Editor (lanmatrix) login.

    Unauthenticated page requests are redirected to ``/lanmatrix/login`` (with a
    ``next`` param); unauthenticated API/SSE requests get a 401 JSON envelope so
    the browser fetch layer can react without parsing an HTML redirect.
    """
    from urllib.parse import quote

    from flask import jsonify, redirect, request

    from .routes.lanmatrix_pages import _current_user

    # Endpoints reachable without a session (login bootstrap + static assets).
    open_endpoints = {
        "static",
        "lanmatrix_pages.login",
        "lanmatrix_pages.register",
        "lanmatrix_auth.login",
        "lanmatrix_auth.register",
        "lanmatrix_auth.logout",
        "lanmatrix_auth.me",
        "lanmatrix_auth.health",
        "lanmatrix_auth.download_cli",
    }

    @app.before_request
    def _require_login():  # noqa: ANN202
        endpoint = request.endpoint or ""
        if endpoint in open_endpoints:
            return None
        # Allow blueprint-specific static handlers and 404s to pass through.
        if endpoint.endswith(".static") or endpoint == "":
            return None
        if request.path.startswith("/api/"):
            from .routes.lanmatrix._base import current_user
            user = current_user()
        else:
            user = _current_user()
        if user is not None and user.must_change_password and endpoint not in {
                "lanmatrix_auth.change_password", "lanmatrix_auth.me",
                "lanmatrix_auth.logout", "lanmatrix_pages.password",
                "lanmatrix_pages.logout"}:
            if request.path.startswith("/api/"):
                return jsonify(
                    success=False, data=None,
                    error={"code": "PASSWORD_CHANGE_REQUIRED",
                           "message": "请先修改初始密码后再继续使用", "details": None},
                    request_id="req-authgate",
                ), 403
            return redirect("/lanmatrix/password")
        if user is not None:
            return None
        if not app.config.get("GLOBAL_LOGIN_REQUIRED", True) or app.config.get("TESTING"):
            return None
        if request.path.startswith("/api/"):
            from .routes.lanmatrix._base import bearer_token, current_user
            if bearer_token() is not None and current_user() is not None:
                return None
            return jsonify(
                success=False, data=None,
                error={"code": "UNAUTHENTICATED",
                       "message": "未登录或会话已过期", "details": None},
                request_id="req-authgate",
            ), 401
        return redirect("/lanmatrix/login?next=" + quote(request.full_path))

    return


