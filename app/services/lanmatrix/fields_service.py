"""Field-definition service (LAN Test Matrix): per-project custom fields."""
from __future__ import annotations

from typing import Any, Optional

from ...extensions import db
from ...models import FieldDefinition, LMUser, Project, TestItemRow
from . import audit, fields as fld
from .errors import ServiceError
from .validation import FieldSpec


# --------------------------------------------------------------------------- #
# Fields
# --------------------------------------------------------------------------- #
def list_fields(project_id: int, *, active_only: bool = False) -> list[FieldDefinition]:
    q = FieldDefinition.query.filter_by(project_id=project_id)
    if active_only:
        q = q.filter_by(is_active=True)
    return q.order_by(FieldDefinition.display_order).all()


def field_specs(project_id: int, *, active_only: bool = True) -> list[FieldSpec]:
    return [FieldSpec.from_definition(f.to_dict())
            for f in list_fields(project_id, active_only=active_only)]


def add_field(user: LMUser, project: Project, data: dict[str, Any]) -> FieldDefinition:
    field_key = (data.get("field_key") or "").strip()
    if not field_key:
        raise ServiceError("字段标识不能为空", code="VALIDATION_ERROR")
    if FieldDefinition.query.filter_by(project_id=project.id, field_key=field_key).first():
        raise ServiceError("字段标识已存在", code="DUPLICATE")
    data_type = data.get("data_type", "text")
    if data_type not in fld.DATA_TYPES:
        raise ServiceError(f"不支持的数据类型: {data_type}", code="VALIDATION_ERROR")
    max_order = db.session.query(db.func.max(FieldDefinition.display_order)) \
        .filter_by(project_id=project.id).scalar() or 0
    sheet = (data.get("sheet") or fld.DEFAULT_SHEET).strip()
    if sheet not in fld.SHEETS:
        raise ServiceError(f"不支持的 Sheet 页: {sheet}", code="VALIDATION_ERROR")
    fdef = FieldDefinition(
        project_id=project.id, field_key=field_key,
        display_name=data.get("display_name") or field_key,
        data_type=data_type,
        sheet=sheet,
        is_system=False,
        is_required=bool(data.get("is_required", False)),
        is_readonly=bool(data.get("is_readonly", False)),
        default_value=data.get("default_value"),
        validation_rule=data.get("validation_rule") or {},
        option_source={"options": data.get("options", [])} if data.get("options") else None,
        help_text=data.get("help_text", ""),
        display_order=max_order + 1,
        is_active=True,
    )
    db.session.add(fdef)
    audit.record("field.create", actor_id=user.id, object_type="field",
                 object_id=field_key, project_id=project.id, new_value=data)
    db.session.commit()
    return fdef


def ensure_fields(user: LMUser, project: Project,
                  specs: list[dict[str, Any]]) -> int:
    """Create any of ``specs`` that the project does not yet have.

    Used by the Lib / Const importers to provision their field set on the target
    project before creating rows (``create_item`` only applies values whose keys
    exist as field definitions). Existing fields are left untouched; returns the
    number of fields created.
    """
    existing = {
        f.field_key for f in FieldDefinition.query.filter_by(
            project_id=project.id).all()
    }
    created = 0
    for spec in specs:
        if spec["field_key"] in existing:
            continue
        add_field(user, project, spec)
        created += 1
    return created


def update_field(user: LMUser, project: Project, fdef: FieldDefinition,
                 changes: dict[str, Any]) -> FieldDefinition:
    old = fdef.to_dict()
    # field_key is immutable (it is the storage/column-routing identity); every
    # other attribute — including data_type — can be changed.
    if "data_type" in changes:
        new_type = changes["data_type"]
        if new_type not in fld.DATA_TYPES:
            raise ServiceError(f"不支持的数据类型: {new_type}", code="VALIDATION_ERROR")
        fdef.data_type = new_type
    if "sheet" in changes:
        new_sheet = (changes["sheet"] or fld.DEFAULT_SHEET).strip()
        if new_sheet not in fld.SHEETS:
            raise ServiceError(f"不支持的 Sheet 页: {new_sheet}", code="VALIDATION_ERROR")
        fdef.sheet = new_sheet
    for key in ("display_name", "is_required", "is_readonly", "default_value",
                "validation_rule", "help_text", "display_order", "is_active"):
        if key in changes:
            setattr(fdef, key, changes[key])
    if "options" in changes:
        fdef.option_source = {"options": changes["options"]}
    audit.record("field.update", actor_id=user.id, object_type="field",
                 object_id=fdef.field_key, project_id=project.id,
                 old_value=old, new_value=fdef.to_dict())
    db.session.commit()
    return fdef


def delete_field(user: LMUser, project: Project, fdef: FieldDefinition,
                 *, purge_values: bool = True) -> None:
    """Delete a field definition.

    Any field may be deleted — there are no protected system fields. By default
    the field's stored values are also purged from every item's
    ``custom_values`` so no orphaned data lingers.
    """
    old = fdef.to_dict()
    field_key = fdef.field_key
    if purge_values:
        rows = TestItemRow.query.filter_by(project_id=project.id).all()
        for row in rows:
            cv = row.custom_values or {}
            if field_key in cv:
                cv = dict(cv)
                cv.pop(field_key, None)
                row.custom_values = cv
    db.session.delete(fdef)
    audit.record("field.delete", actor_id=user.id, object_type="field",
                 object_id=field_key, project_id=project.id, old_value=old)
    db.session.commit()
