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
    _attach_diffs(items)
    # `filtered` tells the UI whether an empty page means "nothing happened" or
    # "your filters excluded everything" -- two very different conclusions for
    # someone reviewing an incident.
    return {"items": items, "page": page,
            "page_size": page_size, "total": total,
            "filtered": is_filtered(crit)}


# --------------------------------------------------------------------------- #
# Field-level diff
# --------------------------------------------------------------------------- #
# `old_value` / `new_value` are whole-object snapshots (see item.update, which
# stores `old` and `item.to_dict()`). Rendering them as two blobs of truncated
# JSON side by side means the one thing a reviewer actually needs -- *which
# field changed, from what, to what* -- is the one thing the log will not tell
# them. This computes that per field, once, on the server, so the table view
# and the CSV export cannot disagree about what an entry means.

# Snapshot bookkeeping that is noise in a diff: it changes on every write and
# never explains anything.
_DIFF_IGNORE = frozenset({
    "updated_at", "updated_by", "version", "row_order", "id", "uuid",
})

# Labels for the fixed core columns. Project-defined columns keep their own key
# -- inventing a translation for a user-named field would be worse than showing
# the key the user chose themselves.
_FIELD_LABELS = {
    "case_id": "用例编号", "title": "标题", "sheet": "工作表",
    "status": "状态", "priority": "优先级", "owner": "负责人",
    "role": "角色", "name": "名称", "code": "项目代号",
    "description": "描述", "field_key": "字段", "label": "显示名",
    "field_type": "字段类型", "required": "必填", "options": "可选值",
    "is_locked": "已锁定", "deleted": "已删除", "direction": "方向",
    "moved": "移动的行",
}

_MAX_DIFF_FIELDS = 40


def field_label(key: str) -> str:
    """Display name for a diff row. Falls back to the raw key."""
    return _FIELD_LABELS.get(key, key)


def format_value(v) -> str:
    """Render a JSON value for a diff cell.

    Booleans and None get words rather than Python/JSON spellings: a reviewer
    reading "None → True" has to know what produced it, while "（空）→ 是" is
    readable by the QA engineer this log exists for.
    """
    if v is None:
        return ""
    if isinstance(v, bool):
        return "是" if v else "否"
    if isinstance(v, str):
        return v
    if isinstance(v, (int, float)):
        return str(v)
    if isinstance(v, (list, tuple)):
        return "、".join(format_value(x) for x in v)
    if isinstance(v, dict):
        return "; ".join("%s=%s" % (k, format_value(v[k])) for k in sorted(v))
    return str(v)


def diff_values(old, new, *, limit: int = _MAX_DIFF_FIELDS) -> list[dict]:
    """Per-field changes between two audit snapshots.

    Returns ``[{"field", "label", "old", "new", "kind"}]`` where *kind* is
    ``added`` / ``removed`` / ``changed``. Creates (no ``old``) and deletes (no
    ``new``) are reported as the fields that appeared or disappeared, so the
    same renderer covers every action rather than special-casing three of them.

    Pure: no database, no request context, so the rules are directly testable
    in an environment that cannot start PostgreSQL.
    """
    if not isinstance(old, dict) and not isinstance(new, dict):
        # Scalars, lists, or nothing at all: there is no field structure to
        # diff. Report one pseudo-row rather than silently rendering blank.
        if old is None and new is None:
            return []
        return [{"field": "", "label": "值", "old": format_value(old),
                 "new": format_value(new), "kind": "changed"}]
    o = old if isinstance(old, dict) else {}
    n = new if isinstance(new, dict) else {}
    out = []
    for key in sorted(set(o) | set(n)):
        if key in _DIFF_IGNORE:
            continue
        had, has = key in o, key in n
        ov, nv = o.get(key), n.get(key)
        if had and has:
            if ov == nv:
                continue
            kind = "changed"
        elif has:
            kind = "added"
        else:
            kind = "removed"
        out.append({"field": key, "label": field_label(key),
                    "old": format_value(ov) if had else "",
                    "new": format_value(nv) if has else "",
                    "kind": kind})
    # A runaway snapshot (a 200-column project) must not turn one log entry
    # into a page of its own. Truncation is reported, never silent.
    if limit and len(out) > limit:
        hidden = len(out) - limit
        out = out[:limit]
        out.append({"field": "", "label": "…", "old": "",
                    "new": "另有 %d 个字段变更未显示" % hidden,
                    "kind": "truncated"})
    return out


def _attach_diffs(items: list) -> None:
    for i in items:
        i["changes"] = diff_values(i.get("old_value"), i.get("new_value"))


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


# --------------------------------------------------------------------------- #
# CSV export
# --------------------------------------------------------------------------- #
AUDIT_CSV_HEADER = ["时间", "操作", "结果", "对象类型", "对象", "操作人",
                    "字段", "原值", "新值", "来源 IP", "错误"]

# An export that silently stops is worse than one that refuses: the reviewer
# would draw conclusions from a truncated trail believing it complete.
AUDIT_CSV_MAX_ROWS = 50000


def csv_cell(value) -> str:
    """Make one value safe to write into a CSV that Excel will open.

    Excel treats a leading ``= + - @`` (or tab/CR, which it strips first) as the
    start of a formula, so an audit value such as ``=cmd|'/c calc'!A1`` -- which
    an attacker can plant simply by typing it into a test-case title -- executes
    on the reviewer's machine. Prefixing with an apostrophe keeps the text
    visible while disarming it. This matters more here than almost anywhere
    else: the audit log deliberately records hostile input verbatim.
    """
    s = "" if value is None else str(value)
    if s and s.lstrip("\t\r\n ")[:1] in ("=", "+", "-", "@"):
        return "'" + s
    return s


def audit_csv_rows(project_id: int, *, max_rows: int = AUDIT_CSV_MAX_ROWS,
                   **filters):
    """Yield audit rows (header first) as lists of strings, one per changed
    field so the export is as readable as the on-screen diff.

    Streamed in batches: an audit log is unbounded by design and materialising
    a whole project's history to build a download would take the server down
    with it.
    """
    yield AUDIT_CSV_HEADER
    crit = audit_criteria(project_id, **filters)
    query = (AuditLog.query.filter(*crit)
             .order_by(AuditLog.created_at.desc(), AuditLog.id.desc()))
    emitted = 0
    offset = 0
    batch = 500
    capped = False
    while emitted < max_rows:
        logs = query.offset(offset).limit(batch).all()
        if not logs:
            return
        offset += len(logs)
        items = [l.to_dict() for l in logs]
        _attach_actor_names(items)
        _attach_diffs(items)
        for a in items:
            actor = a.get("actor_name") or a.get("actor_id") or ""
            head = [
                (a.get("created_at") or "").replace("T", " ").split(".")[0],
                a.get("action") or "", a.get("result") or "",
                a.get("object_type") or "", a.get("object_id") or "",
                actor,
            ]
            tail = [a.get("client_ip") or "", a.get("error_summary") or ""]
            changes = a.get("changes") or []
            if not changes:
                # Still emit the entry: an action with no field delta (a login,
                # a run trigger) is exactly the kind of thing a reviewer looks
                # for, and dropping it would make the export disagree with the
                # totals shown on screen.
                rows = [head + ["", "", ""] + tail]
            else:
                rows = [head + [c["label"], c["old"], c["new"]] + tail
                        for c in changes]
            for r in rows:
                if emitted >= max_rows:
                    capped = True
                    break
                yield [csv_cell(x) for x in r]
                emitted += 1
            if capped:
                break
        if capped or len(logs) < batch:
            break
    if capped or emitted >= max_rows:
        # Reaching the cap is reported inside the file itself, so a truncated
        # export can never be mistaken for a complete one.
        yield ["导出已达到上限 %d 行，请缩小时间范围或添加筛选条件后重新导出"
               % max_rows] + [""] * (len(AUDIT_CSV_HEADER) - 1)


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
