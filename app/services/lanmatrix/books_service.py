"""Specification-book registry for Silver multi-book projects."""

from __future__ import annotations

import io
import json
import os
import re
import shutil
import zipfile
from pathlib import Path
from typing import Any, Optional

from flask import current_app

from ...extensions import db
from ...models import Project, ProjectModel, Task, TestBook, TestItemRow
from ...services import project_model_service
from . import audit, book_excel
from .errors import ServiceError


def _clean_name(value: str) -> str:
    name = (value or "").strip()
    if not name:
        raise ServiceError("式样书名称不能为空", code="VALIDATION_ERROR")
    if len(name) > 120:
        raise ServiceError("式样书名称不能超过 120 个字符", code="VALIDATION_ERROR")
    return name


def _model_for(project_id: int, model_id: Any) -> ProjectModel:
    try:
        wanted = int(model_id)
    except (TypeError, ValueError):
        raise ServiceError("请选择式样书绑定的模型", code="VALIDATION_ERROR")
    row = ProjectModel.query.filter_by(id=wanted, project_id=project_id).first()
    if row is None:
        raise ServiceError("模型不属于当前项目", code="VALIDATION_ERROR")
    return row


def validate_model(project_id: int, model_id: Any) -> ProjectModel:
    """Validate a model chosen for a book before a preview is created."""
    return _model_for(project_id, model_id)


def ensure_book_fields(user, project: Project) -> int:
    """Provision the multi-book sheet vocabulary once per project.

    The ``test`` sheet uses the 23-column book layout; ``lib`` / ``const`` /
    ``io`` keep their project-wide pools. Field definitions are project-scoped
    and idempotent, so calling this on every book creation is safe.
    """
    from . import fields as fld, fields_service

    created = fields_service.ensure_fields(user, project, fld.BOOK_TEST_FIELDS)
    created += fields_service.ensure_fields(user, project, fld.LIB_FIELDS)
    created += fields_service.ensure_fields(user, project, fld.CONST_FIELDS)
    created += fields_service.ensure_fields(user, project, fld.IO_FIELDS)
    return created


def list_books(project_id: int) -> list[dict[str, Any]]:
    books = (TestBook.query.filter_by(project_id=project_id)
             .order_by(TestBook.sort_order.asc(), TestBook.id.asc()).all())
    if not books:
        return []
    counts = {
        (book_id, sheet): count
        for book_id, sheet, count in (
            db.session.query(
                TestItemRow.book_id, TestItemRow.sheet, db.func.count(TestItemRow.id),
            )
            .filter(
                TestItemRow.project_id == project_id,
                TestItemRow.book_id.isnot(None),
                TestItemRow.deleted_at.is_(None),
            )
            .group_by(TestItemRow.book_id, TestItemRow.sheet).all()
        )
    }
    models = {
        row.id: row for row in ProjectModel.query.filter(
            ProjectModel.project_id == project_id,
            ProjectModel.id.in_([book.model_id for book in books if book.model_id]),
        ).all()
    }
    out = []
    for book in books:
        entry = book.to_dict()
        model = models.get(book.model_id)
        entry["model"] = model.to_dict() if model is not None else None
        entry["test_count"] = int(counts.get((book.id, "test"), 0))
        entry["io_count"] = int(counts.get((book.id, "io"), 0))
        out.append(entry)
    return out


def get_book(project_id: int, book_id: Any) -> TestBook:
    try:
        wanted = int(book_id)
    except (TypeError, ValueError):
        raise ServiceError("式样书不存在", code="NOT_FOUND")
    book = TestBook.query.filter_by(id=wanted, project_id=project_id).first()
    if book is None:
        raise ServiceError("式样书不存在", code="NOT_FOUND")
    return book


def require_model(project_id: int, book: TestBook) -> tuple[int, str, str, Any]:
    if not book.model_id:
        raise ServiceError(
            f"式样书「{book.name}」尚未绑定模型，请先在式样书管理中绑定。",
            code="NO_MODEL",
        )
    try:
        return project_model_service.resolve_id(project_id, book.model_id)
    except project_model_service.ModelError as exc:
        raise ServiceError(str(exc), code="NO_MODEL") from exc


def create_book(user, project: Project, *, name: str, source_filename: str,
                model_id: Any, commit: bool = False) -> TestBook:
    name = _clean_name(name)
    existing = TestBook.query.filter_by(project_id=project.id, name=name).first()
    if existing is not None:
        raise ServiceError(f"式样书「{name}」已存在", code="DUPLICATE")
    model = _model_for(project.id, model_id)
    sort_order = (db.session.query(db.func.max(TestBook.sort_order))
                  .filter_by(project_id=project.id).scalar() or 0) + 1
    book = TestBook(
        project_id=project.id,
        name=name,
        source_filename=(source_filename or "").strip()[:255],
        model_id=model.id,
        sort_order=sort_order,
        created_by=getattr(user, "id", None),
    )
    db.session.add(book)
    db.session.flush()
    ensure_book_fields(user, project)
    audit.record(
        "book.create", actor_id=getattr(user, "id", None),
        object_type="book", object_id=book.id, project_id=project.id,
        new_value={"name": book.name, "model_id": book.model_id},
    )
    if commit:
        db.session.commit()
    return book


def delete_book(user, project: Project, book: TestBook, *,
                commit: bool = True) -> None:
    """Delete an empty book.

    Rows are never silently thrown away with their book: a book that still has
    live rows must be emptied through the editor (so the recycle bin keeps the
    deletion reversible) before it can be removed.
    """
    remaining = TestItemRow.query.filter_by(
        project_id=project.id, book_id=book.id).count()
    if remaining:
        raise ServiceError(
            f"式样书「{book.name}」仍有 {remaining} 行数据（含回收站），请先彻底清理后再删除式样书。",
            code="BOOK_NOT_EMPTY",
        )
    task_count = Task.query.filter_by(
        project_id=project.id, book_id=book.id).count()
    if task_count:
        raise ServiceError(
            f"式样书「{book.name}」仍有 {task_count} 个测试任务（含回收站），"
            "请先清理任务后再删除式样书。",
            code="BOOK_HAS_TASKS",
        )
    old = book.to_dict()
    source_path = book_source_path(project, book)
    db.session.delete(book)
    audit.record(
        "book.delete", actor_id=getattr(user, "id", None),
        object_type="book", object_id=book.id, project_id=project.id,
        old_value=old,
    )
    if commit:
        db.session.commit()
        discard_book_source(source_path)


def update_book(user, project: Project, book: TestBook,
                changes: dict[str, Any], *, commit: bool = True) -> TestBook:
    old = book.to_dict()
    if "name" in changes:
        name = _clean_name(changes.get("name"))
        clash = TestBook.query.filter(
            TestBook.project_id == project.id,
            TestBook.name == name,
            TestBook.id != book.id,
        ).first()
        if clash is not None:
            raise ServiceError(f"式样书「{name}」已存在", code="DUPLICATE")
        book.name = name
    if "model_id" in changes:
        model = _model_for(project.id, changes.get("model_id"))
        book.model_id = model.id
    if "source_filename" in changes:
        book.source_filename = str(changes.get("source_filename") or "")[:255]
    db.session.flush()
    audit.record(
        "book.update", actor_id=getattr(user, "id", None),
        object_type="book", object_id=book.id, project_id=project.id,
        old_value=old, new_value=book.to_dict(),
    )
    if commit:
        db.session.commit()
    return book


def book_items(project_id: int, book_id: int) -> list[TestItemRow]:
    return (TestItemRow.query
            .filter_by(project_id=project_id, book_id=book_id, sheet="test",
                       deleted_at=None)
            .order_by(TestItemRow.row_order.asc(), TestItemRow.id.asc())
            .all())


def _steps(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, dict) else {}
        except (TypeError, ValueError):
            return {}
    return {}


def _source_root(project: Project) -> Path:
    instance_dir = Path(current_app.config_obj.INSTANCE_DIR)
    return instance_dir / "books" / f"project_{project.id}"


def project_book_root(project: Project) -> Path:
    """Directory holding every preserved original workbook for one project."""
    return _source_root(project)


def book_source_path(project: Project, book: TestBook) -> Path:
    """The preserved original workbook for one specification book."""
    return _source_root(project) / f"book_{book.id}.xlsx"


def stage_book_source(project: Project, source) -> str:
    """Persist an uploaded workbook until its import preview is committed."""
    if hasattr(source, "seek"):
        source.seek(0)
    pending = _source_root(project) / "pending"
    pending.mkdir(parents=True, exist_ok=True)
    path = pending / f"{os.urandom(12).hex()}.xlsx"
    with path.open("wb") as handle:
        shutil.copyfileobj(source, handle)
    return str(path)


def discard_book_source(path_value: Any) -> None:
    if not path_value:
        return
    try:
        Path(path_value).unlink(missing_ok=True)
    except OSError:
        pass


def promote_book_source(project: Project, book: TestBook,
                        staged_path: Any) -> Optional[dict[str, str]]:
    """Atomically replace a book's preserved original with the staged upload."""
    if not staged_path:
        return None
    staged = Path(str(staged_path))
    if not staged.is_file():
        return None
    final = book_source_path(project, book)
    final.parent.mkdir(parents=True, exist_ok=True)
    backup = None
    if final.exists():
        backup = final.with_name(f"{final.name}.bak.{os.urandom(8).hex()}")
        os.replace(final, backup)
    try:
        os.replace(staged, final)
    except Exception:
        if backup is not None and backup.exists():
            os.replace(backup, final)
        raise
    return {
        "final": str(final),
        "backup": str(backup) if backup is not None else "",
    }


def finalize_source_promotion(promotion: Optional[dict[str, str]]) -> None:
    if not promotion:
        return
    backup = promotion.get("backup")
    if backup:
        discard_book_source(backup)


def rollback_source_promotion(promotion: Optional[dict[str, str]]) -> None:
    if not promotion:
        return
    final = Path(promotion["final"])
    backup = promotion.get("backup")
    discard_book_source(final)
    if backup and Path(backup).is_file():
        os.replace(backup, final)


def export_book(project: Project, book: TestBook) -> io.BytesIO:
    items = []
    for row in book_items(project.id, book.id):
        item = row.to_dict()
        item["steps"] = _steps(item.get("steps"))
        items.append(item)
    source = book_source_path(project, book)
    wb = (book_excel.rebuild_workbook(source, items)
          if source.is_file() else book_excel.build_workbook(items))
    return book_excel.workbook_bytes(wb)


def _safe_filename(name: str, fallback: str) -> str:
    token = re.sub(r'[<>:"/\\|?*\x00-\x1f]+', "_", (name or "").strip())
    token = token.strip(" .")
    return token or fallback


def export_all_books(project: Project) -> io.BytesIO:
    books = (TestBook.query.filter_by(project_id=project.id)
             .order_by(TestBook.sort_order.asc(), TestBook.id.asc()).all())
    handle = io.BytesIO()
    used: set[str] = set()
    with zipfile.ZipFile(handle, "w", zipfile.ZIP_DEFLATED) as archive:
        for book in books:
            source = book.source_filename or f"{book.name}.xlsx"
            if not source.lower().endswith(".xlsx"):
                source += ".xlsx"
            filename = _safe_filename(source, f"book_{book.id}.xlsx")
            if filename in used:
                filename = f"{book.id}_{filename}"
            used.add(filename)
            archive.writestr(filename, export_book(project, book).getvalue())
    handle.seek(0)
    return handle
