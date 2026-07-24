"""System-admin PostgreSQL introspection, SQL console and table CRUD for the LAN Test Matrix API."""

from __future__ import annotations

import datetime as _dt
import io
import json
import secrets
import zipfile
from pathlib import Path

from flask import (
    Blueprint, Response, current_app, g, request, send_file, session,
    stream_with_context,
)

from ...extensions import db
from ...models import DataJob, FieldDefinition, LMUser, Project, Task, TaskStatus
from ...services import (
    event_service, license_service, model_service, report_service,
    task_service, upload_service,
)
from ...services.upload_service import UploadError
from ...services.lanmatrix import (
    dbadmin, excel_service, permissions, service, settings,
)
from ._base import (
    ok, err, current_user, login_required, bootstrap_admin_required,
    register_common, _project_and_role, _client_ip,
    _LOCK_THRESHOLD, _LOCK_MINUTES,
)

bp = Blueprint("lanmatrix_admin_db", __name__, url_prefix="/api/v1")
register_common(bp)

@bp.get("/admin/db/overview")
@bootstrap_admin_required
def admin_db_overview():
    try:
        return ok(dbadmin.overview())
    except dbadmin.DbAdminError as ex:
        return err("DB_ERROR", str(ex), status=400)

@bp.post("/admin/db/query")
@bootstrap_admin_required
def admin_db_query():
    body = request.get_json(silent=True) or {}
    sql = (body.get("sql") or "").strip()
    if not sql:
        return err("VALIDATION_ERROR", "请输入 SQL 语句", status=400)
    read_only = bool(body.get("read_only", True))
    try:
        result = dbadmin.run_sql(sql, read_only=read_only)
    except dbadmin.DbAdminError as ex:
        return err("SQL_ERROR", str(ex), status=400)
    return ok(result)

@bp.get("/admin/db/tables")
@bootstrap_admin_required
def admin_db_tables():
    try:
        return ok({"tables": dbadmin.list_manageable_tables()})
    except dbadmin.DbAdminError as ex:
        return err("DB_ERROR", str(ex), status=400)

@bp.get("/admin/db/tables/<table>/schema")
@bootstrap_admin_required
def admin_db_table_schema(table):
    try:
        return ok(dbadmin.table_schema(table))
    except dbadmin.DbAdminError as ex:
        return err("DB_ERROR", str(ex), status=404)

@bp.get("/admin/db/tables/<table>/rows")
@bootstrap_admin_required
def admin_db_table_rows(table):
    try:
        result = dbadmin.read_rows(
            table,
            page=int(request.args.get("page", 1)),
            page_size=int(request.args.get("page_size", 50)),
            order_by=request.args.get("order_by") or None,
            descending=request.args.get("desc") in ("1", "true", "True"),
        )
    except dbadmin.DbAdminError as ex:
        return err("DB_ERROR", str(ex), status=400)
    return ok(result)

@bp.post("/admin/db/tables/<table>/rows")
@bootstrap_admin_required
def admin_db_insert_row(table):
    body = request.get_json(silent=True) or {}
    values = body.get("values", {})
    if not isinstance(values, dict):
        return err("VALIDATION_ERROR", "values 必须是对象", status=400)
    try:
        row = dbadmin.insert_row(table, values)
    except dbadmin.DbAdminError as ex:
        return err("SQL_ERROR", str(ex), status=400)
    return ok({"row": row}, status=201)

@bp.patch("/admin/db/tables/<table>/rows")
@bootstrap_admin_required
def admin_db_update_row(table):
    body = request.get_json(silent=True) or {}
    pk = body.get("pk", {})
    changes = body.get("changes", {})
    if not isinstance(pk, dict) or not isinstance(changes, dict):
        return err("VALIDATION_ERROR", "pk 与 changes 必须是对象", status=400)
    try:
        row = dbadmin.update_row(table, pk, changes)
    except dbadmin.DbAdminError as ex:
        return err("SQL_ERROR", str(ex), status=400)
    return ok({"row": row})

@bp.delete("/admin/db/tables/<table>/rows")
@bootstrap_admin_required
def admin_db_delete_row(table):
    body = request.get_json(silent=True) or {}
    pk = body.get("pk", {})
    if not isinstance(pk, dict):
        return err("VALIDATION_ERROR", "pk 必须是对象", status=400)
    try:
        deleted = dbadmin.delete_row(table, pk)
    except dbadmin.DbAdminError as ex:
        return err("SQL_ERROR", str(ex), status=400)
    return ok({"deleted": deleted})
