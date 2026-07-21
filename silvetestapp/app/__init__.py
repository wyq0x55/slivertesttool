"""Application factory for the Silver streaming test platform.

Creates and wires the Flask app, initialises the PostgreSQL database, seeds the
license settings, and registers the page + API blueprints. The same factory is
used by the web server (``run_web.py``) and by the Huey worker so both share one
configuration, database and model set.
"""

from __future__ import annotations

import logging

from flask import Flask

from .config import Config
from .extensions import db

__version__ = "2.13.0"


def create_app(config_object: type[Config] = Config) -> Flask:
    config_object.ensure_dirs()

    app = Flask(
        __name__,
        instance_path=str(config_object.INSTANCE_DIR),
        template_folder="templates",
        static_folder="static",
    )
    app.config.from_object(config_object)
    # Convenience handle for code that wants the raw config class (e.g. runners).
    app.config_obj = config_object

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    db.init_app(app)

    with app.app_context():
        # Import models so SQLAlchemy is aware of them before create_all.
        from . import models  # noqa: F401
        from .services import license_service

        db.create_all()
        _migrate_schema()
        # Backfill const/lib sheet field definitions onto pre-existing projects
        # so their const/lib sheets have columns (and can persist rows) even if
        # created before those sheets existed. Idempotent; safe on every start.
        try:
            from .services.lanmatrix import projects_service as _lm_projects
            _lm_projects.backfill_sheet_fields()
        except Exception as exc:  # noqa: BLE001 - never block startup
            logging.getLogger(__name__).warning(
                "const/lib field backfill skipped: %s", exc)
        license_service.init_defaults(config_object.LICENSE_LIMIT)

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

    with app.app_context():
        _seed_lanmatrix_admin(app)

    _install_auth_gate(app)

    @app.context_processor
    def _inject_globals() -> dict:
        return {"app_version": __version__}

    return app


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


def _migrate_schema() -> None:
    """Additive, in-place migrations for upgraded databases.

    ``db.create_all()`` never alters existing tables, so newly-introduced
    columns are added here with ``ALTER TABLE ... ADD COLUMN`` when absent. Each
    step is idempotent and safe to run on every startup (PostgreSQL accepts this
    form). Covers the core ``tasks`` table and the merged LAN Test Matrix
    ``lm_*`` tables.
    """
    from sqlalchemy import inspect, text

    inspector = inspect(db.engine)
    try:
        existing_tables = set(inspector.get_table_names())
    except Exception:  # noqa: BLE001 - database may not be ready yet
        return

    additions = {
        "tasks": {
            "sil_name": "VARCHAR(128) NOT NULL DEFAULT ''",
            "project_id": "INTEGER",
            "submitter_id": "INTEGER",
        },
        "lm_projects": {
            "tm_id_prefix": "VARCHAR(64)",
            "tm_summary_sheet": "VARCHAR(120)",
        },
        "lm_field_definitions": {
            "sheet": "VARCHAR(16) NOT NULL DEFAULT 'test'",
        },
        "lm_test_items": {
            "sheet": "VARCHAR(16) NOT NULL DEFAULT 'test'",
        },
    }
    for table, columns in additions.items():
        if table not in existing_tables:
            continue
        have = {c["name"] for c in inspector.get_columns(table)}
        for name, ddl in columns.items():
            if name in have:
                continue
            try:
                with db.engine.begin() as conn:
                    conn.execute(text(
                        f"ALTER TABLE {table} ADD COLUMN {name} {ddl}"))
            except Exception as exc:  # noqa: BLE001
                logging.getLogger(__name__).warning(
                    "schema migration: could not add %s.%s: %s", table, name, exc)

    # The ``sheet`` columns are filtered on every field/item load, so back them
    # with an index (the ADD COLUMN above does not create one). Idempotent.
    for table in ("lm_field_definitions", "lm_test_items"):
        if table not in existing_tables:
            continue
        try:
            with db.engine.begin() as conn:
                conn.execute(text(
                    f"CREATE INDEX IF NOT EXISTS ix_{table}_sheet "
                    f"ON {table} (sheet)"))
        except Exception as exc:  # noqa: BLE001
            logging.getLogger(__name__).warning(
                "schema migration: could not index %s.sheet: %s", table, exc)

    _migrate_user_fk_ondelete(inspector)


def _migrate_user_fk_ondelete(inspector) -> None:
    """Unify the ``ON DELETE`` behaviour of every FK that points at ``lm_users``.

    Older databases created these foreign keys with the default ``NO ACTION``
    rule, so deleting a user that authored/owned a project, test item or comment
    raised a ``ForeignKeyViolation``. This reconciles them to a single policy:

    * membership rows (``lm_project_members.user_id``) → ``CASCADE``
    * authorship / ownership columns → ``SET NULL`` (history is preserved)

    Only PostgreSQL enforces these constraints; on SQLite the step is skipped
    (constraints are unenforced and ``ALTER TABLE ... DROP CONSTRAINT`` is
    unsupported). Every step is idempotent: a constraint is only rewritten when
    its current rule differs from the target.
    """
    from sqlalchemy import text

    if db.engine.dialect.name != "postgresql":
        return

    # (table, column) -> desired ON DELETE action
    targets = {
        ("lm_project_members", "user_id"): "CASCADE",
        ("lm_projects", "owner_id"): "SET NULL",
        ("lm_projects", "created_by"): "SET NULL",
        ("lm_test_items", "owner_id"): "SET NULL",
        ("lm_test_items", "created_by"): "SET NULL",
        ("lm_test_items", "updated_by"): "SET NULL",
        ("lm_cell_comments", "created_by"): "SET NULL",
    }
    log = logging.getLogger(__name__)
    try:
        existing_tables = set(inspector.get_table_names())
    except Exception:  # noqa: BLE001
        return

    for (table, column), action in targets.items():
        if table not in existing_tables:
            continue
        try:
            fks = inspector.get_foreign_keys(table)
        except Exception:  # noqa: BLE001
            continue
        for fk in fks:
            if fk.get("referred_table") != "lm_users":
                continue
            if list(fk.get("constrained_columns") or []) != [column]:
                continue
            name = fk.get("name")
            if not name:
                continue
            current = (fk.get("options") or {}).get("ondelete") or "NO ACTION"
            if current.upper() == action.upper():
                break  # already correct
            try:
                with db.engine.begin() as conn:
                    conn.execute(text(
                        f'ALTER TABLE {table} DROP CONSTRAINT "{name}"'))
                    conn.execute(text(
                        f'ALTER TABLE {table} ADD CONSTRAINT "{name}" '
                        f"FOREIGN KEY ({column}) REFERENCES lm_users(id) "
                        f"ON DELETE {action}"))
                log.info("schema migration: %s.%s FK -> ON DELETE %s",
                         table, column, action)
            except Exception as exc:  # noqa: BLE001
                log.warning("schema migration: could not update FK %s on %s.%s: %s",
                            name, table, column, exc)
            break


def _seed_lanmatrix_admin(app: Flask) -> None:
    """Seed — and reconcile — the bootstrap LAN Test Matrix administrator.

    Credentials come from the centralised configuration (``.env`` via
    :class:`app.config.Config`). When ``LM_ADMIN_PASSWORD`` is not set the
    built-in default is used and the admin is forced to change it on first login.

    Because the admin row is only ever created once, editing ``LM_ADMIN_PASSWORD``
    in ``.env`` after first start would otherwise have no effect (login would keep
    failing with the old password). To make ``.env`` the single source of truth we
    *reconcile* on every startup: when ``LM_ADMIN_PASSWORD`` is explicitly set, the
    bootstrap admin's password is (re)applied and any lock/disabled state cleared.
    Leaving ``LM_ADMIN_PASSWORD`` empty never overwrites an existing password.
    """
    from .models import LMUser

    cfg = getattr(app, "config_obj", Config)
    username = cfg.LM_ADMIN_USER
    explicit_password = (cfg.LM_ADMIN_PASSWORD or "").strip()

    # Prefer matching by the configured username; fall back to any existing
    # system admin so we reconcile the right row even if the name changed.
    admin = (LMUser.query.filter_by(username=username).first()
             or LMUser.query.filter_by(is_system_admin=True).first())

    if admin is not None:
        if not explicit_password:
            return  # nothing to reconcile without an explicit password
        changed = False
        if not admin.is_system_admin:
            admin.is_system_admin = True
            changed = True
        if admin.status != "active":
            admin.status = "active"
            changed = True
        if admin.locked_until is not None or (admin.failed_logins or 0):
            admin.locked_until = None
            admin.failed_logins = 0
            changed = True
        # Only rewrite the hash when the configured password no longer matches,
        # so we don't needlessly churn the row on every boot.
        if not admin.check_password(explicit_password):
            admin.set_password(explicit_password)
            admin.must_change_password = False
            changed = True
        if changed:
            db.session.commit()
            app.logger.info(
                "LAN Test Matrix: reconciled bootstrap admin '%s' from .env",
                admin.username)
        return

    password = explicit_password or cfg.LM_ADMIN_DEFAULT_PASSWORD
    admin = LMUser(
        username=username, display_name="System Administrator",
        status="active", is_system_admin=True,
        must_change_password=not explicit_password,
    )
    admin.set_password(password)
    db.session.add(admin)
    db.session.commit()
    app.logger.info("LAN Test Matrix: seeded bootstrap admin '%s'", username)
