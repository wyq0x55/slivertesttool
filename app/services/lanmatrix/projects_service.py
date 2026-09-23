"""Project lifecycle service (LAN Test Matrix): list/create/update/delete."""
from __future__ import annotations

import datetime as _dt
import logging
import shutil
from typing import Any, Optional

from flask import current_app

from ...extensions import db
from ...models import FieldDefinition, LMUser, Project, ProjectMember
from . import audit
from .errors import ServiceError


def _utcnow() -> _dt.datetime:
    return _dt.datetime.now(_dt.timezone.utc)


# --------------------------------------------------------------------------- #
# Projects
# --------------------------------------------------------------------------- #
def list_projects(user: LMUser, *, include_deleted: bool = False) -> list[Project]:
    q = Project.query
    if not include_deleted:
        q = q.filter(Project.deleted_at.is_(None))
    projects = q.order_by(Project.updated_at.desc()).all()
    if user.is_system_admin:
        return projects
    member_ids = {
        m.project_id for m in ProjectMember.query.filter_by(user_id=user.id).all()
    }
    return [p for p in projects if p.id in member_ids or p.owner_id == user.id]


def create_project(user: LMUser, *, code: str, name: str,
                   description: str = "", owner_id: Optional[int] = None) -> Project:
    code = (code or "").strip().upper()
    if not code:
        raise ServiceError("项目编码不能为空", code="VALIDATION_ERROR")
    if Project.query.filter_by(code=code).first():
        raise ServiceError("项目编码已存在", code="DUPLICATE")
    project = Project(
        code=code, name=(name or "").strip() or code, description=description,
        status="draft", owner_id=owner_id or user.id, created_by=user.id,
    )
    db.session.add(project)
    db.session.flush()
    # Creator becomes project admin.
    db.session.add(ProjectMember(
        project_id=project.id, user_id=user.id, role="project_admin"))
    # A new project starts empty: no seeded fields and no rows. Fields are
    # created on demand — by importing a Test-Matrix / Const / Lib workbook
    # (which provisions its own field set) or by adding them manually.
    audit.record("project.create", actor_id=user.id, object_type="project",
                 object_id=project.id, project_id=project.id,
                 new_value={"code": code, "name": project.name})
    db.session.commit()
    return project


def _heal_actor(project: Project) -> Optional[LMUser]:
    """Pick a user to attribute self-heal field creation to (audit actor)."""
    if project.owner_id:
        owner = db.session.get(LMUser, project.owner_id)
        if owner is not None:
            return owner
    return (LMUser.query.filter_by(is_system_admin=True).first()
            or LMUser.query.first())


def backfill_sheet_fields() -> int:
    """Self-heal orphaned data on startup.

    Projects are empty by default and fields are provisioned on import, so
    there is nothing to seed for a brand-new project. But a project imported
    under an older build (or before the importer provisioned its field set) can
    hold rows on a sheet that has **zero** field definitions. The editor then
    shows the row *count* with a blank header row and empty cells, because with
    no fields there are no columns to render into.

    Repair only that broken state: for every live project and every sheet, if
    the sheet has non-deleted rows but no field definitions, provision that
    sheet's default field set. Empty projects (no rows) are left untouched, so
    the "new project starts empty" contract still holds.
    """
    from . import fields as fld, fields_service
    from ...models import TestItemRow

    sheet_specs = {
        "test": fld.TEST_FIELDS,
        "const": fld.CONST_FIELDS,
        "lib": fld.LIB_FIELDS,
        "io": fld.IO_FIELDS,
    }
    created_total = 0
    projects = Project.query.filter(Project.deleted_at.is_(None)).all()
    for project in projects:
        actor = _heal_actor(project)
        if actor is None:
            continue
        for sheet, specs in sheet_specs.items():
            has_fields = FieldDefinition.query.filter_by(
                project_id=project.id, sheet=sheet).first() is not None
            if has_fields:
                continue
            has_rows = TestItemRow.query.filter_by(
                project_id=project.id, sheet=sheet, deleted_at=None).first() is not None
            if not has_rows:
                continue
            created_total += fields_service.ensure_fields(actor, project, specs)
    if created_total:
        db.session.commit()
    return created_total


def get_project(project_id: int) -> Project:
    project = db.session.get(Project, project_id)
    if project is None or project.deleted_at is not None:
        raise ServiceError("项目不存在", code="NOT_FOUND")
    return project


def update_project(user: LMUser, project: Project, changes: dict[str, Any]) -> Project:
    old = project.to_dict()
    for key in ("name", "description", "status", "owner_id"):
        if key in changes:
            setattr(project, key, changes[key])
    audit.record("project.update", actor_id=user.id, object_type="project",
                 object_id=project.id, project_id=project.id,
                 old_value=old, new_value=project.to_dict())
    db.session.commit()
    return project


def soft_delete_project(user: LMUser, project: Project) -> None:
    project.deleted_at = _utcnow()
    audit.record("project.delete", actor_id=user.id, object_type="project",
                 object_id=project.id, project_id=project.id)
    db.session.commit()


def delete_project(user: LMUser, project: Project) -> dict[str, int]:
    """Hard-delete a project, its DB-owned rows and safely-owned artifacts."""
    from ...models import (AuditLog, CellComment, DataJob, FieldDefinition,
                           Notification, ProjectMember, Task, TaskEvent,
                           TestItemRow)
    from ...runners import run_layout
    from ...services import project_model_service

    pid = project.id
    code = project.code
    cfg = current_app.config_obj
    counts: dict[str, int] = {}

    # Capture filesystem ownership before deleting DB rows. Legacy workspaces
    # were keyed by mutable/non-unique project names, so only remove a legacy
    # test subtree when no task from another project still references it.
    tasks = Task.query.filter_by(project_id=pid).all()
    safe_artifacts: set[tuple[str, str]] = set()
    seen_artifacts: set[tuple[str, str]] = set()
    skipped_shared = 0
    for task in tasks:
        workspace = (task.workspace or "").strip()
        test_id = (task.test_id or "").strip()
        if not (workspace and test_id):
            continue
        key = (workspace, test_id)
        if key in seen_artifacts:
            continue
        seen_artifacts.add(key)
        other = Task.query.filter(
            db.or_(Task.project_id != pid, Task.project_id.is_(None)),
            Task.workspace == workspace,
            Task.test_id == test_id,
        ).first()
        if other is not None:
            skipped_shared += 1
            continue
        safe_artifacts.add(key)

    code_workspace = run_layout.project_root(cfg, project)
    workspace_shared = Task.query.filter(
        db.or_(Task.project_id != pid, Task.project_id.is_(None)),
        Task.workspace == str(code_workspace),
    ).first() is not None
    model_root = project_model_service.project_models_root(cfg, project)

    task_ids = [t.id for t in tasks]
    if task_ids:
        counts["task_events"] = TaskEvent.query.filter(
            TaskEvent.task_id.in_(task_ids)).delete(synchronize_session=False)
        counts["tasks"] = Task.query.filter_by(project_id=pid).delete(
            synchronize_session=False)

    counts["notifications"] = Notification.query.filter_by(
        project_id=pid).delete(synchronize_session=False)
    counts["cell_comments"] = CellComment.query.filter_by(project_id=pid).delete(
        synchronize_session=False)
    counts["items"] = TestItemRow.query.filter_by(project_id=pid).delete(
        synchronize_session=False)
    counts["fields"] = FieldDefinition.query.filter_by(project_id=pid).delete(
        synchronize_session=False)
    counts["data_jobs"] = DataJob.query.filter_by(project_id=pid).delete(
        synchronize_session=False)
    counts["members"] = ProjectMember.query.filter_by(project_id=pid).delete(
        synchronize_session=False)
    counts["audit_logs"] = AuditLog.query.filter_by(project_id=pid).delete(
        synchronize_session=False)

    db.session.delete(project)
    audit.record("project.hard_delete", actor_id=user.id, object_type="project",
                 object_id=pid, project_id=None,
                 old_value={"code": code, "deleted": counts})
    db.session.commit()

    # Filesystem cleanup follows the committed DB deletion. A cleanup failure
    # must never restore/half-restore database state; log it as orphan cleanup
    # debt instead. New code-keyed roots are unique and may be removed whole.
    fs_errors = 0
    for workspace, test_id in safe_artifacts:
        try:
            shutil.rmtree(run_layout.log_dir(workspace, test_id),
                          ignore_errors=False)
        except FileNotFoundError:
            pass
        except Exception:
            fs_errors += 1
            logging.getLogger(__name__).exception(
                "could not remove task log for deleted project %s: %s/%s",
                code, workspace, test_id)
        try:
            shutil.rmtree(run_layout.staging_dir(workspace, test_id),
                          ignore_errors=False)
        except FileNotFoundError:
            pass
        except Exception:
            fs_errors += 1
            logging.getLogger(__name__).exception(
                "could not remove task staging dir for deleted project %s: %s/%s",
                code, workspace, test_id)

    if not workspace_shared:
        try:
            shutil.rmtree(code_workspace, ignore_errors=False)
        except FileNotFoundError:
            pass
        except Exception:
            fs_errors += 1
            logging.getLogger(__name__).exception(
                "could not remove workspace root for deleted project %s: %s",
                code, code_workspace)

    try:
        shutil.rmtree(model_root, ignore_errors=False)
    except FileNotFoundError:
        pass
    except Exception:
        fs_errors += 1
        logging.getLogger(__name__).exception(
            "could not remove model root for deleted project %s: %s",
            code, model_root)

    if skipped_shared:
        counts["shared_legacy_artifacts_skipped"] = skipped_shared
    if fs_errors:
        counts["filesystem_cleanup_errors"] = fs_errors
    return counts

