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

    # Fallback only: every ``run_*.py`` entry point configures logging with its
    # own process role before importing the app, and ``configure`` is a no-op
    # once that has happened. This keeps a bare ``create_app()`` (a shell, a
    # test, a management script) from logging into the void.
    from .logging_setup import configure as _configure_logging
    # ``config_object`` (the class), not ``app.config`` (a dict): the helper
    # reads attributes so it can also run before an app exists.
    _configure_logging("cli", config_object)

    db.init_app(app)

    from .routes.api_routes import api_bp
    from .routes.lanmatrix import BLUEPRINTS as lanmatrix_api_blueprints
    from .routes.lanmatrix_pages import pages_bp as lanmatrix_pages_bp
    from .routes.page_routes import page_bp

    app.register_blueprint(page_bp)
    app.register_blueprint(api_bp)
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
    """Gzip large static JS/CSS on the fly and mark vendor bundles immutable.

    The Univer bundle (``univer.full.umd.js``) is ~11 MB uncompressed, which makes
    the first editor load painfully slow over a LAN. Waitress/Flask ship static
    files verbatim with no ``Content-Encoding`` and no useful cache lifetime, so
    every client re-downloads all 11 MB on every visit.

    This hook fixes both cheaply and offline (stdlib ``gzip`` only, no extra
    dependency):

    * Compresses text-like ``/static`` assets (js/css/json/svg/map) when the
      client advertises ``Accept-Encoding: gzip`` — ~11 MB drops to ~2.7 MB.
    * Caches the compressed bytes in-process keyed by path + original length, so
      only the *first* request per asset pays the CPU cost; the length key means a
      rebuilt bundle is transparently recompressed.
    * Tags ``/static/vendor/`` assets ``immutable`` with a one-year max-age so
      repeat visits skip the download entirely.
    """
    import gzip as _gzip

    from flask import request

    cache: dict[str, tuple[int, bytes]] = {}
    # Below this size gzip's framing overhead isn't worth the CPU round-trip.
    min_size = 2048

    @app.after_request
    def _compress_and_cache(resp):  # noqa: ANN001, ANN202
        try:
            path = request.path or ""
            if not path.startswith("/static/"):
                return resp
            if not path.endswith((".js", ".css", ".json", ".svg", ".map")):
                return resp

            # Long-lived immutable caching for versioned vendor bundles: they only
            # change when the file is replaced (new length -> new URL content), so
            # a year is safe and makes repeat loads instant.
            if "/vendor/" in path:
                resp.headers["Cache-Control"] = "public, max-age=31536000, immutable"

            if resp.status_code != 200:
                return resp

            accept = request.headers.get("Accept-Encoding", "")
            if "gzip" not in accept.lower():
                return resp
            if resp.headers.get("Content-Encoding"):
                return resp

            # Fast path: for a static file Flask has already set Content-Length to
            # the file size, so a cache hit needs neither reading nor recompressing
            # the (up to 11 MB) body. The length doubles as a cheap change key —
            # a rebuilt bundle has a different size and is transparently redone.
            clen = resp.content_length
            hit = cache.get(path)
            if clen is not None and hit is not None and hit[0] == clen:
                gz = hit[1]
            else:
                # Materialise the (possibly file-streamed) body to compress it.
                resp.direct_passthrough = False
                data = resp.get_data()
                if len(data) < min_size:
                    return resp
                gz = _gzip.compress(data, compresslevel=6)
                cache[path] = (len(data), gz)

            resp.set_data(gz)
            resp.headers["Content-Encoding"] = "gzip"
            resp.headers["Content-Length"] = str(len(gz))
            vary = resp.headers.get("Vary")
            if not vary:
                resp.headers["Vary"] = "Accept-Encoding"
            elif "accept-encoding" not in vary.lower():
                resp.headers["Vary"] = vary + ", Accept-Encoding"
        except Exception:  # noqa: BLE001 - never break a response over compression
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


