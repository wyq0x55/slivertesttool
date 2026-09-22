"""LAN Matrix route ownership: dashboard."""

from __future__ import annotations

import csv
import datetime as _dt
import io
import secrets
import zipfile
from pathlib import Path

from flask import (
    Blueprint, Response, current_app, g, request, send_file, session,
    stream_with_context,
)

from ...extensions import db
from ...models import (
    DataJob, FieldDefinition, LMUser, Project, ProjectMember, Task, TaskStatus,
)
from ...services import (
    event_service, license_service, project_model_service,
    report_service, task_service, upload_service,
)
from ...services.upload_service import UploadError
from ...services.lanmatrix import (
    audit, dbadmin, excel_service, fields, permissions, sbs_service, service,
    settings, trash_service,
)
from ...services.lanmatrix.permissions import PermissionDenied
from ...services.lanmatrix.service import ServiceError, VersionConflict
from ._base import (
    ok, err, arg_int, arg_json, arg_str, arg_date,
    current_user, login_required, system_admin_required,
    register_common, _project_and_role, _client_ip,
    _LOCK_THRESHOLD, _LOCK_MINUTES,
)



from .projects_items import bp


@bp.get("/projects/<int:project_id>/categories")
@login_required
def list_project_categories(project_id: int):
    """Distinct テスト区分 in this project, with names and case counts.

    Exists so the routing editor can offer the 区分 that actually exist instead
    of making an admin type them from memory: a mistyped 区分 produces a rule
    that never matches, and nothing in the UI would ever say so.
    """
    from ...models import TestItemRow
    from ...services.lanmatrix import review_routes as rr

    _project_and_role(project_id, "project.view")
    rows = (db.session.query(TestItemRow.custom_values)
            .filter(TestItemRow.project_id == project_id,
                    TestItemRow.deleted_at.is_(None),
                    TestItemRow.sheet == "test")
            .all())

    tally: dict[str, dict] = {}
    for (values,) in rows:
        values = values or {}
        key = rr.normalise_category(values.get(rr.CATEGORY_KEY))
        if not key:
            continue
        entry = tally.setdefault(
            key, {"category": key, "category_name": "", "count": 0})
        entry["count"] += 1
        if not entry["category_name"]:
            entry["category_name"] = str(
                values.get(rr.CATEGORY_NAME_KEY) or "").strip()

    # Numeric 区分 first and in numeric order (1, 2, 10 -- not 1, 10, 2), which
    # is the order the editor's category pager already uses.
    def sort_key(item: dict):
        raw = item["category"]
        try:
            return (0, float(raw), "")
        except ValueError:
            return (1, 0.0, raw)

    return ok({"categories": sorted(tally.values(), key=sort_key)})


@bp.get("/projects/<int:project_id>/dashboard")
@login_required
def project_dashboard_data(project_id: int):
    """Progress, trend, per-version and review aggregates for one project.

    Served as a single bundle rather than four endpoints so the page cannot
    render a progress ring and a review funnel computed seconds apart.

    Gated on ``project.view``: this is a read-only summary of data the member
    can already see row by row, so requiring a stronger capability would only
    push people back to counting cells by hand.
    """
    from ...services.lanmatrix import dashboard_service

    project, role = _project_and_role(project_id, "project.view")
    data = dashboard_service.snapshot(project)
    # The review-policy panel reuses this payload. Tell it up front whether this
    # user may change the policy: without the flag a reader is shown live
    # checkboxes that only fail on save, which reads as a broken page rather
    # than as a permission boundary.
    data["can_edit_policy"] = permissions.can(
        "project.edit", role, is_system_admin=g.user.is_system_admin)
    return ok(data)

