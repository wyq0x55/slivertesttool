"""Test-item (row) service (LAN Test Matrix).

Row query (pagination/sort/filter), single-row CRUD with validation +
optimistic locking + audit, and multi-row (Excel-style) operations.
"""
from __future__ import annotations

import datetime as _dt
import logging
from typing import Any, Optional

_log = logging.getLogger(__name__)

from ...extensions import db
from ...models import LMUser, Project, TestItemRow
from . import audit, queries, settings, validation
from .errors import ServiceError, VersionConflict
from .fields_service import field_specs, list_fields


def _utcnow() -> _dt.datetime:
    return _dt.datetime.now(_dt.timezone.utc)


# --------------------------------------------------------------------------- #
# Items — query
# --------------------------------------------------------------------------- #
def list_items(project_id: int, *, page: int = 1, page_size: Optional[int] = None,
               sort: Optional[str] = None, filters: Optional[list] = None,
               combinator: str = "and", quick: Optional[str] = None,
               sheet: Optional[str] = None) -> dict:
    if page_size is None:
        page_size = settings.PAGE_SIZE
    q = TestItemRow.query.filter(
        TestItemRow.project_id == project_id,
        TestItemRow.deleted_at.is_(None),
    )
    if sheet:
        q = q.filter(TestItemRow.sheet == sheet)
    clause = queries.build_filter_clause(filters or [], combinator)
    if clause is not None:
        q = q.filter(clause)
    if quick:
        like = f"%{quick}%"
        # ``title``/``comment`` back the unified-protocol test_name/remark fields,
        # so quick-search covers the Test-Matrix vocabulary after the data-layer
        # unification (see ``TestItemRow._FIELD_ALIASES``).
        q = q.filter(db.or_(
            TestItemRow.case_id.ilike(like),
            TestItemRow.title.ilike(like),
            TestItemRow.comment.ilike(like),
            TestItemRow.test_steps.ilike(like),
            TestItemRow.expected_result.ilike(like),
        ))
    total = q.count()
    q = queries.apply_sort(q, sort)
    page = max(1, page)
    page_size = min(max(1, page_size), settings.PAGE_SIZE_MAX)
    items = q.offset((page - 1) * page_size).limit(page_size).all()
    return {
        "items": [it.to_dict() for it in items],
        "page": page, "page_size": page_size, "total": total,
    }


def get_item(project_id: int, item_id: int) -> TestItemRow:
    item = TestItemRow.query.filter_by(
        id=item_id, project_id=project_id, deleted_at=None).first()
    if item is None:
        raise ServiceError("测试项不存在", code="NOT_FOUND")
    return item


# --------------------------------------------------------------------------- #
# Items — mutate (with validation + optimistic lock + audit)
# --------------------------------------------------------------------------- #
def _unique_checker(project_id: int, field_key: str, exclude_id: Optional[int]):
    def check(fkey: str, value: Any) -> bool:
        if fkey != "case_id":
            return True  # only case_id is uniqueness-enforced at DB level in V1
        q = TestItemRow.query.filter_by(
            project_id=project_id, case_id=value, deleted_at=None)
        if exclude_id is not None:
            q = q.filter(TestItemRow.id != exclude_id)
        return q.first() is None
    return check


def _apply_field_defaults(project_id: int, values: dict[str, Any],
                          specs: list) -> dict[str, Any]:
    """Fill in field ``default_value`` for keys the caller left empty.

    Also auto-generates a unique ``case_id`` when none was supplied, so a user
    can add a blank draft row and fill the cells in afterwards.
    """
    defs = {f.field_key: f for f in list_fields(project_id, active_only=True)}
    for key, fdef in defs.items():
        current = values.get(key)
        empty = current is None or (isinstance(current, str) and current.strip() == "")
        if empty and fdef.default_value not in (None, ""):
            values[key] = fdef.default_value
    case_id = values.get("case_id")
    if not case_id or (isinstance(case_id, str) and not case_id.strip()):
        values["case_id"] = _auto_case_id(project_id)
    return values


def _auto_case_id(project_id: int) -> str:
    base = _dt.datetime.now().strftime("TC_%Y%m%d_%H%M%S")
    candidate, n = base, 1
    while TestItemRow.query.filter_by(
            project_id=project_id, case_id=candidate, deleted_at=None).first():
        n += 1
        candidate = f"{base}_{n}"
    return candidate


def _max_row_order(project_id: int) -> int:
    return db.session.query(db.func.max(TestItemRow.row_order)) \
        .filter_by(project_id=project_id).scalar() or 0


def _shift_row_orders(project_id: int, from_order: int, delta: int) -> None:
    """Bump ``row_order`` by ``delta`` for every live row at/after ``from_order``.

    Used to open a gap when inserting rows at a specific position (Excel-style
    "insert above / below") so the new row can take ``from_order``.
    """
    db.session.query(TestItemRow).filter(
        TestItemRow.project_id == project_id,
        TestItemRow.deleted_at.is_(None),
        TestItemRow.row_order >= from_order,
    ).update({TestItemRow.row_order: TestItemRow.row_order + delta},
             synchronize_session=False)


def create_item(user: LMUser, project: Project, values: dict[str, Any],
                *, draft: bool = False, anchor_id: Optional[int] = None,
                place: str = "below", sheet: Optional[str] = None,
                commit: bool = True) -> TestItemRow:
    from . import fields as fld
    specs = field_specs(project.id)
    values = dict(values or {})
    # ``sheet`` is a row attribute (which editor tab the row lives on), not a
    # field value — accept it either as a keyword or inside ``values``.
    row_sheet = (sheet or values.pop("sheet", None) or fld.DEFAULT_SHEET)
    if row_sheet not in fld.SHEETS:
        row_sheet = fld.DEFAULT_SHEET
    # Only validate/apply fields that belong to this row's sheet, so required
    # fields from other tabs don't block a const/lib row and vice-versa.
    specs = [s for s in specs if getattr(s, "sheet", "test") == row_sheet]
    values = _apply_field_defaults(project.id, values, specs)
    coerced, errors = validation.validate_record(
        specs, values,
        unique_checker=lambda k, v: _unique_checker(project.id, k, None)(k, v),
        enforce_required=not draft)
    if validation.has_blocking(errors):
        raise ServiceError("输入数据校验失败", code="VALIDATION_ERROR",
                           details=[e.to_dict() for e in errors])
    # Positional insert: place the new row above/below an anchor row and shift
    # the following rows down to make room. Without an anchor we append at end.
    target_order = _max_row_order(project.id) + 1
    if anchor_id is not None:
        anchor = TestItemRow.query.filter_by(
            id=anchor_id, project_id=project.id, deleted_at=None).first()
        if anchor is not None:
            target_order = anchor.row_order + (1 if place == "below" else 0)
            _shift_row_orders(project.id, target_order, 1)
    item = TestItemRow(project_id=project.id, row_order=target_order,
                       sheet=row_sheet,
                       created_by=user.id, updated_by=user.id, version=1)
    _apply_values(item, coerced, specs)
    # ``case_id`` is an internal identity column and is no longer surfaced as a
    # seeded field, so it won't be in ``coerced`` / ``specs``; apply the value
    # (auto-generated in ``_apply_field_defaults``) directly to the column.
    if values.get("case_id"):
        item.case_id = values["case_id"]
    db.session.add(item)
    db.session.flush()
    audit.record("item.create", actor_id=user.id, object_type="item",
                 object_id=item.id, project_id=project.id, new_value=item.to_dict())
    if commit:
        db.session.commit()
    return item


def update_item(user: LMUser, project: Project, item: TestItemRow,
                version: int, changes: dict[str, Any],
                *, commit: bool = True) -> TestItemRow:
    if version != item.version:
        raise VersionConflict(version, item.version, item.to_dict())
    specs = field_specs(project.id)
    spec_by_key = {s.field_key: s for s in specs}
    old = item.to_dict()
    errors: list = []
    coerced: dict[str, Any] = {}
    for key, raw in changes.items():
        spec = spec_by_key.get(key)
        if spec is None or spec.is_readonly:
            continue
        value, errs = validation.validate_value(
            spec, raw, unique_checker=_unique_checker(project.id, key, item.id))
        coerced[key] = value
        errors.extend(errs)
    if validation.has_blocking(errors):
        raise ServiceError("输入数据校验失败", code="VALIDATION_ERROR",
                           details=[e.to_dict() for e in errors])
    _apply_values(item, coerced, specs)
    item.version += 1
    item.updated_by = user.id
    item.updated_at = _utcnow()
    audit.record("item.update", actor_id=user.id, object_type="item",
                 object_id=item.id, project_id=project.id,
                 old_value=old, new_value=item.to_dict())
    if commit:
        db.session.commit()
    return item


def _apply_values(item: TestItemRow, coerced: dict[str, Any], specs: list) -> None:
    for spec in specs:
        if spec.is_readonly:
            continue
        if spec.field_key in coerced:
            item.set_field(spec.field_key, coerced[spec.field_key])


def duplicate_item(user: LMUser, project: Project, item: TestItemRow) -> TestItemRow:
    data = item.to_dict()
    data.pop("id", None)
    data["case_id"] = _next_case_id(project.id, item.case_id)
    return create_item(user, project, data)


def _next_case_id(project_id: int, base: str) -> str:
    candidate = f"{base}_copy"
    n = 1
    while TestItemRow.query.filter_by(
            project_id=project_id, case_id=candidate, deleted_at=None).first():
        n += 1
        candidate = f"{base}_copy{n}"
    return candidate


def soft_delete_item(user: LMUser, project: Project, item: TestItemRow,
                     *, commit: bool = True) -> None:
    old = item.to_dict()
    item.deleted_at = _utcnow()
    audit.record("item.delete", actor_id=user.id, object_type="item",
                 object_id=item.id, project_id=project.id, old_value=old)
    if commit:
        db.session.commit()


def restore_item(user: LMUser, project: Project, item_id: int,
                 *, commit: bool = True) -> TestItemRow:
    item = TestItemRow.query.filter_by(id=item_id, project_id=project.id).first()
    if item is None or item.deleted_at is None:
        raise ServiceError("回收站中无此记录", code="NOT_FOUND")
    item.deleted_at = None
    audit.record("item.restore", actor_id=user.id, object_type="item",
                 object_id=item.id, project_id=project.id)
    if commit:
        db.session.commit()
    return item


# --------------------------------------------------------------------------- #
# Multi-row operations (Excel-style: copy / delete / move several rows at once)
# --------------------------------------------------------------------------- #
def _live_rows_by_ids(project_id: int, ids: list[int]) -> list[TestItemRow]:
    if not ids:
        return []
    rows = TestItemRow.query.filter(
        TestItemRow.project_id == project_id,
        TestItemRow.id.in_(ids),
        TestItemRow.deleted_at.is_(None),
    ).order_by(TestItemRow.row_order.asc()).all()
    return rows


def bulk_soft_delete(user: LMUser, project: Project, ids: list[int],
                     *, commit: bool = True) -> int:
    """Soft-delete several rows at once; returns the number actually removed."""
    rows = _live_rows_by_ids(project.id, ids)
    now = _utcnow()
    for item in rows:
        old = item.to_dict()
        item.deleted_at = now
        audit.record("item.delete", actor_id=user.id, object_type="item",
                     object_id=item.id, project_id=project.id, old_value=old)
    if commit:
        db.session.commit()
    return len(rows)


def bulk_duplicate(user: LMUser, project: Project,
                   ids: list[int], *, commit: bool = True) -> list[TestItemRow]:
    """Duplicate several rows, inserting the copies right below the selection
    block (in the original order). Each copy reuses the validated single-row
    path, so field defaults, ``case_id`` uniqueness and audit all apply."""
    if not project.is_editable:
        raise ServiceError("项目当前不可编辑", code="PROJECT_LOCKED")
    rows = _live_rows_by_ids(project.id, ids)
    created: list[TestItemRow] = []
    anchor_id = rows[-1].id if rows else None
    for src in rows:
        data = src.to_dict()
        data.pop("id", None)
        data["case_id"] = _next_case_id(project.id, src.case_id)
        dup = create_item(user, project, data,
                          anchor_id=anchor_id, place="below", commit=False)
        anchor_id = dup.id  # keep copies contiguous and in order
        created.append(dup)
    if commit:
        db.session.commit()
    return created


def move_items(user: LMUser, project: Project, ids: list[int],
               direction: str, *, commit: bool = True) -> int:
    """Move the selected rows one position up or down (block move), then
    normalize ``row_order`` to a gap-free 1..N sequence. Returns rows moved."""
    if direction not in ("up", "down"):
        raise ServiceError("方向无效", code="VALIDATION_ERROR")
    ordered = TestItemRow.query.filter(
        TestItemRow.project_id == project.id,
        TestItemRow.deleted_at.is_(None),
    ).order_by(TestItemRow.row_order.asc()).all()
    sel = set(int(i) for i in (ids or []))
    if not sel:
        return 0
    idxs = [i for i, r in enumerate(ordered) if r.id in sel]
    if direction == "up":
        for i in idxs:  # top-down so the block slides up as a unit
            if i - 1 >= 0 and ordered[i - 1].id not in sel:
                ordered[i - 1], ordered[i] = ordered[i], ordered[i - 1]
    else:
        for i in reversed(idxs):  # bottom-up for a downward slide
            if i + 1 < len(ordered) and ordered[i + 1].id not in sel:
                ordered[i + 1], ordered[i] = ordered[i], ordered[i + 1]
    moved = 0
    for pos, r in enumerate(ordered, start=1):
        if r.row_order != pos:
            r.row_order = pos
            moved += 1
    if moved:
        audit.record("item.reorder", actor_id=user.id, object_type="project",
                     object_id=project.id, project_id=project.id,
                     new_value={"moved": sorted(sel), "direction": direction})
        if commit:
            db.session.commit()
    return len(sel)


# --------------------------------------------------------------------------- #
# Materialization (CRDT / Y.Doc -> DB reconcile)
#
# In collaboration mode the per-project Y.Doc is the source of truth for row
# content and ordering; the database is a materialized projection written back
# by a single writer (the collab server). These helpers upsert rows keyed by
# ``TestItemRow.uuid`` WITHOUT optimistic-lock conflicts (the Y.Doc already
# resolved the merge) and let the caller batch a whole snapshot into one
# transaction. They must only be invoked from the single materialization path
# — see design doc §1.6 "单一写者边界".
# --------------------------------------------------------------------------- #

# Row-state keys that materialization manages itself and must never copy blindly
# from the Y.Map onto the row (identity / ordering / audit bookkeeping).
_MATERIALIZE_SKIP = frozenset({
    "id", "uuid", "row_order", "sheet", "version",
    "created_at", "created_by", "updated_at", "updated_by",
})


def find_row_by_uuid(project_id: int, row_uuid: str,
                     *, include_deleted: bool = False) -> Optional[TestItemRow]:
    """Look up a row by its stable ``uuid`` (the CRDT row identity)."""
    q = TestItemRow.query.filter_by(project_id=project_id, uuid=row_uuid)
    if not include_deleted:
        q = q.filter(TestItemRow.deleted_at.is_(None))
    return q.first()


def _apply_materialized_values(item: TestItemRow, state: dict[str, Any]) -> None:
    """Copy field values from a Y.Map snapshot onto the row.

    Bypasses spec validation on purpose: the Y.Doc is authoritative, so we
    persist exactly what collaborators agreed on. Identity/ordering keys in
    ``_MATERIALIZE_SKIP`` are handled by the caller.
    """
    for key, value in state.items():
        if key in _MATERIALIZE_SKIP:
            continue
        item.set_field(key, value)


def materialize_create(user: LMUser, project: Project, state: dict[str, Any],
                       *, sheet: Optional[str] = None,
                       row_order: Optional[int] = None,
                       commit: bool = False) -> TestItemRow:
    """Insert a new row for a Y.Map that has no DB row yet.

    Preserves the Y.Map ``uuid`` so subsequent reconciles find the same row.
    Keeps ``case_id`` auto-generation so identity/NOT NULL invariants hold even
    though field validation is skipped.
    """
    from . import fields as fld
    row_sheet = sheet or state.get("sheet") or fld.DEFAULT_SHEET
    if row_sheet not in fld.SHEETS:
        row_sheet = fld.DEFAULT_SHEET
    order = row_order if row_order is not None else _max_row_order(project.id) + 1
    kwargs: dict[str, Any] = dict(
        project_id=project.id, sheet=row_sheet, row_order=order,
        created_by=user.id, updated_by=user.id, version=1)
    row_uuid = (state.get("uuid") or "").strip()
    if row_uuid:
        kwargs["uuid"] = row_uuid
    item = TestItemRow(**kwargs)
    _apply_materialized_values(item, state)
    if not (item.case_id or "").strip():
        item.case_id = _auto_case_id(project.id)
    db.session.add(item)
    db.session.flush()
    audit.record("item.materialize", actor_id=user.id, object_type="item",
                 object_id=item.id, project_id=project.id, new_value=item.to_dict())
    if commit:
        db.session.commit()
    return item


def materialize_update(user: LMUser, project: Project, item: TestItemRow,
                       changes: dict[str, Any], *,
                       commit: bool = False) -> TestItemRow:
    """Apply Y.Map field values to an existing row without a version check.

    Unlike :func:`update_item` this never raises :class:`VersionConflict` — the
    CRDT already merged concurrent edits — and it resurrects a row that had been
    soft-deleted but has reappeared in the Y.Array. ``version`` is still bumped
    so downstream consumers (SSE/export) can detect the change.
    """
    old = item.to_dict()
    if item.deleted_at is not None:
        item.deleted_at = None
    _apply_materialized_values(item, changes)
    item.version += 1
    item.updated_by = user.id
    item.updated_at = _utcnow()
    audit.record("item.materialize", actor_id=user.id, object_type="item",
                 object_id=item.id, project_id=project.id,
                 old_value=old, new_value=item.to_dict())
    if commit:
        db.session.commit()
    return item


def materialize_sheet(user: LMUser, project: Project, sheet: str,
                      rows: list[dict[str, Any]], *,
                      commit: bool = True,
                      actor_by_uuid: Optional[dict[str, LMUser]] = None) -> dict[str, int]:
    """Reconcile one sheet against a Y.Array snapshot (list of row-state dicts).

    Ordering rules (single writer, whole reconcile is one transaction):

    * uuid present in DB   -> update fields, ``row_order`` = 1-based index
    * uuid absent in DB    -> create at that index
    * a soft-deleted uuid that reappears -> resurrected via ``materialize_update``
    * a live DB row whose uuid is absent from the snapshot -> soft delete

    ``rows`` is exactly what ``Y.Array.to_py()`` yields for the sheet, in visual
    order. Every element must carry its ``uuid`` (malformed rows are skipped).
    Returns a ``{created, updated, removed, failed, total, errors}`` summary
    where ``errors`` maps ``uuid -> {"cells": [field_key, ...], "message": str}``
    for rows that could not be persisted.

    Per-row isolation (design §12.2): a single bad row must never block the rest
    of the sheet. Each row is validated (non-blocking / draft mode, so a
    half-typed live edit is not flagged) and then written inside its own
    SAVEPOINT. A blocking validation error keeps the last good DB value for that
    row; a database-level failure rolls back only that row's savepoint. Either
    way the offending ``uuid`` is reported back through ``errors`` and the loop
    continues, so healthy rows still materialize.

    ``actor_by_uuid`` optionally attributes individual rows to the collaborator
    who was editing them (derived from Awareness, design §7.1 step 4): a row's
    create/update is credited to ``actor_by_uuid[uuid]`` when present, otherwise
    to the batch ``user``. Soft-deletions always use the batch ``user`` (nobody's
    cursor sits on a row that has just disappeared).
    """
    from . import fields as fld
    row_sheet = sheet if sheet in fld.SHEETS else fld.DEFAULT_SHEET
    by_uuid = actor_by_uuid or {}
    # Validate only fields that belong to this sheet, so a required cell on
    # another tab never flags a const/lib row and vice-versa.
    specs = [s for s in field_specs(project.id)
             if getattr(s, "sheet", fld.DEFAULT_SHEET) == row_sheet]
    existing: dict[str, TestItemRow] = {
        r.uuid: r for r in TestItemRow.query.filter_by(
            project_id=project.id, sheet=row_sheet).all()}
    seen: set[str] = set()
    created = updated = failed = 0
    errors: dict[str, dict[str, Any]] = {}
    for index, state in enumerate(rows, start=1):
        row_uuid = (state.get("uuid") or "").strip()
        if not row_uuid:
            continue
        seen.add(row_uuid)
        # Non-blocking validation: live collaboration means rows are constantly
        # mid-edit, so required-but-blank is tolerated (enforce_required=False);
        # only hard errors (bad number/date/enum) mark the cell.
        _, verrs = validation.validate_record(
            specs, state, enforce_required=False)
        blocking = [e for e in verrs if e.severity == "blocking"]
        if blocking:
            errors[row_uuid] = {
                "cells": [e.field for e in blocking],
                "message": "；".join(e.message for e in blocking)}
            failed += 1
            continue
        row_actor = by_uuid.get(row_uuid) or user
        item = existing.get(row_uuid)
        try:
            with db.session.begin_nested():
                if item is None:
                    materialize_create(row_actor, project, state,
                                       sheet=row_sheet, row_order=index,
                                       commit=False)
                    is_create = True
                else:
                    item.row_order = index
                    materialize_update(row_actor, project, item, state,
                                       commit=False)
                    is_create = False
        except Exception as exc:  # noqa: BLE001 - one row must not poison others
            errors[row_uuid] = {
                "cells": [],
                "message": f"物化失败：{exc.__class__.__name__}"}
            failed += 1
            _log.warning("materialize row %s (sheet=%s) failed: %s",
                         row_uuid, row_sheet, exc)
            continue
        if is_create:
            created += 1
        else:
            updated += 1
    now = _utcnow()
    removed = 0
    for row_uuid, item in existing.items():
        if row_uuid not in seen and item.deleted_at is None:
            old = item.to_dict()
            item.deleted_at = now
            audit.record("item.materialize_delete", actor_id=user.id,
                         object_type="item", object_id=item.id,
                         project_id=project.id, old_value=old)
            removed += 1
    if commit:
        db.session.commit()
    return {"created": created, "updated": updated, "removed": removed,
            "failed": failed, "total": len(rows), "errors": errors}


def sheet_uuid_index(project_id: int, sheet: str) -> dict[str, tuple[int, int]]:
    """Map ``uuid -> (id, version)`` for the live rows of a sheet.

    Used by the collab materializer to write the authoritative primary key and
    version back into the Y.Doc after a reconcile, so client-created rows (which
    start with a temporary negative ``id``) pick up their real DB id.
    """
    from . import fields as fld
    row_sheet = sheet if sheet in fld.SHEETS else fld.DEFAULT_SHEET
    rows = TestItemRow.query.filter_by(
        project_id=project_id, sheet=row_sheet, deleted_at=None).all()
    return {r.uuid: (r.id, int(r.version or 0)) for r in rows}
