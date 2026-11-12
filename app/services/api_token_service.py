"""Issue, list, revoke and authenticate personal API tokens.

A token acts as its owner with the owner's current roles; it never grants
more. The secret is shown once at creation and only its SHA-256 is stored.
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import secrets
from typing import Optional

from ..extensions import db
from ..models import ApiToken, LMUser
from .lanmatrix import audit
from .lanmatrix.errors import ServiceError

PREFIX = "slv_"
MAX_DAYS = 365
MAX_ACTIVE = 20
# last_used_at is advisory; writing it on every request would turn each read into a write.
_TOUCH_INTERVAL = _dt.timedelta(minutes=5)


def _now() -> _dt.datetime:
    return _dt.datetime.now(_dt.timezone.utc)


def _aware(value: Optional[_dt.datetime]) -> Optional[_dt.datetime]:
    if value is not None and value.tzinfo is None:
        return value.replace(tzinfo=_dt.timezone.utc)
    return value


def _digest(secret: str) -> str:
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()


def _active(query):
    now = _now()
    return query.filter(ApiToken.revoked_at.is_(None),
                        db.or_(ApiToken.expires_at.is_(None), ApiToken.expires_at > now))


def create(user: LMUser, name: str, days: object, *, replace: bool = False) -> tuple[ApiToken, str]:
    """Issue a token; with ``replace`` the owner's active tokens of the same name are revoked."""
    name = (name or "").strip()
    if not name or len(name) > 64:
        raise ServiceError("name must be 1-64 characters", code="VALIDATION_ERROR")
    if type(days) is not int or not 1 <= days <= MAX_DAYS:
        raise ServiceError(f"expires_days must be an integer from 1 to {MAX_DAYS}", code="VALIDATION_ERROR")
    now = _now()
    if replace:
        for old in _active(ApiToken.query.filter_by(user_id=user.id, name=name)).all():
            old.revoked_at = now
            audit.record("api_token.revoke", actor_id=user.id, object_type="api_token", object_id=old.id)
    if _active(ApiToken.query.filter_by(user_id=user.id)).count() >= MAX_ACTIVE:
        raise ServiceError(f"at most {MAX_ACTIVE} active tokens per user; revoke one first", code="VALIDATION_ERROR")
    secret = PREFIX + secrets.token_urlsafe(32)
    token = ApiToken(user_id=user.id, name=name, token_hash=_digest(secret), prefix=secret[:12],
                     created_at=now, expires_at=now + _dt.timedelta(days=days))
    db.session.add(token)
    db.session.flush()
    audit.record("api_token.create", actor_id=user.id, object_type="api_token", object_id=token.id,
                 new_value=token.to_dict())
    db.session.commit()
    return token, secret


def list_for(user: LMUser) -> list[ApiToken]:
    return ApiToken.query.filter_by(user_id=user.id).order_by(ApiToken.id.desc()).all()


def revoke(user: LMUser, token_id: int) -> ApiToken:
    token = ApiToken.query.filter_by(id=token_id, user_id=user.id).first()
    if token is None:
        raise ServiceError("token not found", code="NOT_FOUND")
    if token.revoked_at is None:
        token.revoked_at = _now()
        audit.record("api_token.revoke", actor_id=user.id, object_type="api_token", object_id=token.id)
        db.session.commit()
    return token


def revoke_all(user: LMUser, *, actor_id: int) -> int:
    """Revoke every active token of ``user`` (e.g. after a password reset); the caller commits."""
    now = _now()
    tokens = _active(ApiToken.query.filter_by(user_id=user.id)).all()
    for token in tokens:
        token.revoked_at = now
        audit.record("api_token.revoke", actor_id=actor_id, object_type="api_token", object_id=token.id)
    return len(tokens)


def authenticate(secret: str) -> Optional[LMUser]:
    """The active owner of a valid, unexpired, unrevoked token, else None."""
    if not secret.startswith(PREFIX):
        return None
    token = _active(ApiToken.query.filter_by(token_hash=_digest(secret))).first()
    if token is None:
        return None
    user = db.session.get(LMUser, token.user_id)
    if user is None or not user.is_active:
        return None
    now = _now()
    last = _aware(token.last_used_at)
    if last is None or now - last >= _TOUCH_INTERVAL:
        token.last_used_at = now
        db.session.commit()
    return user
