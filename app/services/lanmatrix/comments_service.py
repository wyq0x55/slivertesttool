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
# An audit trail you cannot query by *who* and *when* is not an audit trail --
# it is a scrolling wall. These are the filters the log is actually asked for
# during an incident review.
AUDIT_RESULTS = ("success", "failure")


def is_filtered(crit: list) -> bool:
    """Whether *crit* carries any filter beyond the mandatory project scope.

    A one-line helper only because it must be unit-testable: the UI uses this
    to decide between "暂无日志" and "筛选条件排除了全部记录", and getting it
    wrong makes a reviewer conclude nothing happened when something did.
    """
    return len(crit) > 1


def audit_criteria(project_id: int, *, actor_id=None, action=None,
                   object_type=None, result=None, date_from=None,
                   date_to=None, q=None) -> list:
    """Build the SQLAlchemy filter criteria for an audit query.

    Split out from :func:`list_audit` so the filter logic is testable without a
    database: SQLAlchemy builds these expressions from the declarative model
    alone, no connection required. Given this project cannot run PostgreSQL in
    CI, a pure-function seam is the difference between the filters being tested
    and merely being written.
    """
    crit = [AuditLog.project_id == project_id]
    if actor_id is not None:
        crit.append(AuditLog.actor_id == actor_id)
    if action:
        crit.append(AuditLog.action == action)
    if object_type:
        crit.append(AuditLog.object_type == object_type)
    if result:
        crit.append(AuditLog.result == result)
    if date_from is not None:
        crit.append(AuditLog.created_at >= date_from)
    if date_to is not None:
        crit.append(AuditLog.created_at <= date_to)
    if q:
        # Escape the LIKE wildcards, or a search for "100%" silently matches
        # every row beginning with "100".
        safe = q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        like = "%" + safe + "%"
        crit.append(db.or_(AuditLog.object_id.ilike(like, escape="\\"),
                           AuditLog.action.ilike(like, escape="\\"),
                           AuditLog.error_summary.ilike(like, escape="\\")))
    return crit


def list_audit(project_id: int, *, page: int = 1,
               page_size: Optional[int] = None, actor_id=None, action=None,
               object_type=None, result=None, date_from=None, date_to=None,
               q=None) -> dict:
    if page_size is None:
        page_size = settings.PAGE_SIZE
    crit = audit_criteria(project_id, actor_id=actor_id, action=action,
                          object_type=object_type, result=result,
                          date_from=date_from, date_to=date_to, q=q)
    query = AuditLog.query.filter(*crit).order_by(AuditLog.created_at.desc())
    total = query.count()
    page = max(1, page)
    page_size = min(max(1, page_size), settings.PAGE_SIZE_MAX)
    logs = query.offset((page - 1) * page_size).limit(page_size).all()
    items = [l.to_dict() for l in logs]
    _attach_actor_names(items)
    # `filtered` tells the UI whether an empty page means "nothing happened" or
    # "your filters excluded everything" -- two very different conclusions for
    # someone reviewing an incident.
    return {"items": items, "page": page,
            "page_size": page_size, "total": total,
            "filtered": is_filtered(crit)}


def _attach_actor_names(items: list) -> None:
    """Resolve ``actor_id`` to a display name, in one query for the whole page.

    ``AuditLog.actor_id`` is a bare integer with no relationship, so without
    this the UI can only render "操作人 3" -- an audit trail nobody can read.
    Batched deliberately: a per-row lookup would be N+1 on every page load.
    """
    ids = {i.get("actor_id") for i in items if i.get("actor_id")}
    if not ids:
        return
    rows = LMUser.query.filter(LMUser.id.in_(ids)).all()
    names = {u.id: (u.display_name or u.username) for u in rows}
    for i in items:
        # Fall back to the raw id: a deleted user must not blank the trail.
        i["actor_name"] = names.get(i.get("actor_id"))


def audit_actors(project_id: int) -> list[dict]:
    """Actors who appear in this project's audit log, for the filter dropdown."""
    rows = (db.session.query(AuditLog.actor_id)
            .filter(AuditLog.project_id == project_id,
                    AuditLog.actor_id.isnot(None))
            .distinct().all())
    ids = [r[0] for r in rows]
    if not ids:
        return []
    users = LMUser.query.filter(LMUser.id.in_(ids)).all()
    known = {u.id: (u.display_name or u.username) for u in users}
    out = [{"id": i, "name": known.get(i) or ("用户 %d" % i)} for i in ids]
    return sorted(out, key=lambda a: a["name"])


def audit_actions(project_id: int) -> list[str]:
    """Distinct action names present for this project, for the filter dropdown.

    Populated from the data rather than a hardcoded list so a newly recorded
    action type cannot become unfilterable.
    """
    rows = (db.session.query(AuditLog.action)
            .filter(AuditLog.project_id == project_id)
            .distinct().order_by(AuditLog.action).all())
    return [r[0] for r in rows if r[0]]
