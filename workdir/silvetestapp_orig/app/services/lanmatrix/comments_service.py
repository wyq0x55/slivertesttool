"""Cell-comment and audit-log query service (LAN Test Matrix)."""
from __future__ import annotations

from typing import Optional

from ...extensions import db
from ...models import AuditLog, CellComment, LMUser, Project, TestItemRow
from . import audit, settings


# --------------------------------------------------------------------------- #
# Comments (FR-GRID-006)
# --------------------------------------------------------------------------- #
def add_comment(user: LMUser, project: Project, item: TestItemRow,
                field_key: str, content: str) -> CellComment:
    c = CellComment(project_id=project.id, test_item_id=item.id,
                    field_key=field_key, content=content, created_by=user.id)
    db.session.add(c)
    audit.record("comment.add", actor_id=user.id, object_type="comment",
                 object_id=item.id, project_id=project.id,
                 new_value={"field_key": field_key})
    db.session.commit()
    return c


def list_comments(project_id: int, item_id: int) -> list[CellComment]:
    return CellComment.query.filter_by(
        project_id=project_id, test_item_id=item_id, deleted_at=None
    ).order_by(CellComment.created_at).all()


# --------------------------------------------------------------------------- #
# Audit
# --------------------------------------------------------------------------- #
def list_audit(project_id: int, *, page: int = 1,
               page_size: Optional[int] = None) -> dict:
    if page_size is None:
        page_size = settings.PAGE_SIZE
    q = AuditLog.query.filter_by(project_id=project_id).order_by(AuditLog.created_at.desc())
    total = q.count()
    page = max(1, page)
    page_size = min(max(1, page_size), settings.PAGE_SIZE_MAX)
    logs = q.offset((page - 1) * page_size).limit(page_size).all()
    return {"items": [l.to_dict() for l in logs], "page": page,
            "page_size": page_size, "total": total}
