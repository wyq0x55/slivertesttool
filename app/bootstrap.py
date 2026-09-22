"""Explicit one-owner bootstrap for persistent application state.

The Flask application factory is intentionally side-effect-light: constructing an
app must not create or alter database schema, migrate files, seed users, or run
retention jobs. Those operations belong here so a deployment can nominate one
process as the bootstrap owner before web, worker, and collab processes start.
"""

from __future__ import annotations

import logging

from flask import Flask

from .config import Config
from .extensions import db


def bootstrap_app(app: Flask) -> None:
    """Create or upgrade persistent state for an application.

    The operations are idempotent, but deployments should still call this from
    exactly one owner process. run.py does so before spawning worker/collab;
    split-process deployments should run python manage.py bootstrap first.
    """
    config_object = getattr(app, "config_obj", Config)
    config_object.ensure_dirs()

    with app.app_context():
        # Import models so SQLAlchemy has complete metadata before create_all.
        from . import models  # noqa: F401
        from .services import license_service

        db.create_all()
        _migrate_schema()

        try:
            from .services.lanmatrix import projects_service as _lm_projects
            _lm_projects.backfill_sheet_fields()
        except Exception as exc:  # noqa: BLE001 - bootstrap remains best-effort here
            logging.getLogger(__name__).warning(
                "const/lib field backfill skipped: %s", exc)

        try:
            from .services import project_model_service as _lm_models
            moved = _lm_models.migrate_model_dirs(config_object)
            if moved:
                logging.getLogger(__name__).info(
                    "model dirs migrated to project-code names: %d", moved)
        except Exception as exc:  # noqa: BLE001
            logging.getLogger(__name__).warning(
                "model dir migration skipped: %s", exc)

        license_service.init_defaults(config_object.LICENSE_LIMIT)

        try:
            from .services.lanmatrix import trash_service as _lm_trash
            swept = _lm_trash.purge_expired()
            if sum(swept.values()):
                logging.getLogger(__name__).info(
                    "recycle bin retention sweep: %s", swept)
        except Exception as exc:  # noqa: BLE001
            logging.getLogger(__name__).warning(
                "recycle bin retention sweep skipped: %s", exc)

        _seed_lanmatrix_admin(app)


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
            "deleted_at": "TIMESTAMP",
            "deleted_by": "INTEGER",
            # Re-runs reuse the task row, so the counter is the only way to see
            # that a retest actually executed when the verdict is unchanged.
            "run_count": "INTEGER NOT NULL DEFAULT 1",
        },
        "lm_projects": {
            "tm_id_prefix": "VARCHAR(64)",
            "tm_summary_sheet": "VARCHAR(120)",
            # Which verdicts require reviewer sign-off (per-project policy).
            "review_required_on": "JSONB",
            # Who reviews when a row names no reviewer of its own.
            "default_reviewer_id": "INTEGER",
            # Per-テスト区分 reviewer routing rules (ordered, first match wins).
            "review_routes": "JSONB",
        },
        "lm_notifications": {
            # Retention ages rows by read_at; archived rows leave the bell but
            # stay in history.
            "read_at": "TIMESTAMP",
            "archived_at": "TIMESTAMP",
        },
        "lm_field_definitions": {
            "sheet": "VARCHAR(16) NOT NULL DEFAULT 'test'",
            "deleted_at": "TIMESTAMP",
            "deleted_by": "INTEGER",
        },
        "lm_test_items": {
            "sheet": "VARCHAR(16) NOT NULL DEFAULT 'test'",
            "deleted_by": "INTEGER",
            # Review sign-off state machine (see TestItemRow).
            "review_status": "VARCHAR(16) NOT NULL DEFAULT ''",
            "reviewer_id": "INTEGER",
            "review_note": "TEXT NOT NULL DEFAULT ''",
            "review_requested_at": "TIMESTAMP",
            # Notification target for a review decision (see TestItemRow).
            "review_requested_by": "INTEGER",
            "reviewed_at": "TIMESTAMP",
            "review_verdict": "VARCHAR(24) NOT NULL DEFAULT ''",
            # Scope-exemption sign-off for 項目作成 = 不要 (see TestItemRow).
            "exempt_status": "VARCHAR(16) NOT NULL DEFAULT ''",
            "exempt_value": "VARCHAR(24) NOT NULL DEFAULT ''",
            "exempt_note": "TEXT NOT NULL DEFAULT ''",
            "exempt_reviewer_id": "INTEGER",
            "exempt_requested_at": "TIMESTAMP",
            "exempt_requested_by": "INTEGER",
            "exempt_decided_at": "TIMESTAMP",
        },
        "lm_project_models": {
            "is_current": "BOOLEAN NOT NULL DEFAULT FALSE",
            # Release identity of the plant model, stamped onto every row it
            # produces a verdict for and used to group the dashboard charts.
            "version": "VARCHAR(64)",
            "version_note": "TEXT NOT NULL DEFAULT ''",
            "deprecated_at": "TIMESTAMP",
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

    # Every recycle-bin listing and every "live rows only" query filters on
    # deleted_at, so the freshly-added columns need the index the model declares
    # (ADD COLUMN does not create one).
    for table in ("tasks", "lm_field_definitions"):
        if table not in existing_tables:
            continue
        try:
            with db.engine.begin() as conn:
                conn.execute(text(
                    f"CREATE INDEX IF NOT EXISTS ix_{table}_deleted_at "
                    f"ON {table} (deleted_at)"))
        except Exception as exc:  # noqa: BLE001
            logging.getLogger(__name__).warning(
                "schema migration: could not index %s.deleted_at: %s", table, exc)

    # The bell's two tabs filter on these, and the retention sweep scans
    # read_at across every user. ADD COLUMN creates no index.
    if "lm_notifications" in existing_tables:
        for column in ("read_at", "archived_at"):
            try:
                with db.engine.begin() as conn:
                    conn.execute(text(
                        f"CREATE INDEX IF NOT EXISTS ix_notif_{column} "
                        f"ON lm_notifications ({column})"))
            except Exception as exc:  # noqa: BLE001
                logging.getLogger(__name__).warning(
                    "schema migration: could not index lm_notifications.%s: %s",
                    column, exc)

    # The 不要 (項目作成) sign-off queue filters on these three, both per project
    # and across projects for "my queue". The model declares them indexed, but
    # ADD COLUMN on an upgraded database creates no index.
    if "lm_test_items" in existing_tables:
        for column in ("exempt_status", "exempt_reviewer_id",
                       "exempt_requested_by"):
            try:
                with db.engine.begin() as conn:
                    conn.execute(text(
                        f"CREATE INDEX IF NOT EXISTS ix_lm_items_{column} "
                        f"ON lm_test_items ({column})"))
            except Exception as exc:  # noqa: BLE001
                logging.getLogger(__name__).warning(
                    "schema migration: could not index lm_test_items.%s: %s",
                    column, exc)

    _migrate_widen_testitem_uuid(existing_tables, inspector)
    _migrate_user_fk_ondelete(inspector)
    _migrate_testitem_field_keys(existing_tables)


def _migrate_widen_testitem_uuid(existing_tables, inspector) -> None:
    """Widen ``lm_test_items.uuid`` so a client-generated UUID fits.

    The column was created as ``VARCHAR(32)`` to hold the server default
    (``uuid.uuid4().hex`` -- 32 chars, no dashes). But in collaborative mode the
    browser/CRDT layer mints canonical **36-char dashed** UUIDs, and the
    materializer persists each row under that same uuid *verbatim* (it also
    writes the authoritative id/version back into the Y.Doc keyed by uuid, so
    the stored value must equal the doc's uuid exactly -- it can never be
    truncated). Inserting such a row into ``VARCHAR(32)`` overflowed the column
    and every collab-created row failed with ``DataError: value too long for
    type character varying(32)`` -- which is why freshly inserted rows vanished
    on save while imported rows (32-hex ids) survived.

    ``db.create_all()`` never alters an existing column, so widen it in place
    here. Idempotent: only runs on PostgreSQL when the current length is under
    64. (SQLite does not enforce VARCHAR length, so the step is skipped there.)
    """
    if "lm_test_items" not in existing_tables:
        return
    if db.engine.dialect.name != "postgresql":
        return

    from sqlalchemy import text

    log = logging.getLogger(__name__)
    try:
        col = next((c for c in inspector.get_columns("lm_test_items")
                    if c["name"] == "uuid"), None)
    except Exception as exc:  # noqa: BLE001
        log.warning("schema migration: could not inspect lm_test_items.uuid: %s", exc)
        return
    if col is None:
        return
    length = getattr(col.get("type"), "length", None)
    if length is not None and length >= 64:
        return  # already wide enough
    try:
        with db.engine.begin() as conn:
            conn.execute(text(
                "ALTER TABLE lm_test_items ALTER COLUMN uuid TYPE VARCHAR(64)"))
        log.info("schema migration: widened lm_test_items.uuid to VARCHAR(64)")
    except Exception as exc:  # noqa: BLE001
        log.warning(
            "schema migration: could not widen lm_test_items.uuid: %s", exc)


def _migrate_testitem_field_keys(existing_tables) -> None:
    """One-time data migration for the unified ("identity") field protocol.

    The Test-Matrix editor now speaks a single field vocabulary (``test_name``,
    ``remark``, …). Two of those keys alias onto existing first-class columns
    (``test_name`` -> ``title``, ``remark`` -> ``comment``; see
    ``TestItemRow._FIELD_ALIASES``). Rows written before the unification stored
    those values in the ``custom_values`` JSONB bag instead, which left the
    backing columns empty and therefore invisible to quick-search / sort /
    filter. This migration lifts any such legacy value into its column and drops
    the key from the JSONB bag.

    Idempotent: it only touches rows that still carry a legacy alias key, and it
    never overwrites a column that already holds a value, so repeated boots and a
    rolling upgrade (old + new workers side by side) are both safe.
    """
    if "lm_test_items" not in existing_tables:
        return

    from sqlalchemy import text

    from .models import TestItemRow

    aliases = TestItemRow._FIELD_ALIASES
    keys = list(aliases)
    log = logging.getLogger(__name__)

    try:
        query = TestItemRow.query
        if db.engine.dialect.name == "postgresql":
            # Select only rows that still carry a legacy alias key in the JSONB
            # bag, so steady-state boots scan (almost) nothing.
            query = query.filter(
                text("jsonb_exists_any(custom_values, :alias_keys)")
                .bindparams(alias_keys=keys))
        else:
            query = query.filter(TestItemRow.custom_values.isnot(None))
        rows = query.all()
    except Exception as exc:  # noqa: BLE001 - never block startup
        db.session.rollback()
        log.warning("field-key migration: row scan failed: %s", exc)
        return

    changed = 0
    for row in rows:
        cv = dict(row.custom_values or {})
        touched = False
        for key, col in aliases.items():
            if key not in cv:
                continue
            val = cv.pop(key)
            # Only fill an empty column; never clobber an explicit column edit.
            if not (getattr(row, col) or "").strip():
                setattr(row, col, "" if val is None else val)
            touched = True
        if touched:
            row.custom_values = cv
            changed += 1

    if not changed:
        return
    try:
        db.session.commit()
        log.info("field-key migration: unified %d legacy row(s)", changed)
    except Exception as exc:  # noqa: BLE001
        db.session.rollback()
        log.warning("field-key migration: commit failed: %s", exc)


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
