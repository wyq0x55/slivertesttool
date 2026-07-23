"""Audit-log writer (FR-AUDIT-001/002). Never stores passwords or secrets."""

from __future__ import annotations

from typing import Any, Optional

from ...extensions import db
from ...models import AuditLog

# Field names that must never be persisted into audit values.
_REDACT_KEYS = {"password", "password_hash", "new_password", "token", "secret"}


def _redact(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            k: ("***" if k.lower() in _REDACT_KEYS else _redact(v))
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [_redact(v) for v in value]
    return value


def record(
    action: str,
    *,
    actor_id: Optional[int] = None,
    object_type: str = "",
    object_id: Optional[Any] = None,
    project_id: Optional[int] = None,
    old_value: Any = None,
    new_value: Any = None,
    client_ip: Optional[str] = None,
    request_id: Optional[str] = None,
    batch_id: Optional[str] = None,
    result: str = "success",
    error_summary: Optional[str] = None,
    commit: bool = False,
) -> AuditLog:
    """Append an audit entry. Caller controls transaction commit."""
    entry = AuditLog(
        action=action,
        actor_id=actor_id,
        object_type=object_type,
        object_id=None if object_id is None else str(object_id),
        project_id=project_id,
        old_value=_redact(old_value),
        new_value=_redact(new_value),
        client_ip=client_ip,
        request_id=request_id,
        batch_id=batch_id,
        result=result,
        error_summary=(error_summary or "")[:255] or None,
    )
    db.session.add(entry)
    if commit:
        db.session.commit()
    return entry
