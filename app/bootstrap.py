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


def reconcile_schema() -> None:
    """Reconcile the current application and Huey schema.

    SQLAlchemy model metadata is the application-table source of truth.
    Embedded Alembic revisions apply versioned schema and data upgrades for
    existing databases. Required schema/data migrations are fail-closed:
    callers must not start services on a partially upgraded database.
    """
    from . import models  # noqa: F401
    from .migrations import run_migrations

    if not db.session.new and not db.session.dirty and not db.session.deleted:
        db.session.rollback()

    db.create_all()
    run_migrations(db.engine)

    from .jobqueue.huey_app import ensure_huey_schema
    ensure_huey_schema()


def bootstrap_app(app: Flask) -> None:
    """Create or upgrade persistent state for an application.

    The operations are idempotent, but deployments should still call this from
    exactly one owner process. run.py does so before spawning worker/collab;
    split-process deployments should run python manage.py bootstrap first.
    """
    config_object = getattr(app, "config_obj", Config)
    config_object.ensure_dirs()

    with app.app_context():
        from .services import license_service

        reconcile_schema()

        # Required data/filesystem reconciliation is deployment correctness.
        # Abort bootstrap rather than serving partially upgraded state.
        from .services.lanmatrix import projects_service as _lm_projects
        _lm_projects.backfill_sheet_fields()

        from .services import project_model_service as _lm_models
        moved = _lm_models.migrate_model_dirs(config_object)
        if moved:
            logging.getLogger(__name__).info(
                "model dirs migrated to project-code names: %d", moved)

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
    """Re-apply idempotent Alembic revisions against ``db.engine``."""
    from . import models  # noqa: F401
    from .migrations import run_migrations

    if not db.session.new and not db.session.dirty and not db.session.deleted:
        db.session.rollback()

    run_migrations(db.engine, force_reconcile=True)



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
