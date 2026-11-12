"""Transactional outbox for database field writes mirrored into live Y.Docs.

Writers take the project lock before reading/updating rows and queue the actual
field changes in the same transaction. For a live room, delivery acknowledges
exact IDs only after the Doc (including its receipt) is durable, and pending
fields are protected from stale materialization meanwhile.

The outbox only has to reach rooms that are live. A room opened later reads the
database (see ``doc_model.reconcile_from_db``), which already holds every queued
value, so entries of projects without a live room are acknowledged unapplied.
This module deliberately has no pycrdt dependency: REST and workers use it too.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Mapping

from sqlalchemy import inspect

from ..extensions import db
from ..models import Project, RowWriteback, TestItemRow

_log = logging.getLogger(__name__)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def lock_project(project_id: int) -> None:
    """Serialize row writers until the caller commits or rolls back.

    Do not autoflush here: a caller may already have dirty row metadata, and
    writing that before acquiring the project lock reverses the lock order.
    Selecting only the ID also leaves caller-owned Project changes untouched.
    """
    with db.session.no_autoflush:
        db.session.query(Project.id).filter_by(id=project_id).with_for_update().one()


def refresh_row(row: TestItemRow) -> None:
    """Reload a locked row while preserving the caller's unflushed changes.

    Exemption decisions edit metadata and increment version before publishing a
    verdict. Preserve those edits, rebase that increment on the stored version,
    and merge locally changed JSON keys onto the refreshed custom-values bag.
    """
    state = inspect(row)
    changes = {}
    for attr in state.mapper.column_attrs:
        history = state.attrs[attr.key].history
        if history.has_changes():
            changes[attr.key] = (getattr(row, attr.key),
                                 history.deleted[0] if history.deleted else None)
    with db.session.no_autoflush:
        db.session.refresh(row)
    for key, (value, previous) in changes.items():
        if key == "version" and previous is not None:
            value = row.version + (value - previous)
        elif key == "custom_values":
            before, after = previous or {}, value or {}
            merged = dict(row.custom_values or {})
            for field in before.keys() - after.keys():
                merged.pop(field, None)
            merged.update({field: val for field, val in after.items()
                           if field not in before or before[field] != val})
            value = merged
        setattr(row, key, value)


def expand_aliases(values: Mapping[str, Any]) -> dict[str, Any]:
    """Keep both spellings of an aliased column consistent in the Doc."""
    out = dict(values)
    for key, value in values.items():
        column = TestItemRow._FIELD_ALIASES.get(key, key)
        if column in TestItemRow._FIELD_ALIASES.values():
            out[column] = value
            for alias, target in TestItemRow._FIELD_ALIASES.items():
                if target == column:
                    out[alias] = value
    return out


def enqueue_fields(project_id: int, row: TestItemRow,
                   values: Mapping[str, Any], *, sheet: str | None = None) -> None:
    """Queue fields on an existing UUID in the caller's locked transaction."""
    from ..services.lanmatrix import fields
    row_sheet = sheet or row.sheet or fields.DEFAULT_SHEET
    if row_sheet not in fields.SHEETS:
        raise ValueError(f"write-back is not allowed on sheet {row_sheet!r}")
    if row.project_id != project_id or not row.uuid:
        raise ValueError("write-back requires an existing row UUID in the project")
    if values:
        lock_project(project_id)
        db.session.add(RowWriteback(
            project_id=project_id, sheet=row_sheet, row_uuid=row.uuid,
            payload=expand_aliases(values)))


def apply_server_fields(project_id: int, row: TestItemRow,
                        values: Mapping[str, Any], *,
                        sheet: str = "test") -> bool:
    """Write and queue effective server fields; the caller owns the transaction."""
    from ..services.lanmatrix import fields
    if sheet not in fields.SHEETS:
        raise ValueError(f"write-back is not allowed on sheet {sheet!r}")
    if not values:
        return False
    lock_project(project_id)
    refresh_row(row)
    old = row.to_dict()
    for key, value in values.items():
        try:
            row.set_field(key, value)
        except Exception:
            _log.warning("write-back skipped unknown field %r (project=%s)",
                         key, project_id)
    new = row.to_dict()
    changed = {key: value for key, value in new.items()
               if old.get(key) != value}
    if not changed:
        return False
    row.version = (row.version or 0) + 1
    row.updated_at = _utcnow()
    enqueue_fields(project_id, row, changed, sheet=sheet)
    return True


def pending_batch(project_ids: Iterable[int]) -> list[dict[str, Any]]:
    """Read up to 2000 ordered, serializable entries without acknowledging them."""
    project_ids = [int(pid) for pid in project_ids]
    if not project_ids:
        return []
    rows = (RowWriteback.query
            .filter(RowWriteback.project_id.in_(project_ids),
                    RowWriteback.applied_at.is_(None))
            .order_by(RowWriteback.id.asc()).limit(2000).all())
    return [{"id": row.id, "project_id": row.project_id, "sheet": row.sheet,
             "row_uuid": row.row_uuid, "payload": dict(row.payload or {})}
            for row in rows]


def pending_ids(project_id: int) -> list[int]:
    """IDs pending for one project, for a caller that holds the project lock.

    Read in the same locked transaction as the rows themselves, these are
    exactly the entries whose values that row snapshot already contains.
    """
    return [entry_id for (entry_id,) in
            db.session.query(RowWriteback.id).filter_by(
                project_id=project_id, applied_at=None).all()]


def acknowledge_offline(live_project_ids: Iterable[int]) -> int:
    """Acknowledge entries of projects that have no live room.

    Their values are in the database, which the next room open reads. The
    caller must hold the collab server's room lifecycle lock, so a room cannot
    go live between choosing ``live_project_ids`` and this update.
    """
    query = RowWriteback.query.filter(RowWriteback.applied_at.is_(None))
    live = [int(pid) for pid in live_project_ids]
    if live:
        query = query.filter(RowWriteback.project_id.notin_(live))
    count = query.update({RowWriteback.applied_at: _utcnow()},
                         synchronize_session=False)
    db.session.commit()
    return int(count or 0)


def acknowledge(ids: Iterable[int]) -> None:
    """Mark only the IDs whose Doc update has already been persisted."""
    ids = list(ids)
    if not ids:
        return
    (RowWriteback.query.filter(RowWriteback.id.in_(ids),
                              RowWriteback.applied_at.is_(None))
     .update({RowWriteback.applied_at: _utcnow()}, synchronize_session=False))
    db.session.commit()


def purge_applied(older_than_seconds: int = 3600) -> int:
    """Trim acknowledged rows older than ``older_than_seconds``."""
    cutoff = _utcnow() - timedelta(seconds=max(0, older_than_seconds))
    deleted = (RowWriteback.query
               .filter(RowWriteback.applied_at.isnot(None),
                       RowWriteback.applied_at < cutoff)
               .delete(synchronize_session=False))
    db.session.commit()
    return int(deleted or 0)
