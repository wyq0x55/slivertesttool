"""Excel import/export orchestration on top of :mod:`excel_io` (FR-EXCEL-*).

Import is a two-step flow: create a job with a validated preview (no writes),
then commit to persist inside a transaction (whole-file rollback on error).
"""

from __future__ import annotations

import datetime as _dt
from typing import Any, BinaryIO, Optional, Union

from ...extensions import db
from . import (
    audit, excel_io, import_job_service, permissions, service, settings,
    validation,
)
from ...models import DataJob, Project, TestItemRow
from .validation import FieldSpec


def _utcnow() -> _dt.datetime:
    return _dt.datetime.now(_dt.timezone.utc)


IMPORT_MODES = ("insert_only", "upsert", "update_only", "replace_all")


def build_template_bytes(project: Project):
    specs = [f.to_dict() for f in service.list_fields(project.id, active_only=True)]
    wb = excel_io.build_template(project.to_dict(), specs)
    return excel_io.workbook_bytes(wb)


def create_import_preview(
    user, project: Project, source: Union[str, "BinaryIO"],
    *, original_filename: str = "", mode: str = "upsert",
) -> DataJob:
    if mode not in IMPORT_MODES:
        raise service.ServiceError(f"未知导入模式: {mode}", code="VALIDATION_ERROR")
    if not project.is_editable:
        raise service.ServiceError("项目当前不可编辑", code="PROJECT_LOCKED")
    specs_defs = [
        f.to_dict() for f in service.list_fields(project.id, active_only=True)
        if f.sheet == "test"
    ]
    specs = [FieldSpec.from_definition(d) for d in specs_defs]

    try:
        parsed = excel_io.parse_import(source, specs_defs)
    except excel_io.ExcelIOError as exc:
        raise service.ServiceError(str(exc), code="IMPORT_PARSE_ERROR")

    if parsed["missing_required"]:
        raise service.ServiceError(
            "缺少必填列: " + ", ".join(parsed["missing_required"]),
            code="IMPORT_MISSING_COLUMNS",
            details=parsed["missing_required"])

    existing_ids = {
        r.case_id for r in TestItemRow.query.filter_by(
            project_id=project.id, sheet="test", deleted_at=None).all()
    }

    errors: list[dict] = []
    valid = insert_n = update_n = skip_n = 0
    seen_case_ids: set[str] = set()
    normalized_rows: list[dict] = []

    for entry in parsed["rows"]:
        values = entry["values"]
        cols = entry["cols"]
        coerced, verrs = validation.validate_record(specs, values)
        case_id = coerced.get("case_id")
        row_errors = [e for e in verrs if e.severity == "blocking"]

        if case_id and case_id in seen_case_ids:
            row_errors.append(validation.FieldError("case_id", "文件内测试ID重复"))
        elif case_id:
            seen_case_ids.add(case_id)

        is_update = case_id in existing_ids
        if mode == "insert_only" and is_update:
            row_errors.append(validation.FieldError("case_id", "测试ID已存在(仅新增模式)"))
        if mode == "update_only" and not is_update:
            row_errors.append(validation.FieldError("case_id", "测试ID不存在(仅更新模式)"))

        if row_errors:
            for e in row_errors:
                errors.append({
                    "sheet": excel_io.DATA_SHEET, "row": entry["row"],
                    "column": cols.get(e.field, e.field),
                    "field": e.field, "value": values.get(e.field),
                    "message": e.message,
                })
            continue

        valid += 1
        if is_update:
            update_n += 1
        else:
            insert_n += 1
        normalized_rows.append({"row": entry["row"], "values": coerced,
                                "is_update": is_update})

    preview = {
        "mode": mode,
        "total": len(parsed["rows"]),
        "valid": valid,
        "invalid": len(parsed["rows"]) - valid,
        "insert": insert_n,
        "update": update_n,
        "skip": skip_n,
        "unmapped": parsed["unmapped"],
        "errors": errors[: settings.IMPORT_ERROR_LIMIT],
        "rows": normalized_rows,
    }
    job = DataJob(
        project_id=project.id, job_type="import", status="previewed",
        original_filename=original_filename,
        parameters={
            "mode": mode,
            "_snapshot": import_job_service.capture_snapshot(project.id, "test"),
        },
        preview=preview, total_count=len(parsed["rows"]),
        error_count=len(errors), created_by=user.id,
        expires_at=_utcnow() + _dt.timedelta(days=1),
    )
    db.session.add(job)
    audit.record("import.preview", actor_id=user.id, object_type="import",
                 object_id=None, project_id=project.id,
                 new_value={"mode": mode, "valid": valid, "invalid": preview["invalid"]})
    db.session.commit()
    return job


def commit_import(user, project: Project, job: DataJob) -> dict:
    if job.job_type != "import" or job.status != "previewed":
        raise service.ServiceError("导入任务状态无效", code="VALIDATION_ERROR")
    if (job.parameters or {}).get("format"):
        return import_job_service.commit_special_import(user, project, job)
    if not project.is_editable:
        raise service.ServiceError("项目当前不可编辑", code="PROJECT_LOCKED")
    if job.expires_at is None:
        raise service.ServiceError(
            "导入预览缺少有效期，请重新上传",
            code="IMPORT_PREVIEW_INVALID",
        )
    if _expired(job.expires_at):
        raise service.ServiceError("导入预览已过期，请重新上传", code="IMPORT_PREVIEW_EXPIRED")
    preview = job.preview or {}
    mode = (job.parameters or {}).get("mode", "upsert")
    if mode == "replace_all":
        permissions.require(
            "import.replace",
            service.role_in_project(project.id, user),
            is_system_admin=bool(user is not None and user.is_system_admin),
        )
    rows = preview.get("rows", [])
    if preview.get("invalid", 0) > 0:
        raise service.ServiceError("存在校验未通过的行，无法提交", code="IMPORT_HAS_ERRORS")
    expected_snapshot = (job.parameters or {}).get("_snapshot")
    if (not isinstance(expected_snapshot, dict) or
            not isinstance(expected_snapshot.get("rows"), list) or
            not isinstance(expected_snapshot.get("fields"), list) or
            not isinstance(expected_snapshot.get("project"), dict)):
        raise service.ServiceError(
            "导入预览缺少数据快照，请重新上传",
            code="IMPORT_PREVIEW_INVALID",
        )
    if expected_snapshot != import_job_service.capture_snapshot(project.id, "test"):
        raise service.ServiceError(
            "预览后的数据或字段已变化，请重新上传并预览",
            code="IMPORT_PREVIEW_STALE",
        )

    specs = [spec for spec in service.field_specs(project.id)
             if spec.sheet == "test"]
    inserted = updated = 0
    try:
        if mode == "replace_all":
            for it in TestItemRow.query.filter_by(
                    project_id=project.id, sheet="test", deleted_at=None):
                it.deleted_at = _utcnow()
            db.session.flush()

        by_case = {}
        if mode in ("upsert", "update_only"):
            for it in TestItemRow.query.filter_by(project_id=project.id, deleted_at=None):
                if it.sheet == "test":
                    by_case[it.case_id] = it

        max_order = db.session.query(db.func.max(TestItemRow.row_order)) \
            .filter_by(project_id=project.id, sheet="test").scalar() or 0

        for row in rows:
            values = row["values"]
            case_id = values.get("case_id")
            target = by_case.get(case_id) if mode in ("upsert", "update_only") else None
            if target is not None:
                for spec in specs:
                    if not spec.is_readonly and spec.field_key in values:
                        target.set_field(spec.field_key, values[spec.field_key])
                target.version += 1
                target.updated_by = user.id
                target.updated_at = _utcnow()
                updated += 1
            else:
                max_order += 1
                item = TestItemRow(project_id=project.id, row_order=max_order,
                                   sheet="test", created_by=user.id,
                                   updated_by=user.id, version=1)
                for spec in specs:
                    if not spec.is_readonly and spec.field_key in values:
                        item.set_field(spec.field_key, values[spec.field_key])
                db.session.add(item)
                inserted += 1

        job.status = "completed"
        job.success_count = inserted + updated
        job.finished_at = _utcnow()
        audit.record("import.commit", actor_id=user.id, object_type="import",
                     object_id=job.id, project_id=project.id,
                     new_value={"inserted": inserted, "updated": updated, "mode": mode})
        db.session.commit()
    except Exception as exc:  # noqa: BLE001
        db.session.rollback()
        job.status = "failed"
        db.session.commit()
        raise service.ServiceError(f"导入失败并已回滚: {exc}", code="IMPORT_FAILED")
    return {"inserted": inserted, "updated": updated}


def _expired(expires_at) -> bool:
    if expires_at is None:
        return False
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=_dt.timezone.utc)
    return expires_at <= _utcnow()


def export_project(project: Project, *, columns: Optional[list[str]] = None,
                   item_ids: Optional[list[int]] = None,
                   filters: Optional[list] = None):
    specs = [f.to_dict() for f in service.list_fields(project.id, active_only=True)]
    q = TestItemRow.query.filter_by(project_id=project.id, deleted_at=None)
    if item_ids:
        q = q.filter(TestItemRow.id.in_(item_ids))
    rows = [it.to_dict() for it in q.order_by(TestItemRow.row_order).all()]
    meta = project.to_dict()
    meta["_exported_at"] = _utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
    meta["_data_version"] = str(len(rows))
    wb = excel_io.build_export(meta, specs, rows, columns=columns)
    return excel_io.workbook_bytes(wb)
