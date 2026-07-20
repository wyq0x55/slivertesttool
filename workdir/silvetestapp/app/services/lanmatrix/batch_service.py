"""Batch search/replace service (LAN Test Matrix): preview/apply/undo."""
from __future__ import annotations

import datetime as _dt
import uuid
from typing import Any, Optional

from ...extensions import db
from ...models import AuditLog, LMUser, Project, TestItemRow
from . import audit, batch as batch_ops, settings, validation
from .errors import ServiceError
from .fields_service import field_specs
from .items_service import list_items


def _utcnow() -> _dt.datetime:
    return _dt.datetime.now(_dt.timezone.utc)


# --------------------------------------------------------------------------- #
# Batch (FR-BATCH-001..003)
# --------------------------------------------------------------------------- #
def _resolve_scope(project_id: int, scope: dict[str, Any]) -> list[TestItemRow]:
    kind = scope.get("type", "ids")
    if kind == "ids":
        ids = scope.get("ids", [])
        if not ids:
            return []
        return TestItemRow.query.filter(
            TestItemRow.project_id == project_id,
            TestItemRow.id.in_(ids),
            TestItemRow.deleted_at.is_(None)).all()
    if kind == "filter":
        result = list_items(project_id, page=1, page_size=settings.PAGE_SIZE_MAX,
                            filters=scope.get("filters", []),
                            combinator=scope.get("combinator", "and"),
                            quick=scope.get("quick"))
        ids = [r["id"] for r in result["items"]]
        return TestItemRow.query.filter(
            TestItemRow.project_id == project_id,
            TestItemRow.id.in_(ids),
            TestItemRow.deleted_at.is_(None)).all()
    if kind == "all":
        return TestItemRow.query.filter(
            TestItemRow.project_id == project_id,
            TestItemRow.deleted_at.is_(None)).all()
    raise ServiceError(f"未知的作用范围: {kind}", code="VALIDATION_ERROR")


def batch_preview(project: Project, field_key: str, operation: dict,
                  scope: dict, *, sample_limit: Optional[int] = None) -> dict:
    if sample_limit is None:
        sample_limit = settings.BATCH_SAMPLE_LIMIT
    batch_ops.validate_operation(operation)
    specs = {s.field_key: s for s in field_specs(project.id)}
    spec = specs.get(field_key)
    if spec is None:
        raise ServiceError("字段不存在", code="VALIDATION_ERROR")
    if spec.is_readonly:
        raise ServiceError("只读字段不可批量修改", code="VALIDATION_ERROR")

    rows = _resolve_scope(project.id, scope)
    samples, invalid = [], 0
    for item in rows:
        old = item.get_field(field_key)
        try:
            new = batch_ops.apply_operation(operation, old)
        except batch_ops.BatchOperationError as exc:
            invalid += 1
            if len(samples) < sample_limit:
                samples.append({"id": item.id, "old": old, "error": str(exc)})
            continue
        _val, errs = validation.validate_value(spec, new)
        entry = {"id": item.id, "case_id": item.case_id, "old": old, "new": new}
        if validation.has_blocking(errs):
            invalid += 1
            entry["error"] = "; ".join(e.message for e in errs)
        if len(samples) < sample_limit:
            samples.append(entry)
    return {
        "scope": scope.get("type"),
        "matched": len(rows),
        "invalid": invalid,
        "reversible": True,
        "samples": samples,
    }


def batch_update(user: LMUser, project: Project, field_key: str,
                 operation: dict, scope: dict) -> dict:
    batch_ops.validate_operation(operation)
    specs = {s.field_key: s for s in field_specs(project.id)}
    spec = specs.get(field_key)
    if spec is None or spec.is_readonly:
        raise ServiceError("字段不可批量修改", code="VALIDATION_ERROR")

    rows = _resolve_scope(project.id, scope)
    batch_id = "batch-" + uuid.uuid4().hex[:12]
    changed = 0
    # Whole-batch transaction: validate all, then apply, else roll back.
    prepared = []
    for item in rows:
        old = item.get_field(field_key)
        new = batch_ops.apply_operation(operation, old)
        value, errs = validation.validate_value(spec, new)
        if validation.has_blocking(errs):
            db.session.rollback()
            raise ServiceError(
                f"记录 {item.case_id} 校验失败: {'; '.join(e.message for e in errs)}",
                code="VALIDATION_ERROR")
        prepared.append((item, old, value))

    for item, old, value in prepared:
        item.set_field(field_key, value)
        item.version += 1
        item.updated_by = user.id
        item.updated_at = _utcnow()
        audit.record("item.batch_update", actor_id=user.id, object_type="item",
                     object_id=item.id, project_id=project.id, batch_id=batch_id,
                     old_value={field_key: old}, new_value={field_key: value})
        changed += 1
    audit.record("batch.update", actor_id=user.id, object_type="batch",
                 object_id=batch_id, project_id=project.id, batch_id=batch_id,
                 new_value={"field": field_key, "operation": operation,
                            "changed": changed})
    db.session.commit()
    return {"batch_id": batch_id, "changed": changed}


def batch_undo(user: LMUser, project: Project, batch_id: str) -> dict:
    entries = AuditLog.query.filter_by(
        project_id=project.id, batch_id=batch_id, action="item.batch_update").all()
    if not entries:
        raise ServiceError("找不到该批次", code="NOT_FOUND")
    restored = 0
    for entry in entries:
        item = db.session.get(TestItemRow, int(entry.object_id))
        if item is None or item.deleted_at is not None:
            continue
        old = entry.old_value or {}
        for key, value in old.items():
            item.set_field(key, value)
        item.version += 1
        item.updated_by = user.id
        item.updated_at = _utcnow()
        restored += 1
    new_batch = "undo-" + uuid.uuid4().hex[:12]
    audit.record("batch.undo", actor_id=user.id, object_type="batch",
                 object_id=batch_id, project_id=project.id, batch_id=new_batch,
                 new_value={"undone_batch": batch_id, "restored": restored})
    db.session.commit()
    return {"restored": restored, "undo_batch_id": new_batch}
