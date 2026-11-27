"""Freshness checks for database-backed Silver workbook downloads."""

from __future__ import annotations

from ..extensions import db
from ..models import CollabDoc, CollabSyncMark, TestBook
from .writeback import lock_project


FENCE_KEY = "book_export_fences"


def export_block_reason(project_id: int, book_id: int | None, fence: str | None) -> str | None:
    """Lock project rows until the caller finishes reading the workbook data."""
    lock_project(project_id)
    updates = (db.session.query(CollabDoc.update)
               .filter_by(project_id=project_id)
               .order_by(CollabDoc.seq).all())
    if not updates:
        return "pending" if fence else None

    from pycrdt import Doc, Map
    from . import doc_model

    doc = Doc()
    for (update,) in updates:
        doc.apply_update(bytes(update))

    # Ingress is durable before YRoom applies a browser update. A receipt in
    # the same socket stream also covers a caller edit that was still in
    # transit when its HTTP request reached Flask.
    if fence and doc.get(FENCE_KEY, type=Map).get(fence) is not True:
        return "pending"
    mark = db.session.get(CollabSyncMark, project_id)
    if mark is None or mark.content_hash != doc_model.content_hash(doc):
        return "pending"

    errors = doc.get(doc_model.ERRORS_KEY, type=Map)
    if errors:
        book_ids = ({book_id} if book_id is not None else
                    {book.id for book in TestBook.query.filter_by(project_id=project_id)})
        if any(row.get("book_id") in book_ids and row.get("uuid") in errors
               for row in doc_model.snapshot_sheet(doc, "test")):
            return "invalid"
    return None
