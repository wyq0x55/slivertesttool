"""In-app notifications: delivery, collapsing, reading and retention.

Scope
-----
Two events genuinely need to reach somebody who is not currently looking at the
page: *"the run you submitted finished"* and *"you have been asked to review
this"*. Without the second one a reviewer only ever discovers assigned work by
chance, which makes the whole review feature unreliable.

This is deliberately **in-app only** -- no email, no WebSocket push. On a LAN
tool a bell with an unread count and a 30-second poll is the entire requirement,
and it costs no SMTP configuration, no delivery failures and no extra process.
The API is shaped so a push transport could be added later without callers
changing.

One event, one row
------------------
Collapsing used to merge every "run finished" of a project into a single row
carrying ``×50``. It made the bell short, and useless: a merged row can only
carry *one* ``link_url``, so clicking it navigated to one arbitrary member and
the other events behind the count were unreachable -- the user could see that
something else had happened but had no way to find out what. A notification you
cannot open is not a notification.

So the default ``group_key`` is now per *referenced object*
(``type:project:ref_id``) and the collapsing window defaults to 0
(``LM_NOTIFY_GROUP_SECONDS``): distinct events always get their own row with
their own link. The merge path is kept -- with a non-zero window it still folds
*literally the same event delivered twice* (same ref) into one row -- so an
operator can trade detail for volume, but nothing merges by default.

Self-notification is suppressed: being told about the consequences of the click
you just made is noise, not information.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Iterable, Optional
from urllib.parse import quote

from flask import current_app

from ...extensions import db
from ...models import Notification

logger = logging.getLogger(__name__)

# Notification types.
TASK_FINISHED = "task.finished"
REVIEW_ASSIGNED = "review.assigned"
REVIEW_APPROVED = "review.approved"
REVIEW_REJECTED = "review.rejected"


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


# --------------------------------------------------------------------------- #
# Link builders
#
# Every page of this product is served under the ``/lanmatrix`` blueprint
# prefix, but notification call sites used to hand-write paths like
# ``/projects/<id>/tasks`` and ``/workspace/reviews``. The first is missing the
# prefix and the second is not a route at all, so both 404. A notification you
# cannot click through is worse than no notification: it reports that something
# needs attention and then refuses to show it.
#
# Centralised here so a new call site cannot reintroduce a hand-written path.
# --------------------------------------------------------------------------- #
#: URL prefix of the page blueprint (``lanmatrix_pages``).
PAGE_PREFIX = "/lanmatrix"


def project_link(project_id: Optional[int]) -> str:
    """The project's editor page (its matrix)."""
    if not project_id:
        return f"{PAGE_PREFIX}/projects"
    return f"{PAGE_PREFIX}/projects/{int(project_id)}"


def task_link(project_id: Optional[int], task_key: str = "") -> str:
    """The project's task list, deep-linked to one task when known.

    ``?task=`` is the query parameter the task page already restores on load,
    so the notification lands on the specific run rather than on a list the
    user then has to search.
    """
    if not project_id:
        return f"{PAGE_PREFIX}/home"
    base = f"{PAGE_PREFIX}/projects/{int(project_id)}/tasks"
    key = (task_key or "").strip()
    return f"{base}?task={quote(key, safe='')}" if key else base


def review_item_link(project_id: Optional[int], row_uuid: str = "") -> str:
    """The matrix page, deep-linked to the single row under review.

    An assigned-review notification is about one case, so it must open that
    case. Sending the reviewer to the queue instead makes them search for the
    row the notification already identified.
    """
    if not project_id:
        return review_queue_link(None)
    base = f"{PAGE_PREFIX}/projects/{int(project_id)}"
    key = (row_uuid or "").strip()
    return f"{base}?row={quote(key, safe='')}&from=workspace" if key else base


def review_queue_link(project_id: Optional[int] = None) -> str:
    """The cross-project review queue on the workspace page.

    The queue lives on ``/lanmatrix/home`` (the post-login workspace), not on a
    ``/workspace/reviews`` route -- that path has never existed.
    """
    base = f"{PAGE_PREFIX}/home?view=reviews"
    return f"{base}&project_id={int(project_id)}" if project_id else base


def exemption_queue_link(project_id: Optional[int] = None) -> str:
    """The 不要 (項目作成) sign-off queue on the workspace page.

    Deliberately the queue and not the row: unlike a verdict review -- where
    the reviewer must read the case to judge it -- a 不要 claim is decided from
    the list (区分 + case + who asked), and the 通过 / 驳回 buttons only exist
    here. Pointing at the matrix row would land the reviewer on a screen with
    no way to answer the notification that brought them there.
    """
    base = f"{PAGE_PREFIX}/home?view=exemptions"
    return f"{base}&project_id={int(project_id)}" if project_id else base


def _default_group_key(type: str, project_id: Optional[int],
                       ref_id: str = "") -> str:
    """``type:project:ref`` -- unique per referenced object.

    Callers that omit ``ref_id`` fall back to the old ``type:project`` shape,
    which still separates projects and event kinds.
    """
    ref = str(ref_id or "").strip()
    base = f"{type}:{project_id or 0}"
    return f"{base}:{ref}" if ref else base


def notify(user_id: Optional[int], type: str, title: str, *,
           body: str = "", project_id: Optional[int] = None,
           link_url: str = "", ref_type: str = "", ref_id: str = "",
           group_key: str = "", actor_id: Optional[int] = None,
           commit: bool = False) -> Optional[Notification]:
    """Deliver one notification, collapsing it into a recent twin if possible.

    Returns the created *or updated* row, or ``None`` when the notification was
    intentionally suppressed. Never raises: a notification problem must not roll
    back the business transaction that triggered it, so failures are logged and
    swallowed.

    ``commit`` is False by default so the notification joins the caller's
    transaction and cannot be delivered for something that then failed to save.
    """
    try:
        if not user_id or not type:
            return None
        # Don't tell people about their own actions.
        if actor_id is not None and int(actor_id) == int(user_id):
            return None

        # Default key identifies the referenced object, not just its project:
        # two different cases waiting for review are two different pieces of
        # news and must not share a row (and therefore a link).
        key = (group_key or _default_group_key(type, project_id, ref_id))[:120]
        window = int(current_app.config.get("LM_NOTIFY_GROUP_SECONDS", 0) or 0)

        existing = None
        if window > 0:
            since = _utcnow() - timedelta(seconds=window)
            existing = (Notification.query
                        .filter_by(user_id=user_id, group_key=key, is_read=False)
                        # An archived row is one the user explicitly filed away.
                        # Collapsing a new event into it would resurrect it as
                        # unread, i.e. undo a deliberate action.
                        .filter(Notification.archived_at.is_(None))
                        .filter(Notification.created_at >= since)
                        .order_by(Notification.id.desc())
                        .first())

        if existing is not None:
            existing.count = (existing.count or 1) + 1
            existing.title = title[:200]
            existing.body = body
            existing.updated_at = _utcnow()
            # Point at the collection rather than the newest single item: with a
            # count > 1 a link to one arbitrary member is misleading.
            if link_url:
                existing.link_url = link_url[:500]
            row = existing
        else:
            row = Notification(
                user_id=user_id, type=type, title=title[:200], body=body,
                project_id=project_id, link_url=link_url[:500],
                ref_type=ref_type[:32], ref_id=str(ref_id)[:64],
                group_key=key, count=1,
            )
            db.session.add(row)

        if commit:
            db.session.commit()
        return row
    except Exception:  # noqa: BLE001 - notifications must never break a flow
        logger.exception("failed to deliver notification (type=%s user=%s)",
                         type, user_id)
        return None


def notify_many(user_ids: Iterable[int], type: str, title: str, **kw) -> int:
    """Deliver the same notification to several users. Returns the count sent."""
    sent = 0
    for uid in {u for u in user_ids if u}:
        if notify(uid, type, title, **kw) is not None:
            sent += 1
    return sent


# --------------------------------------------------------------------------- #
# Reads
# --------------------------------------------------------------------------- #
#: Listing scopes. ``unread`` is the working set, ``history`` is everything the
#: user has already dealt with, ``all`` is both.
SCOPE_UNREAD = "unread"
SCOPE_HISTORY = "history"
SCOPE_ALL = "all"
SCOPES = (SCOPE_UNREAD, SCOPE_HISTORY, SCOPE_ALL)


def unread_count(user_id: int) -> int:
    if not user_id:
        return 0
    return (Notification.query
            .filter_by(user_id=user_id, is_read=False)
            .filter(Notification.archived_at.is_(None))
            .count())


def history_count(user_id: int) -> int:
    """How many rows sit in the history tab (read and/or archived)."""
    if not user_id:
        return 0
    return (Notification.query
            .filter_by(user_id=user_id)
            .filter(db.or_(Notification.is_read.is_(True),
                           Notification.archived_at.isnot(None)))
            .count())


def _scoped(user_id: int, scope: str):
    q = Notification.query.filter_by(user_id=user_id)
    if scope == SCOPE_UNREAD:
        return (q.filter_by(is_read=False)
                 .filter(Notification.archived_at.is_(None)))
    if scope == SCOPE_HISTORY:
        return q.filter(db.or_(Notification.is_read.is_(True),
                               Notification.archived_at.isnot(None)))
    return q


def list_for(user_id: int, *, scope: str = SCOPE_ALL,
             only_unread: Optional[bool] = None,
             limit: Optional[int] = None) -> list[Notification]:
    """Notifications for one user, newest first.

    The default stays ``all`` -- the scope argument is a new capability, not a
    new default. Narrowing it here would quietly change what every existing
    caller receives, which is exactly the kind of change that shows up as a
    missing row in production rather than as a failure.

    ``only_unread`` is the pre-scope keyword and is still honoured.
    """
    if not user_id:
        return []
    if only_unread is not None:
        scope = SCOPE_UNREAD if only_unread else SCOPE_ALL
    if scope not in SCOPES:
        scope = SCOPE_ALL
    cap = int(limit or current_app.config.get("LM_NOTIFY_PAGE_SIZE", 30) or 30)
    return (_scoped(user_id, scope)
            .order_by(Notification.id.desc())
            .limit(max(1, min(cap, 200)))
            .all())


def mark_read(user_id: int, ids: Optional[Iterable[int]] = None) -> int:
    """Mark the given notifications read; with no ids, mark them all read.

    ``read_at`` is stamped here because it -- not ``created_at`` -- is what the
    retention sweep ages rows by.
    """
    if not user_id:
        return 0
    q = Notification.query.filter_by(user_id=user_id, is_read=False)
    if ids is not None:
        wanted = [int(i) for i in ids if str(i).strip().lstrip("-").isdigit()]
        if not wanted:
            return 0
        q = q.filter(Notification.id.in_(wanted))
    now = _utcnow()
    changed = q.update({"is_read": True, "read_at": now, "updated_at": now},
                       synchronize_session=False)
    db.session.commit()
    return int(changed or 0)


def archive(user_id: int, ids: Optional[Iterable[int]] = None) -> int:
    """File notifications away into history (read + archived).

    Distinct from deletion on purpose: "I have dealt with this" and "this never
    happened" are different statements, and the second one destroys the only
    record that a review was ever requested.
    """
    if not user_id:
        return 0
    q = Notification.query.filter_by(user_id=user_id).filter(
        Notification.archived_at.is_(None))
    if ids is not None:
        wanted = [int(i) for i in ids if str(i).strip().lstrip("-").isdigit()]
        if not wanted:
            return 0
        q = q.filter(Notification.id.in_(wanted))
    now = _utcnow()
    changed = q.update({"archived_at": now, "is_read": True,
                        "read_at": now, "updated_at": now},
                       synchronize_session=False)
    db.session.commit()
    return int(changed or 0)


def clear_history(user_id: int) -> int:
    """Delete the history tab. Unread rows are never touched.

    The bell fills up because nothing ever leaves it; this is the manual escape
    hatch that does not require waiting out the retention window. It refuses to
    take outstanding work with it -- clearing a list must not be able to delete
    a review request the user has not read.
    """
    if not user_id:
        return 0
    deleted = (Notification.query
               .filter_by(user_id=user_id)
               .filter(db.or_(Notification.is_read.is_(True),
                              Notification.archived_at.isnot(None)))
               .delete(synchronize_session=False))
    db.session.commit()
    return int(deleted or 0)


def purge_old(days: Optional[int] = None) -> int:
    """Delete *read* notifications older than the retention window.

    Aged by ``read_at``, not ``created_at``. Ageing by creation time deletes a
    two-month-old notification the moment it is read -- it vanishes out from
    under the user in the same session they finally looked at it, which is
    indistinguishable from a bug.

    Unread rows are never purged by age: an unread notification is outstanding
    work, and silently deleting it is how a review request gets lost. Rows read
    before ``read_at`` existed have it NULL and fall back to ``created_at``.
    """
    window = int(days if days is not None
                 else current_app.config.get("LM_NOTIFY_RETENTION_DAYS", 30))
    if window <= 0:
        return 0
    cutoff = _utcnow() - timedelta(days=window)
    aged = db.func.coalesce(Notification.read_at, Notification.archived_at,
                            Notification.created_at)
    # Archived rows age out too: filing something away is the user saying they
    # are done with it. Only *unread and unfiled* rows are protected.
    deleted = (Notification.query
               .filter(db.or_(Notification.is_read.is_(True),
                              Notification.archived_at.isnot(None)))
               .filter(aged < cutoff)
               .delete(synchronize_session=False))
    db.session.commit()
    return int(deleted or 0)
