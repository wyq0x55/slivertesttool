"""Preview and transactionally commit the workbook import formats."""

from __future__ import annotations

import datetime as _dt
import logging
from typing import Any

from sqlalchemy import text

from ...extensions import db
from ...models import DataJob, FieldDefinition, Project, ProjectMember, TestItemRow
from . import audit, fields as fld, fields_service, permissions, service, settings, validation
from .validation import FieldSpec

logger = logging.getLogger(__name__)

_SPECIAL_FORMATS = {
    "test_matrix": ("test", fld.TEST_FIELDS, "test_id"),
    "libfunc": ("lib", fld.LIB_FIELDS, "lib_func"),
    "const": ("const", fld.CONST_FIELDS, "const_name"),
    "io": ("io", fld.IO_FIELDS, "io_name"),
}
_SPECIAL_MODES = ("upsert", "insert_only", "replace_all")


def _utcnow() -> _dt.datetime:
    return _dt.datetime.now(_dt.timezone.utc)


def _capture_row_snapshot(project_id: int, sheet: str) -> list[dict[str, Any]]:
    rows = TestItemRow.query.filter_by(
        project_id=project_id, sheet=sheet, deleted_at=None,
    ).order_by(TestItemRow.id).all()
    return [
        {
            "id": row.id,
            "version": row.version,
            "case_id": row.case_id,
            "identity": (
                str(row.get_field("io_name") or row.case_id or "").strip()
                if sheet == "io" else row.case_id
            ),
        }
        for row in rows
    ]


def capture_snapshot(project_id: int, sheet: str) -> dict[str, Any]:
    fields = FieldDefinition.query.filter_by(
        project_id=project_id, sheet=sheet,
    ).order_by(FieldDefinition.id).all()
    project = db.session.get(Project, project_id)
    return {
        "project": {
            "tm_id_prefix": project.tm_id_prefix if project else None,
            "tm_summary_sheet": project.tm_summary_sheet if project else None,
        },
        "rows": _capture_row_snapshot(project_id, sheet),
        "fields": [
            {
                "id": field.id,
                "field_key": field.field_key,
                "data_type": field.data_type,
                "is_required": field.is_required,
                "is_readonly": field.is_readonly,
                "default_value": field.default_value,
                "validation_rule": field.validation_rule,
                "option_source": field.option_source,
                "is_active": field.is_active,
                "deleted_at": field.deleted_at.isoformat() if field.deleted_at else None,
            }
            for field in fields
        ],
    }


def _lock_special_import_tables() -> None:
    connection = db.session.connection()
    if connection.dialect.name != "postgresql":
        return
    tables = ", ".join(
        connection.dialect.identifier_preparer.format_table(table)
        for table in (TestItemRow.__table__, FieldDefinition.__table__)
    )
    db.session.execute(text(
        f"LOCK TABLE {tables} IN SHARE ROW EXCLUSIVE MODE"
    ))


def _identity_key(import_format: str, identity: Any) -> str:
    value = str(identity or "").strip()
    return value.lower() if import_format == "io" else value


def create_special_preview(
    user, project: Project, source, *, import_format: str,
    original_filename: str = "", mode: str = "upsert",
) -> DataJob:
    if import_format not in _SPECIAL_FORMATS:
        raise service.ServiceError("不支持的导入格式", code="VALIDATION_ERROR")
    if mode not in _SPECIAL_MODES:
        raise service.ServiceError(f"未知导入模式: {mode}", code="VALIDATION_ERROR")
    if not project.is_editable:
        raise service.ServiceError("项目当前不可编辑", code="PROJECT_LOCKED")
    if mode == "replace_all":
        permissions.require(
            "import.replace", service.role_in_project(project.id, user),
            is_system_admin=bool(user is not None and user.is_system_admin),
        )

    items, metadata = _parse_workbook(import_format, source, original_filename)
    sheet, field_definitions, identity_field = _SPECIAL_FORMATS[import_format]
    specs = _field_specs(project.id, sheet, field_definitions)
    rows, errors, inserts, updates = _build_preview_rows(
        project, import_format, items, specs, identity_field, mode,
        metadata.get("id_prefix"),
    )
    invalid = len(items) - len(rows)
    preview = {
        "mode": mode,
        "format": import_format,
        "sheet": sheet,
        "total": len(items),
        "valid": len(rows),
        "invalid": invalid,
        "insert": inserts,
        "update": updates,
        "skip": 0,
        "unmapped": [],
        "errors": errors[:settings.IMPORT_ERROR_LIMIT],
        "rows": rows,
    }
    parameters = {
        "mode": mode,
        "format": import_format,
        "_snapshot": capture_snapshot(project.id, sheet),
    }
    if import_format == "test_matrix":
        parameters["tm_id_prefix"] = metadata.get("id_prefix")
        parameters["tm_summary_sheet"] = metadata.get("summary_sheet")

    job = DataJob(
        project_id=project.id,
        job_type="import",
        status="previewed",
        original_filename=original_filename,
        parameters=parameters,
        preview=preview,
        total_count=len(items),
        error_count=len(errors),
        created_by=user.id,
        expires_at=_utcnow() + _dt.timedelta(days=1),
    )
    db.session.add(job)
    audit.record(
        "import.preview", actor_id=user.id, object_type="import",
        project_id=project.id,
        new_value={"format": import_format, "mode": mode,
                   "valid": len(rows), "invalid": invalid},
    )
    db.session.commit()
    return job


def commit_special_import(user, project: Project, job: DataJob) -> dict[str, Any]:
    job_id = job.id
    project_id = project.id
    deleted = 0
    inserted = updated = 0
    try:
        job = (DataJob.query.filter_by(id=job_id)
               .with_for_update().populate_existing().first())
        if job is None or job.project_id != project_id:
            raise service.ServiceError("导入任务不存在", code="NOT_FOUND")
        project = (Project.query.filter_by(id=project_id)
                   .with_for_update().populate_existing().first())
        if project is None:
            raise service.ServiceError("项目不存在", code="NOT_FOUND")
        if (user is not None and not user.is_system_admin):
            (ProjectMember.query.filter_by(
                project_id=project.id, user_id=user.id,
            ).with_for_update().populate_existing().first())

        parameters = job.parameters or {}
        import_format = parameters.get("format")
        if job.job_type != "import" or job.status != "previewed":
            raise service.ServiceError("导入任务状态无效", code="VALIDATION_ERROR")
        if import_format not in _SPECIAL_FORMATS:
            raise service.ServiceError("导入任务格式无效", code="VALIDATION_ERROR")
        if job.expires_at is None:
            raise service.ServiceError(
                "导入预览缺少有效期，请重新上传", code="IMPORT_PREVIEW_INVALID",
            )
        if _expired(job.expires_at):
            raise service.ServiceError(
                "导入预览已过期，请重新上传", code="IMPORT_PREVIEW_EXPIRED",
            )
        if not project.is_editable:
            raise service.ServiceError("项目当前不可编辑", code="PROJECT_LOCKED")

        mode = parameters.get("mode", "upsert")
        if mode not in _SPECIAL_MODES:
            raise service.ServiceError("导入任务模式无效", code="VALIDATION_ERROR")
        role = service.role_in_project(project.id, user)
        is_system_admin = bool(user is not None and user.is_system_admin)
        permissions.require("import.run", role, is_system_admin=is_system_admin)
        if mode == "replace_all":
            permissions.require(
                "import.replace", role, is_system_admin=is_system_admin,
            )

        preview = job.preview or {}
        if preview.get("invalid", 0) > 0:
            raise service.ServiceError(
                "存在校验未通过的行，无法提交", code="IMPORT_HAS_ERRORS",
            )
        expected_snapshot = parameters.get("_snapshot")
        if (not isinstance(expected_snapshot, dict) or
                not isinstance(expected_snapshot.get("rows"), list) or
                not isinstance(expected_snapshot.get("fields"), list) or
                not isinstance(expected_snapshot.get("project"), dict)):
            raise service.ServiceError(
                "导入预览缺少数据快照，请重新上传", code="IMPORT_PREVIEW_INVALID",
            )

        sheet, field_definitions, _identity_field = _SPECIAL_FORMATS[import_format]
        _lock_special_import_tables()
        if expected_snapshot != capture_snapshot(project.id, sheet):
            raise service.ServiceError(
                "预览后的数据或字段已变化，请重新上传并预览",
                code="IMPORT_PREVIEW_STALE",
            )

        fields_service.ensure_fields(
            user, project, field_definitions, commit=False,
        )
        if expected_snapshot["rows"] != _capture_row_snapshot(project.id, sheet):
            raise service.ServiceError(
                "预览后的数据已变化，请重新上传并预览",
                code="IMPORT_PREVIEW_STALE",
            )
        if import_format == "test_matrix":
            from . import testmatrix_bridge

            testmatrix_bridge._save_project_meta(project, {
                "id_prefix": parameters.get("tm_id_prefix"),
                "summary_sheet": parameters.get("tm_summary_sheet"),
            })

        if mode == "replace_all":
            now = _utcnow()
            live = TestItemRow.query.filter_by(
                project_id=project.id, sheet=sheet, deleted_at=None,
            ).populate_existing().all()
            for item in live:
                item.deleted_at = now
            deleted = len(live)
            if deleted:
                audit.record(
                    "import.replace_all", actor_id=user.id,
                    object_type="import", project_id=project.id,
                    new_value={"deleted": deleted,
                               "source": job.original_filename,
                               "sheet": sheet},
                )

        current = TestItemRow.query.filter_by(
            project_id=project.id, sheet=sheet, deleted_at=None,
        ).populate_existing().all()
        by_case = {
            _identity_key(
                import_format,
                item.get_field("io_name") or item.case_id
                if import_format == "io" else item.case_id,
            ): item
            for item in current
        }
        expected_versions = {
            _identity_key(
                import_format, row.get("identity", row.get("case_id")),
            ): row["version"]
            for row in expected_snapshot["rows"]
        }
        for row in preview.get("rows", []):
            values = row["values"]
            case_id = row["identity"]
            identity_key = _identity_key(import_format, case_id)
            target = by_case.get(identity_key) if mode == "upsert" else None
            if row.get("is_update"):
                if target is None:
                    raise service.ServiceError(
                        "预览后的目标行已变化，请重新上传并预览",
                        code="IMPORT_PREVIEW_STALE",
                    )
                changes = {key: value for key, value in values.items()
                           if key != "case_id"}
                expected_version = expected_versions.get(identity_key)
                if expected_version is None:
                    raise service.ServiceError(
                        "预览后的目标行已变化，请重新上传并预览",
                        code="IMPORT_PREVIEW_STALE",
                    )
                service.update_item(
                    user, project, target, expected_version,
                    changes, commit=False,
                )
                updated += 1
            else:
                if mode in ("upsert", "insert_only") and identity_key in by_case:
                    raise service.ServiceError(
                        "预览后的目标行已变化，请重新上传并预览",
                        code="IMPORT_PREVIEW_STALE",
                    )
                item = service.create_item(
                    user, project, values, draft=True, sheet=sheet,
                    commit=False,
                )
                by_case[identity_key] = item
                inserted += 1

        job.status = "completed"
        job.success_count = inserted + updated
        job.error_count = 0
        job.finished_at = _utcnow()
        audit.record(
            "import.commit", actor_id=user.id, object_type="import",
            object_id=job.id, project_id=project.id,
            new_value={"format": import_format, "mode": mode,
                       "inserted": inserted, "updated": updated,
                       "deleted": deleted},
        )
        db.session.commit()
    except permissions.PermissionDenied:
        db.session.rollback()
        raise
    except service.VersionConflict as exc:
        db.session.rollback()
        raise service.ServiceError(
            "预览后的目标行已变化，请重新上传并预览",
            code="IMPORT_PREVIEW_STALE",
        ) from exc
    except service.ServiceError:
        db.session.rollback()
        raise
    except Exception as exc:  # noqa: BLE001 - rollback the complete import transaction
        logger.exception("Special import commit failed for job %s", job_id)
        db.session.rollback()
        raise service.ServiceError(
            "导入失败，所有更改均已回滚", code="IMPORT_FAILED",
        ) from exc

    return {
        "inserted": inserted,
        "updated": updated,
        "deleted": deleted,
        "mode": mode,
        "format": import_format,
    }


def _parse_workbook(import_format: str, source, original_filename: str):
    from . import const_excel, io_excel, libfunc_excel, matrix_excel

    try:
        if import_format == "test_matrix":
            parsed = matrix_excel.parse_workbook(
                source, source_filename=original_filename,
            )
        elif import_format == "libfunc":
            parsed = libfunc_excel.parse_workbook(
                source, source_filename=original_filename,
            )
        elif import_format == "const":
            parsed = const_excel.parse_workbook(
                source, source_filename=original_filename,
            )
        else:
            parsed = io_excel.parse_workbook(
                source, source_filename=original_filename,
            )
    except Exception as exc:  # noqa: BLE001 - parser errors are user input failures
        logger.info("Import preview parse failed for format %s", import_format)
        message = str(exc) if exc.__class__.__name__.endswith("ExcelError") else "无法解析该 Excel 文件"
        raise service.ServiceError(
            f"Excel 解析失败：{message}", code="IMPORT_PARSE_ERROR",
        ) from exc

    items = parsed.get("items") or []
    if not items:
        raise service.ServiceError(
            "导入失败：未在文件中解析到任何可导入行",
            code="IMPORT_PARSE_ERROR",
        )
    metadata = parsed if import_format == "test_matrix" else {}
    return items, metadata


def _field_specs(project_id: int, sheet: str, defaults: list[dict[str, Any]]):
    definitions = {
        field.field_key: field.to_dict()
        for field in fields_service.list_fields(project_id, active_only=True)
        if field.sheet == sheet
    }
    for definition in defaults:
        definitions.setdefault(definition["field_key"], definition)
    return [FieldSpec.from_definition(definition)
            for definition in definitions.values()]


def _build_preview_rows(project, import_format, items, specs, identity_field,
                        mode, id_prefix):
    from . import libconst_bridge, matrix_excel, testmatrix_bridge

    sheet = _SPECIAL_FORMATS[import_format][0]
    live_rows = TestItemRow.query.filter_by(
        project_id=project.id, sheet=sheet, deleted_at=None,
    ).all()
    existing = {
        (str(row.get_field("io_name") or row.case_id).strip().lower()
         if import_format == "io" else row.case_id): row
        for row in live_rows
    }
    path_owners = {}
    if import_format == "io" and mode != "replace_all":
        for row in live_rows:
            path = str(row.get_field("io_path") or "").strip().lower()
            if path:
                path_owners[path] = str(
                    row.get_field("io_name") or row.case_id
                ).strip().lower()

    seen = set()
    seen_paths = set()
    rows = []
    errors = []
    inserts = updates = 0
    for index, item in enumerate(items, start=1):
        if import_format == "test_matrix":
            values = testmatrix_bridge.map_item(item)
            identity = testmatrix_bridge.reconstruct_case_id(
                id_prefix or matrix_excel.DEFAULT_ID_PREFIX, item,
            )
            if identity:
                values.setdefault("test_id", identity)
        elif import_format == "libfunc":
            values = libconst_bridge.map_lib_item(item)
            identity = str(item.get(identity_field) or "").strip()
        elif import_format == "const":
            values = libconst_bridge.map_const_item(item)
            identity = str(item.get(identity_field) or "").strip()
        else:
            values = libconst_bridge.map_io_item(item)
            identity = str(item.get(identity_field) or "").strip()

        row_errors = []
        normalized_identity = identity.lower() if import_format == "io" else identity
        existing_key = normalized_identity if import_format == "io" else identity
        target = existing.get(existing_key) if mode == "upsert" else None
        if not identity:
            row_errors.append((identity_field, "标识不能为空"))
        elif normalized_identity in seen:
            row_errors.append((identity_field, "文件内标识重复"))
        elif mode == "insert_only" and existing_key in existing:
            row_errors.append((identity_field, "标识已存在(仅新增模式)"))
        if identity:
            seen.add(normalized_identity)

        if import_format == "io":
            name = identity
            path = str(values.get("io_path") or "").strip()
            normalized_path = path.lower()
            if path and normalized_path in path_owners:
                path_owner = path_owners[normalized_path]
                if path_owner != normalized_identity:
                    row_errors.append(("io_path", f"路径已存在：{path}"))
            if path and normalized_path in seen_paths:
                row_errors.append(("io_path", f"文件内路径重复：{path}"))
            if path:
                seen_paths.add(normalized_path)
            values["case_id"] = name
        elif identity:
            values["case_id"] = identity

        field_values = {key: value for key, value in values.items()
                        if key != "case_id"}
        coerced, field_errors = validation.validate_record(
            specs, field_values, enforce_required=False,
        )
        row_errors.extend((error.field, error.message)
                          for error in field_errors if error.severity == "blocking")
        if row_errors:
            errors.extend({
                "row": index,
                "column": field,
                "field": field,
                "message": message,
            } for field, message in row_errors)
            continue

        normalized_values = {key: coerced.get(key, value)
                             for key, value in field_values.items()}
        normalized_values["case_id"] = identity
        is_update = target is not None and mode == "upsert"
        rows.append({
            "row": index,
            "identity": identity,
            "values": normalized_values,
            "is_update": is_update,
        })
        if is_update:
            updates += 1
        else:
            inserts += 1

        if import_format == "io" and values.get("io_path"):
            path_owners[str(values["io_path"]).strip().lower()] = normalized_identity

    return rows, errors, inserts, updates


def _expired(expires_at) -> bool:
    if expires_at is None:
        return False
    expires_at = expires_at.replace(tzinfo=_dt.timezone.utc) if expires_at.tzinfo is None else expires_at
    return expires_at <= _utcnow()
