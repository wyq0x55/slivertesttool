"""Recycle bin (LAN Test Matrix): one place to see and undo destructive edits.

Deleting used to be final. Rows, fields and tasks are now soft-deleted, and this
module is the single view over everything a project has thrown away, plus the
retention sweep that eventually makes those deletions real.

Two rules the rest of the code depends on:

* **A restore must return something usable.** Values stay in ``custom_values``
  while a field is in the bin, and a task's log/report directories stay on disk
  while the task is in the bin. Handing back a row whose data is gone is worse
  than refusing the restore.
* **Retention is measured, not assumed.** ``expires_at`` is computed and shown
  per entry, so "30 days" is something the user can read off the screen rather
  than a number buried in a config file.
"""
from __future__ import annotations

import datetime as _dt
from typing import Optional

from ...extensions import db
from ...models import (FieldDefinition, LMUser, Project, Task, TestItemRow)
from ...services import task_service
from . import audit, fields_service, items_service
from .errors import ServiceError

# How long a deleted thing stays restorable. Long enough to survive a weekend
# and a holiday; short enough that the bin does not become a second database.
RETENTION_DAYS = 30

# Every kind the bin knows about. The route layer validates against this, so an
# unknown ``kind`` is a 400 rather than a silent empty list.
KINDS = ("item", "field", "task")

KIND_LABEL = {"item": "测试用例", "field": "字段", "task": "任务"}

# One page of the bin. The bin is a review surface, not a data export.
DEFAULT_LIMIT = 100
MAX_LIMIT = 500


def _utcnow() -> _dt.datetime:
    return _dt.datetime.now(_dt.timezone.utc)


def _aware(value):
    """Normalise a stored timestamp to UTC-aware.

    Rows written before the app standardised on aware datetimes come back naive;
    comparing those against ``_utcnow()`` raises TypeError, which would take the
    whole recycle bin down rather than mis-sort one entry.
    """
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=_dt.timezone.utc)
    return value


def naive_utc(value):
    """Drop the tzinfo, keeping the UTC wall clock.

    ``deleted_at`` is a ``db.DateTime`` with no ``timezone=True``, i.e. a
    Postgres ``TIMESTAMP WITHOUT TIME ZONE``. Comparing that column against an
    *aware* cutoff makes Postgres cast one side using the session's TimeZone
    setting, so the retention boundary would quietly slide by however many hours
    the server is offset from UTC. Comparisons that go into SQL use this.
    """
    value = _aware(value)
    return None if value is None else value.replace(tzinfo=None)


def _iso(value) -> Optional[str]:
    """Serialise as naive UTC, matching every other timestamp on the wire.

    The arithmetic here needs aware datetimes, but every other screen in the app
    ships these columns exactly as stored (naive UTC) and the shared JS ``stamp``
    helper only knows how to strip a trailing ``Z``. Emitting ``+00:00`` from
    this one endpoint would leave a stray offset dangling in the table.
    """
    return None if naive_utc(value) is None else naive_utc(value).isoformat()


def expires_at(deleted_at, *, retention_days: int = RETENTION_DAYS):
    deleted_at = _aware(deleted_at)
    if deleted_at is None:
        return None
    return deleted_at + _dt.timedelta(days=retention_days)


def days_left(deleted_at, *, retention_days: int = RETENTION_DAYS,
              now=None) -> int:
    """Whole days remaining before the entry is purged, never negative.

    Rounded **up**, so an entry with six hours left reads "1 天" rather than
    "0 天" -- a bin that says 0 while the item is still restorable invites the
    user to give up on it.
    """
    exp = expires_at(deleted_at, retention_days=retention_days)
    if exp is None:
        return 0
    now = _aware(now) or _utcnow()
    remaining = exp - now
    if remaining.total_seconds() <= 0:
        return 0
    return -(-int(remaining.total_seconds()) // 86400)


def clamp_limit(raw, default: int = DEFAULT_LIMIT) -> int:
    try:
        n = int(raw)
    except (TypeError, ValueError):
        return default
    if n < 1:
        return default
    return min(n, MAX_LIMIT)


# --------------------------------------------------------------------------- #
# Listing
# --------------------------------------------------------------------------- #
def _entry(kind: str, obj_id, title: str, subtitle: str, deleted_at,
           deleted_by, *, retention_days: int, now) -> dict:
    return {
        "kind": kind,
        "kind_label": KIND_LABEL.get(kind, kind),
        "id": obj_id,
        "title": title,
        "subtitle": subtitle,
        "deleted_at": _iso(deleted_at),
        "deleted_by": deleted_by,
        "expires_at": _iso(expires_at(deleted_at, retention_days=retention_days)),
        "days_left": days_left(deleted_at, retention_days=retention_days,
                               now=now),
    }


def _deleted_items(project_id: int, retention_days: int, now) -> list[dict]:
    rows = (TestItemRow.query
            .filter(TestItemRow.project_id == project_id,
                    TestItemRow.deleted_at.isnot(None))
            .order_by(TestItemRow.deleted_at.desc()).all())
    out = []
    for r in rows:
        out.append(_entry("item", r.id, r.case_id or ("#%s" % r.id),
                          r.title or "", r.deleted_at, r.deleted_by,
                          retention_days=retention_days, now=now))
    return out


def _deleted_fields(project_id: int, retention_days: int, now) -> list[dict]:
    rows = (FieldDefinition.query
            .filter(FieldDefinition.project_id == project_id,
                    FieldDefinition.deleted_at.isnot(None))
            .order_by(FieldDefinition.deleted_at.desc()).all())
    out = []
    for f in rows:
        out.append(_entry("field", f.id, f.display_name or f.field_key,
                          "字段标识 %s · %s 表" % (f.field_key, f.sheet or "test"),
                          f.deleted_at, f.deleted_by,
                          retention_days=retention_days, now=now))
    return out


def _deleted_tasks(project_id: int, retention_days: int, now) -> list[dict]:
    rows = (Task.query
            .filter(Task.project_id == project_id,
                    Task.deleted_at.isnot(None))
            .order_by(Task.deleted_at.desc()).all())
    out = []
    for t in rows:
        out.append(_entry("task", t.task_key, t.task_name or t.task_key,
                          "%s · %s" % (t.test_id or "-", t.status),
                          t.deleted_at, t.deleted_by,
                          retention_days=retention_days, now=now))
    return out


_LISTERS = {"item": _deleted_items, "field": _deleted_fields,
            "task": _deleted_tasks}


def _attach_actor_names(entries: list[dict]) -> None:
    """Resolve ``deleted_by`` to a display name, one query for the whole page.

    "字段 优先级 · 删除人 3" tells nobody whether this was their own mistake or
    someone else's decision -- which is the first thing you need to know before
    restoring it. Batched: a per-entry lookup would be N+1 on every page load.
    """
    ids = {e.get("deleted_by") for e in entries if e.get("deleted_by")}
    names = {}
    if ids:
        rows = LMUser.query.filter(LMUser.id.in_(ids)).all()
        names = {u.id: (u.display_name or u.username) for u in rows}
    for e in entries:
        # Always set, even to None: an absent key and a null one look the same
        # to the renderer but not to anything asserting on the payload shape,
        # and the caller should not have to know which kinds record a deleter.
        # Unresolved ids fall back to the raw id in the UI -- a departed
        # colleague must not blank the column out.
        e["deleted_by_name"] = names.get(e.get("deleted_by"))


def list_trash(project_id: int, *, kind: Optional[str] = None,
               limit: int = DEFAULT_LIMIT,
               retention_days: int = RETENTION_DAYS, now=None) -> dict:
    """Everything ``project_id`` has deleted, newest first.

    Returns ``{"entries": [...], "total": N, "truncated": bool,
    "retention_days": N}``. ``total`` is the unclipped count: a bin that shows
    100 of 400 without saying so reads as "the rest is already gone".
    """
    if kind is not None and kind not in KINDS:
        raise ServiceError("未知的回收站类型: %s" % kind, code="VALIDATION_ERROR")
    now = _aware(now) or _utcnow()
    kinds = (kind,) if kind else KINDS
    entries: list[dict] = []
    for k in kinds:
        entries.extend(_LISTERS[k](project_id, retention_days, now))
    entries.sort(key=lambda e: (e["deleted_at"] or ""), reverse=True)
    total = len(entries)
    limit = clamp_limit(limit)
    page = entries[:limit]
    # Only the page that is actually served, not everything we sorted.
    _attach_actor_names(page)
    return {
        "entries": page,
        "total": total,
        "truncated": total > limit,
        "retention_days": retention_days,
    }


def count_trash(project_id: int) -> int:
    """Number of restorable entries, for the nav badge."""
    return (TestItemRow.query.filter(
                TestItemRow.project_id == project_id,
                TestItemRow.deleted_at.isnot(None)).count()
            + FieldDefinition.query.filter(
                FieldDefinition.project_id == project_id,
                FieldDefinition.deleted_at.isnot(None)).count()
            + Task.query.filter(
                Task.project_id == project_id,
                Task.deleted_at.isnot(None)).count())


# --------------------------------------------------------------------------- #
# Restore / purge
# --------------------------------------------------------------------------- #
def _field_in_bin(project_id: int, field_id) -> FieldDefinition:
    f = FieldDefinition.query.filter_by(
        id=field_id, project_id=project_id).first()
    if f is None or f.deleted_at is None:
        raise ServiceError("回收站中无此字段", code="NOT_FOUND")
    return f


def _task_in_bin(project_id: int, task_key) -> Task:
    t = Task.query.filter_by(
        task_key=str(task_key), project_id=project_id).first()
    if t is None or t.deleted_at is None:
        raise ServiceError("回收站中无此任务", code="NOT_FOUND")
    return t


def restore(user: LMUser, project: Project, kind: str, obj_id) -> dict:
    """Bring one entry back. Raises ServiceError if it is not in the bin."""
    if kind not in KINDS:
        raise ServiceError("未知的回收站类型: %s" % kind, code="VALIDATION_ERROR")
    if kind == "item":
        item = items_service.restore_item(user, project, int(obj_id))
        return {"kind": kind, "id": item.id, "title": item.case_id}
    if kind == "field":
        f = _field_in_bin(project.id, obj_id)
        fields_service.restore_field(user, project, f)
        return {"kind": kind, "id": f.id, "title": f.display_name or f.field_key}
    t = _task_in_bin(project.id, obj_id)
    task_service.restore_task(t)
    audit.record("task.restore", actor_id=user.id, object_type="task",
                 object_id=t.task_key, project_id=project.id)
    return {"kind": kind, "id": t.task_key, "title": t.task_name or t.task_key}


def purge(user: LMUser, project: Project, kind: str, obj_id) -> dict:
    """Delete one entry for real, ahead of its retention date."""
    if kind not in KINDS:
        raise ServiceError("未知的回收站类型: %s" % kind, code="VALIDATION_ERROR")
    if kind == "item":
        row = TestItemRow.query.filter_by(
            id=int(obj_id), project_id=project.id).first()
        if row is None or row.deleted_at is None:
            raise ServiceError("回收站中无此记录", code="NOT_FOUND")
        case_id = row.case_id
        db.session.delete(row)
        audit.record("item.purge", actor_id=user.id, object_type="item",
                     object_id=obj_id, project_id=project.id,
                     old_value={"case_id": case_id})
        db.session.commit()
        return {"kind": kind, "id": obj_id, "title": case_id}
    if kind == "field":
        f = _field_in_bin(project.id, obj_id)
        title = f.display_name or f.field_key
        fields_service.purge_field(user, project.id, f)
        return {"kind": kind, "id": obj_id, "title": title}
    t = _task_in_bin(project.id, obj_id)
    title = t.task_name or t.task_key
    audit.record("task.purge", actor_id=user.id, object_type="task",
                 object_id=t.task_key, project_id=project.id)
    task_service.purge_task(t)
    return {"kind": kind, "id": obj_id, "title": title}


# --------------------------------------------------------------------------- #
# Retention sweep
# --------------------------------------------------------------------------- #
def purge_expired(*, retention_days: int = RETENTION_DAYS, now=None,
                  project_id: Optional[int] = None) -> dict:
    """Permanently remove everything past its retention date.

    Safe to call repeatedly (it is driven by ``deleted_at``, not by a cursor),
    which is what lets it run on every startup without bookkeeping.

    Returns a per-kind count. The counts are recorded in the audit log even when
    zero, so "the sweep ran and found nothing" is distinguishable from "the
    sweep never ran" -- the difference matters when data is missing.
    """
    now = _aware(now) or _utcnow()
    cutoff = naive_utc(now - _dt.timedelta(days=retention_days))
    counts = {"items": 0, "fields": 0, "tasks": 0}

    def scoped(query, model):
        q = query.filter(model.deleted_at.isnot(None),
                         model.deleted_at < cutoff)
        if project_id is not None:
            q = q.filter(model.project_id == project_id)
        return q

    for row in scoped(TestItemRow.query, TestItemRow).all():
        db.session.delete(row)
        counts["items"] += 1

    for f in scoped(FieldDefinition.query, FieldDefinition).all():
        # Purging a field is what finally drops its values from every row, so it
        # has to go through the field service rather than a bare delete.
        fields_service.purge_field(None, f.project_id, f, commit=False)
        counts["fields"] += 1

    for t in scoped(Task.query, Task).all():
        task_service.purge_task(t, commit=False)
        counts["tasks"] += 1

    total = sum(counts.values())
    if total:
        audit.record("trash.purge_expired", actor_id=None, object_type="trash",
                     object_id=project_id, project_id=project_id,
                     new_value=dict(counts, retention_days=retention_days))
    db.session.commit()
    return counts
