"""Authentication + public endpoints for the LAN Test Matrix API (login, register, logout, session probe, health)."""

from __future__ import annotations

import datetime as _dt
import io
import json
import secrets
import zipfile
from pathlib import Path

from flask import (
    Blueprint, Response, current_app, g, request, send_file, session,
    stream_with_context,
)

from ...extensions import db
from ...models import DataJob, FieldDefinition, LMUser, Project, Task, TaskStatus
from ...services import (
    event_service, license_service, model_service, report_service,
    task_service, upload_service,
)
from ...services.upload_service import UploadError
from ...services.lanmatrix import (
    dbadmin, excel_service, fields, permissions, service, settings,
)
from ._base import (
    ok, err, current_user, login_required, system_admin_required,
    register_common, _project_and_role, _client_ip,
    _LOCK_THRESHOLD, _LOCK_MINUTES,
)

bp = Blueprint("lanmatrix_auth", __name__, url_prefix="/api/v1")
register_common(bp)


@bp.get("/config")
@login_required
def config():
    """Canonical editor/collab protocol config (single source of truth).

    Serves the sheet catalogue (keys + labels), the default sheet, the per-sheet
    steps field map and the CRDT/room naming prefixes so the frontend consumes
    one schema instead of re-declaring it in ``editor.js`` / ``collab.js``.
    """
    return ok(fields.matrix_config())

@bp.post("/auth/login")
def login():
    body = request.get_json(silent=True) or {}
    username = (body.get("username") or "").strip()
    password = body.get("password") or ""
    user = service.get_user_by_name(username)
    now = _dt.datetime.now(_dt.timezone.utc)

    if user and user.locked_until and user.locked_until.replace(tzinfo=_dt.timezone.utc) > now:
        return err("ACCOUNT_LOCKED", "账号已被临时锁定，请稍后再试", status=423)
    if user is None or not user.is_active or not user.check_password(password):
        if user is not None:
            user.failed_logins = (user.failed_logins or 0) + 1
            if user.failed_logins >= _LOCK_THRESHOLD:
                user.locked_until = now + _dt.timedelta(minutes=_LOCK_MINUTES)
                user.failed_logins = 0
            db.session.commit()
        return err("INVALID_CREDENTIALS", "用户名或密码错误", status=401)

    user.failed_logins = 0
    user.locked_until = None
    user.last_login_at = now
    db.session.commit()

    session.clear()
    session["lm_user_id"] = user.id
    session["csrf_token"] = secrets.token_hex(16)
    session.permanent = True
    return ok({"user": user.to_dict(), "csrf_token": session["csrf_token"]})

@bp.post("/auth/register")
def register():
    """Self-service registration for LAN users.

    Disabled unless ``LM_ALLOW_REGISTRATION`` is on. On success, an ``active``
    account is logged in immediately (session + CSRF token established, mirroring
    login); a ``disabled`` account (approval mode) is created in a pending state
    and the client is told to wait for an administrator.
    """
    if not settings.ALLOW_REGISTRATION:
        return err("REGISTRATION_DISABLED", "用户注册功能已关闭", status=403)

    body = request.get_json(silent=True) or {}
    user = service.register_user(
        (body.get("username") or "").strip(),
        body.get("password") or "",
        display_name=(body.get("display_name") or "").strip(),
        email=(body.get("email") or "").strip(),
    )

    if not user.is_active:
        return ok({"pending": True, "user": user.to_dict()}, status=201)

    session.clear()
    session["lm_user_id"] = user.id
    session["csrf_token"] = secrets.token_hex(16)
    session.permanent = True
    return ok(
        {"pending": False, "user": user.to_dict(),
         "csrf_token": session["csrf_token"]},
        status=201,
    )

@bp.post("/auth/logout")
def logout():
    session.clear()
    return ok({"logged_out": True})

@bp.get("/auth/me")
@login_required
def me():
    return ok({"user": g.user.to_dict(), "csrf_token": session.get("csrf_token")})

@bp.get("/health")
def health():
    db_ok = True
    try:
        db.session.execute(db.text("SELECT 1"))
    except Exception:  # noqa: BLE001
        db_ok = False
    from ... import __version__
    return ok({
        "web": "ok",
        "database": "ok" if db_ok else "error",
        "version": __version__,
    })
