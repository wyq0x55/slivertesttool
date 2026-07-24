"""Shared plumbing for the ``/api/v1`` LAN Test Matrix blueprints.

All ``/api/v1`` responses use the unified envelope. State-changing requests
require a valid session and a matching double-submit CSRF token
(``X-CSRF-Token``). Permissions are enforced server-side (the UI hiding buttons
is not sufficient).

The God-module ``lanmatrix_api`` was split by business boundary into five
blueprints (auth, projects_items, tasks, admin_db, admin_console). Each of them
shares this plumbing: the envelope helpers, the auth decorators, the CSRF guard
and the error handlers. Since a blueprint ``before_request``/``errorhandler``
only fires for that blueprint's own requests, :func:`register_common` attaches
the CSRF guard and the three error handlers to *every* blueprint.
"""

from __future__ import annotations

import functools
import secrets
import uuid
from typing import Any, Optional

from flask import g, jsonify, request, session

from ...extensions import db  # noqa: F401  (re-exported for route modules)
from ...services.lanmatrix import permissions, service, settings
from ...services.lanmatrix.permissions import PermissionDenied
from ...services.lanmatrix.service import ServiceError, VersionConflict

# Account-lockout policy is centrally configured via ``.env`` (LM_LOCK_*).
_LOCK_THRESHOLD = settings.LOCK_THRESHOLD
_LOCK_MINUTES = settings.LOCK_MINUTES


# --------------------------------------------------------------------------- #
# Envelope helpers
# --------------------------------------------------------------------------- #
def _request_id() -> str:
    rid = getattr(g, "request_id", None)
    if rid is None:
        rid = "req-" + uuid.uuid4().hex[:12]
        g.request_id = rid
    return rid


def ok(data: Any = None, status: int = 200):
    return jsonify(success=True, data=data, error=None, request_id=_request_id()), status


def err(code: str, message: str, *, details: Any = None, status: int = 400):
    return jsonify(
        success=False, data=None,
        error={"code": code, "message": message, "details": details},
        request_id=_request_id(),
    ), status


# --------------------------------------------------------------------------- #
# Auth plumbing
# --------------------------------------------------------------------------- #
def current_user() -> Optional[LMUser]:
    uid = session.get("lm_user_id")
    if uid is None:
        return None
    user = service.get_user(uid)
    if user is None or not user.is_active:
        return None
    return user


def login_required(fn):
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        user = current_user()
        if user is None:
            return err("UNAUTHENTICATED", "未登录或会话已过期", status=401)
        g.user = user
        return fn(*args, **kwargs)
    return wrapper


def system_admin_required(fn):
    """Gate an endpoint to the bootstrap system administrator only."""
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        user = current_user()
        if user is None:
            return err("UNAUTHENTICATED", "未登录或会话已过期", status=401)
        if not user.is_system_admin:
            return err("PERMISSION_DENIED", "仅系统管理员可访问", status=403)
        g.user = user
        return fn(*args, **kwargs)
    return wrapper


def bootstrap_admin_required(fn):
    """Gate an endpoint to the single bootstrap administrator only.

    Stricter than :func:`system_admin_required`: accounts merely granted the
    ``is_system_admin`` flag are rejected. Used for whole-database surfaces
    (the PostgreSQL console) that only the bootstrap ``admin`` account may use.
    """
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        user = current_user()
        if user is None:
            return err("UNAUTHENTICATED", "未登录或会话已过期", status=401)
        if not user.is_bootstrap_admin:
            return err("PERMISSION_DENIED", "仅系统 admin 账户可访问", status=403)
        g.user = user
        return fn(*args, **kwargs)
    return wrapper


# Endpoints exempt from CSRF: login bootstraps the token, logout only clears
# state. Both are safe without a pre-existing token (login is guarded by
# credentials; logout is idempotent). Endpoint names are blueprint-qualified;
# all three live on the auth blueprint.
_CSRF_EXEMPT = {
    "lanmatrix_auth.login", "lanmatrix_auth.logout", "lanmatrix_auth.register",
}


def _check_csrf() -> bool:
    if request.method in ("GET", "HEAD", "OPTIONS"):
        return True
    if request.endpoint in _CSRF_EXEMPT:
        return True
    token = request.headers.get("X-CSRF-Token", "")
    return bool(token) and secrets.compare_digest(token, session.get("csrf_token", ""))


def _csrf_guard():
    if not _check_csrf():
        return err("CSRF_FAILED", "CSRF 校验失败", status=403)


def _client_ip() -> str:
    return (request.headers.get("X-Forwarded-For", "") or request.remote_addr or "").split(",")[0].strip()


def _project_and_role(project_id: int, capability: str):
    project = service.get_project(project_id)
    role = service.role_in_project(project.id, g.user)
    permissions.require(capability, role, is_system_admin=g.user.is_system_admin)
    return project, role


# --------------------------------------------------------------------------- #
# Error handling
# --------------------------------------------------------------------------- #
def _perm(exc):
    return err("PERMISSION_DENIED", "没有该操作权限", details=str(exc), status=403)


def _conflict(exc):
    return err(exc.code, str(exc), details=exc.details, status=409)


def _service_err(exc):
    status = {"NOT_FOUND": 404, "DUPLICATE": 409, "PERMISSION_DENIED": 403}.get(exc.code, 400)
    return err(exc.code, str(exc), details=exc.details, status=status)


def register_common(bp) -> None:
    """Attach the shared CSRF guard and error handlers to *bp*.

    Blueprint ``before_request``/``errorhandler`` callbacks only fire for the
    blueprint's own requests, so this must be called for each blueprint that
    makes up ``/api/v1``.
    """
    bp.before_request(_csrf_guard)
    bp.register_error_handler(PermissionDenied, _perm)
    bp.register_error_handler(VersionConflict, _conflict)
    bp.register_error_handler(ServiceError, _service_err)


# ``LMUser`` is only needed for the :func:`current_user` return annotation; the
# import is deferred to module load to avoid a heavy models import at call time.
from ...models import LMUser  # noqa: E402
