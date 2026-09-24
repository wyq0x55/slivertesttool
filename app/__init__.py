"""Application factory for the Silver streaming test platform.

Creates and wires the Flask application without mutating persistent state.
Schema/data/filesystem bootstrap is owned by app.bootstrap and must be run
explicitly before standalone web/worker/collab processes. The all-in-one
run.py entry point performs that bootstrap once before spawning children.
"""

from __future__ import annotations

from flask import Flask

from .config import Config
from .extensions import db

__version__ = "2.13.0"


def create_app(config_object: type[Config] = Config) -> Flask:
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

    @app.context_processor
    def _inject_globals() -> dict:
        return {"app_version": __version__}

    _install_static_compression(app)

    return app


def _install_static_compression(app: Flask) -> None:
    """Serve large static assets efficiently without stale vendor deployments.

    Vendor bundles keep stable filenames for the offline deployment workflow,
    so template URLs carry a short content revision. Only a URL whose revision
    matches the current file receives one-year immutable caching. Rebuilding a
    bundle therefore changes its URL without renaming the checked-in artifact.

    Large text assets are gzip-compressed on demand. The in-process gzip cache
    is keyed by the file revision rather than content length, so a same-size
    watch rebuild cannot reuse stale compressed bytes.
    """
    import gzip as _gzip
    import hashlib as _hashlib
    from pathlib import Path as _Path

    from flask import request, url_for

    static_root = _Path(app.static_folder).resolve()
    revision_cache: dict[str, tuple[int, int, str]] = {}
    gzip_cache: dict[str, tuple[str, bytes]] = {}
    min_size = 2048

    def _revision(filename: str) -> str:
        """Return a short content revision, recomputing after a file change."""
        try:
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

    @app.after_request
    def _compress_and_cache(resp):  # noqa: ANN001, ANN202
        try:
            path = request.path or ""
            static_prefix = (app.static_url_path or "/static").rstrip("/") + "/"
            if not path.startswith(static_prefix):
                return resp
            filename = path[len(static_prefix):]
            current_rev = _revision(filename)

            if "/vendor/" in path:
                requested_rev = (request.args.get("v") or "").strip()
                if current_rev and requested_rev == current_rev:
                    resp.headers["Cache-Control"] = "public, max-age=31536000, immutable"
                else:
                    resp.headers["Cache-Control"] = "no-cache"

            if not path.endswith((".js", ".css", ".json", ".svg", ".map")):
                return resp
            if resp.status_code != 200:
                return resp

            accept = request.headers.get("Accept-Encoding", "")
            if "gzip" not in accept.lower() or resp.headers.get("Content-Encoding"):
                return resp

            clen = resp.content_length
            change_key = current_rev or f"len:{clen}"
            hit = gzip_cache.get(path)
            if hit is not None and hit[0] == change_key:
                gz = hit[1]
            else:
                resp.direct_passthrough = False
                data = resp.get_data()
                if len(data) < min_size:
                    return resp
                gz = _gzip.compress(data, compresslevel=6)
                gzip_cache[path] = (change_key, gz)

            resp.set_data(gz)
            resp.headers["Content-Encoding"] = "gzip"
            resp.headers["Content-Length"] = str(len(gz))
            vary = resp.headers.get("Vary")
            if not vary:
                resp.headers["Vary"] = "Accept-Encoding"
            elif "accept-encoding" not in vary.lower():
                resp.headers["Vary"] = vary + ", Accept-Encoding"
        except Exception:  # noqa: BLE001 - never break a response over compression/cache
            return resp
        return resp

def _install_auth_gate(app: Flask) -> None:
    """Gate the whole site behind the Matrix Editor (lanmatrix) login.

    Unauthenticated page requests are redirected to ``/lanmatrix/login`` (with a
    ``next`` param); unauthenticated API/SSE requests get a 401 JSON envelope so
    the browser fetch layer can react without parsing an HTML redirect.
    """
    if not app.config.get("GLOBAL_LOGIN_REQUIRED", True):
        return

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
    }

    @app.before_request
    def _require_login():  # noqa: ANN202
        if app.config.get("TESTING"):
            return None
        endpoint = request.endpoint or ""
        if endpoint in open_endpoints:
            return None
        # Allow blueprint-specific static handlers and 404s to pass through.
        if endpoint.endswith(".static") or endpoint == "":
            return None
        if _current_user() is not None:
            return None
        if request.path.startswith("/api/"):
            return jsonify(
                success=False, data=None,
                error={"code": "UNAUTHENTICATED",
                       "message": "未登录或会话已过期", "details": None},
                request_id="req-authgate",
            ), 401
        return redirect("/lanmatrix/login?next=" + quote(request.full_path))

    return


