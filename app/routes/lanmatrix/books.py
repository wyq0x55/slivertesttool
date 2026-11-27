"""LAN Matrix route ownership: specification books (multi-book projects)."""

from __future__ import annotations

import datetime as _dt

from flask import g, request, send_file

from ...services import project_model_service
from ...services.lanmatrix import books_service, import_job_service, permissions
from ...services.lanmatrix.errors import ServiceError
from ._base import err, login_required, ok, _project_and_role
from .projects_items import bp
from .projects import _collab_bulk_write_blocked


def _books_project(project_id: int, capability: str):
    project, role = _project_and_role(project_id, capability)
    if project.project_type != "silver_books":
        raise ServiceError(
            "式样书管理仅适用于「Silver 多式样书」项目", code="VALIDATION_ERROR",
        )
    return project, role


@bp.get("/projects/<int:project_id>/books")
@login_required
def list_books(project_id):
    project, role = _project_and_role(project_id, "project.view")
    books = (books_service.list_books(project.id)
             if project.project_type == "silver_books" else [])
    return ok({"books": books,
               "project_type": project.project_type,
               "can_edit": permissions.can(
                   "project.edit", role,
                   is_system_admin=bool(g.user.is_system_admin))})


@bp.post("/projects/<int:project_id>/books")
@login_required
def create_book(project_id):
    project, _ = _books_project(project_id, "project.edit")
    if not project.is_editable:
        return err("PROJECT_LOCKED", "项目当前不可编辑", status=409)
    body = request.get_json(silent=True) or {}
    book = books_service.create_book(
        g.user, project,
        name=body.get("name") or "",
        source_filename=body.get("source_filename") or "",
        model_id=body.get("model_id"),
        commit=True,
    )
    return ok({"book": book.to_dict(), "books": books_service.list_books(project.id)},
              status=201)


@bp.patch("/projects/<int:project_id>/books/<int:book_id>")
@login_required
def update_book(project_id, book_id):
    project, _ = _books_project(project_id, "project.edit")
    if not project.is_editable:
        return err("PROJECT_LOCKED", "项目当前不可编辑", status=409)
    book = books_service.get_book(project.id, book_id)
    body = request.get_json(silent=True) or {}
    changes = {key: body[key] for key in ("name", "model_id", "source_filename")
               if key in body}
    if not changes:
        return err("VALIDATION_ERROR", "没有要修改的内容", status=400)
    book = books_service.update_book(g.user, project, book, changes, commit=True)
    return ok({"book": book.to_dict(), "books": books_service.list_books(project.id)})


@bp.delete("/projects/<int:project_id>/books/<int:book_id>")
@login_required
def delete_book(project_id, book_id):
    project, _ = _books_project(project_id, "project.edit")
    if not project.is_editable:
        return err("PROJECT_LOCKED", "项目当前不可编辑", status=409)
    book = books_service.get_book(project.id, book_id)
    books_service.delete_book(g.user, project, book, commit=True)
    return ok({"deleted": True, "books": books_service.list_books(project.id)})


@bp.post("/projects/<int:project_id>/books/import")
@login_required
def import_book(project_id):
    project, _ = _books_project(project_id, "import.run")
    if not project.is_editable:
        return err("PROJECT_LOCKED", "项目当前不可编辑", status=409)
    file = request.files.get("file")
    if file is None or not file.filename:
        return err("VALIDATION_ERROR", "未上传文件", status=400)
    if not file.filename.lower().endswith(".xlsx"):
        return err("VALIDATION_ERROR", "仅支持 .xlsx 文件", status=400)
    book_id = request.form.get("book_id") or None
    model_id = request.form.get("model_id") or None
    job = import_job_service.create_special_preview(
        g.user, project, file.stream,
        import_format="book", mode="upsert",
        original_filename=file.filename,
        book_id=book_id, model_id=model_id,
        book_name=request.form.get("book_name") or "",
    )
    return ok({"job": job.to_dict(with_preview=True)}, status=201)


@bp.get("/projects/<int:project_id>/books/<int:book_id>/export")
@login_required
def export_book(project_id, book_id):
    project, _ = _books_project(project_id, "export.run")
    book = books_service.get_book(project.id, book_id)
    buffer = books_service.export_book(project, book)
    filename = books_service._safe_filename(
        book.source_filename or f"{book.name}.xlsx", f"book_{book.id}.xlsx")
    if not filename.lower().endswith(".xlsx"):
        filename += ".xlsx"
    return send_file(
        buffer, as_attachment=True, download_name=filename,
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )


@bp.get("/projects/<int:project_id>/books/export.zip")
@login_required
def export_all_books(project_id):
    project, _ = _books_project(project_id, "export.run")
    buffer = books_service.export_all_books(project)
    ts = _dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    return send_file(
        buffer, as_attachment=True,
        download_name=f"{project.code}_books_{ts}.zip",
        mimetype="application/zip",
    )
