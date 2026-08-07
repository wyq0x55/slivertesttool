"""Field-definition service (LAN Test Matrix): per-project custom fields."""
from __future__ import annotations

import datetime as _dt
from typing import Any, Optional

from ...extensions import db
from ...models import FieldDefinition, LMUser, Project, TestItemRow
from . import audit, fields as fld
from .errors import ServiceError
from .validation import FieldSpec


def _utcnow() -> _dt.datetime:
    return _dt.datetime.now(_dt.timezone.utc)


# --------------------------------------------------------------------------- #
# Fields
# --------------------------------------------------------------------------- #
def list_fields(project_id: int, *, active_only: bool = False,
                include_deleted: bool = False) -> list[FieldDefinition]:
    q = FieldDefinition.query.filter_by(project_id=project_id)
    if not include_deleted:
        q = q.filter(FieldDefinition.deleted_at.is_(None))
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
    clash = FieldDefinition.query.filter_by(
        project_id=project.id, field_key=field_key).first()
    if clash is not None:
        # The unique constraint covers soft-deleted rows too, so this cannot be
        # papered over by creating a second definition -- and we would not want
        # to: the old field's values are still sitting in every row's
        # custom_values under this key, and a "new" field would silently inherit
        # them. Send the user to the recycle bin instead.
        if clash.deleted_at is not None:
            raise ServiceError(
                "字段标识 %s 在回收站中，请先还原或彻底删除" % field_key,
                code="DUPLICATE")
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
    number of fields created. A field the user had deleted is revived rather
    than reported as a duplicate: the importer needs the key to be live, and the
    values it is about to write belong under exactly that key.
    """
    existing = {
        f.field_key: f for f in FieldDefinition.query.filter_by(
            project_id=project.id).all()
    }
    created = 0
    for spec in specs:
        have = existing.get(spec["field_key"])
        if have is not None:
            if have.deleted_at is not None:
                restore_field(user, project, have, commit=False)
            continue
        add_field(user, project, spec)
        created += 1
    db.session.commit()
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
                 *, commit: bool = True) -> None:
    """Soft-delete a field definition.

    Any field may be deleted — there are no protected system fields. The
    column disappears from the editor immediately, but the values stay in each
    row's ``custom_values`` so a restore brings the data back with the column.
    Deleting a field is the most destructive action in the product (it removes a
    column from every test case at once); it used to also be the only
    irreversible one. The values are purged only when the recycle bin expires
    the field for real — see ``trash_service.purge_expired``.
    """
    if fdef.deleted_at is not None:
        return
    old = fdef.to_dict()
    fdef.deleted_at = _utcnow()
    fdef.deleted_by = user.id
    audit.record("field.delete", actor_id=user.id, object_type="field",
                 object_id=fdef.field_key, project_id=project.id,
                 old_value=old)
    if commit:
        db.session.commit()


def restore_field(user: LMUser, project: Project, fdef: FieldDefinition,
                  *, commit: bool = True) -> FieldDefinition:
    """Bring a soft-deleted field (and every value stored under it) back."""
    if fdef.deleted_at is None:
        raise ServiceError("回收站中无此字段", code="NOT_FOUND")
    fdef.deleted_at = None
    fdef.deleted_by = None
    audit.record("field.restore", actor_id=user.id, object_type="field",
                 object_id=fdef.field_key, project_id=project.id,
                 new_value=fdef.to_dict())
    if commit:
        db.session.commit()
    return fdef


def purge_field(user: LMUser, project_id: int, fdef: FieldDefinition,
                *, commit: bool = True) -> None:
    """Delete a field for real, taking its stored values with it.

    This is the only path that touches ``custom_values``; it runs either from
    the recycle bin's explicit "彻底删除" action or from the retention sweep.
    """
    old = fdef.to_dict()
    field_key = fdef.field_key
    rows = TestItemRow.query.filter_by(project_id=project_id).all()
    for row in rows:
        cv = row.custom_values or {}
        if field_key in cv:
            cv = dict(cv)
            cv.pop(field_key, None)
            row.custom_values = cv
    db.session.delete(fdef)
    audit.record("field.purge", actor_id=(user.id if user else None),
                 object_type="field", object_id=field_key,
                 project_id=project_id, old_value=old)
    if commit:
        db.session.commit()
