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

import datetime as _dt
import functools
import json
import secrets
import uuid
from typing import Any, Optional

from flask import g, jsonify, request, session

from ...extensions import db  # noqa: F401  (re-exported for route modules)
from ...services.lanmatrix import permissions, service, settings
from ...services.lanmatrix.permissions import PermissionDenied
from ...services.lanmatrix.errors import RequestParamError
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
# Query-string parsing
#
# Bare ``int(request.args.get(...))`` in a route turns ``?page=abc`` into an
# uncaught ValueError, i.e. a 500 -- the server blaming itself for the caller's
# typo. These helpers raise :class:`RequestParamError` instead, which the
# already-registered ServiceError handler renders as a 400.
#
# They are strict rather than forgiving on purpose. ``task_service.clamp_limit``
# deliberately goes the other way for ``?limit=``, but that endpoint reports a
# ``truncated`` flag so a clamped value is still visible to the caller. Here
# there is no such channel: silently serving page 1 to someone who asked for
# page 12, or 200 rows to someone who asked for 100000, is the same
# silent-truncation failure Phase 6 existed to remove.
# --------------------------------------------------------------------------- #
def arg_int(name: str, default=None, *, minimum: int | None = None,
            maximum: int | None = None):
    """Read ``name`` from the query string as a bounded int.

    Absent or empty yields *default* -- omitting an optional parameter is not an
    error. Anything present but unparseable, or outside [minimum, maximum],
    raises :class:`RequestParamError` (400).
    """
    raw = request.args.get(name)
    # An omitted parameter returns the default *unvalidated*: the default is our
    # own constant, so bounds-checking it would answer a request that never
    # mentioned `name` with "参数 name 不能小于 1" -- blaming the caller for our
    # own misconfiguration. Only caller-supplied values are validated.
    if raw is None or raw.strip() == "":
        return default
    try:
        # int() already tolerates surrounding whitespace, so no .strip() here;
        # the .strip() above is still needed, because "" and "   " must both
        # count as "omitted" and int("") raises.
        value = int(raw)
    except (TypeError, ValueError):
        raise RequestParamError(
            "参数 %s 必须是整数，收到：%s" % (name, raw), param=name)
    if minimum is not None and value < minimum:
        raise RequestParamError(
            "参数 %s 不能小于 %d，收到：%d" % (name, minimum, value), param=name)
    if maximum is not None and value > maximum:
        raise RequestParamError(
            "参数 %s 不能大于 %d，收到：%d" % (name, maximum, value), param=name)
    return value


def arg_str(name: str, default: Any = None, *, allowed=None, max_length: int = 64):
    """Read ``name`` from the query string as a bounded string.

    ``allowed`` restricts the value to a whitelist -- used for enum-ish filters
    (``result``, ``object_type``) so a typo returns 400 with the valid options
    rather than silently matching nothing, which reads as "no such records".
    """
    raw = request.args.get(name)
    if raw is None or raw.strip() == "":
        return default
    value = raw.strip()
    if len(value) > max_length:
        raise RequestParamError(
            "参数 %s 长度不能超过 %d" % (name, max_length), param=name)
    if allowed is not None and value not in allowed:
        raise RequestParamError(
            "参数 %s 只能是：%s" % (name, "、".join(sorted(allowed))), param=name)
    return value


def arg_date(name: str, default: Any = None, *, end_of_day: bool = False):
    """Read ``name`` as a date (``YYYY-MM-DD``) or a full ISO-8601 timestamp.

    ``end_of_day`` makes a bare date inclusive of that whole day. Without it,
    ``?date_to=2024-05-01`` would mean midnight and silently exclude everything
    that happened *on* the day the user asked for -- the classic off-by-one-day
    audit filter that makes people believe records are missing.
    """
    raw = request.args.get(name)
    if raw is None or raw.strip() == "":
        return default
    value = raw.strip()
    try:
        if len(value) == 10:
            d = _dt.datetime.strptime(value, "%Y-%m-%d")
            if end_of_day:
                d = d.replace(hour=23, minute=59, second=59, microsecond=999999)
            return d
        return _dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise RequestParamError(
            "参数 %s 必须是 YYYY-MM-DD 或 ISO-8601 时间，收到：%s" % (name, value),
            param=name)


def arg_json(name: str, default: Any = None) -> Any:
    """Read ``name`` from the query string as JSON.

    Absent or empty yields *default*. Malformed JSON raises
    :class:`RequestParamError` (400) rather than escaping as a
    ``json.JSONDecodeError`` -- which, being a ValueError subclass, was
    previously a 500.
    """
    raw = request.args.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return json.loads(raw)
    except ValueError:
        raise RequestParamError("参数 %s 不是合法的 JSON" % name, param=name)


# --------------------------------------------------------------------------- #
# Auth plumbing
# --------------------------------------------------------------------------- #
def current_user() -> Optional[LMUser]:
    uid = session.get("lm_user_id")
    if uid is None:
        return None
    # ``current_user`` is hit several times per request (login_required, each
    # permission check, the route body). Cache the resolved row on ``g`` for the
    # request so a single logical request does not fan out into N identical
    # ``SELECT ... FROM lm_users`` round-trips. Keyed by uid so a mid-request
    # session swap (rare) is not served a stale identity.
    cached = getattr(g, "_lm_current_user", None)
    if cached is not None and cached[0] == uid:
        return cached[1]
    user = service.get_user(uid)
    resolved = user if (user is not None and user.is_active) else None
    g._lm_current_user = (uid, resolved)
    return resolved


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
