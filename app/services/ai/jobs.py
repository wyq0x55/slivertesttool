"""Bounded generation attempts with durable publication and fenced results."""

from __future__ import annotations

import datetime
import json
import logging
import threading
import uuid
from contextvars import ContextVar
from typing import Callable

from sqlalchemy.dialects.postgresql import insert

from ...extensions import db
from ...models import AiDraft, Setting

AI_CONCURRENCY = 2
LEASE_SECONDS = 900
_slots = threading.BoundedSemaphore(AI_CONCURRENCY)
_ownership_lock = threading.Lock()
_owners: dict[threading.Thread, list] = {}
_checkpoint: ContextVar = ContextVar("ai_checkpoint", default=None)
logger = logging.getLogger(__name__)


class AttemptStopped(RuntimeError):
    pass


def metadata(draft: AiDraft) -> dict:
    try:
        parsed = json.loads(draft.meta_json) if draft.meta_json else {}
    except ValueError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _now() -> float:
    return datetime.datetime.now(datetime.timezone.utc).timestamp()


def _save(draft: AiDraft, meta: dict) -> None:
    draft.meta_json = json.dumps(meta, ensure_ascii=False)


def _locked(draft_id: int) -> AiDraft | None:
    return AiDraft.query.filter_by(id=draft_id).populate_existing().with_for_update().first()


def prepare(draft: AiDraft) -> str:
    meta = metadata(draft)
    attempt = uuid.uuid4().hex
    meta["job"] = {"attempt": attempt, "state": "queued", "heartbeat": _now(),
                   "concurrency": AI_CONCURRENCY, "chunk_size": 8, "max_rounds": 3}
    meta.pop("progress", None)
    _save(draft, meta)
    draft.status = AiDraft.STATUS_RUNNING
    draft.error = ""
    draft.output_json = ""
    return attempt


def _reap_owners() -> None:
    for owner in list(_owners):
        if not owner.is_alive():
            for _slot in _owners.pop(owner):
                _slots.release()


def acquire_slot() -> bool:
    with _ownership_lock:
        _reap_owners()
        if not _slots.acquire(blocking=False):
            return False
        _owners.setdefault(threading.current_thread(), []).append(None)
        return True


def release_slot() -> None:
    with _ownership_lock:
        owner = threading.current_thread()
        owned = _owners.get(owner)
        if not owned:
            raise RuntimeError("No AI slot held by the current thread")
        owned.pop()
        if not owned:
            del _owners[owner]
        _slots.release()


def _is_active(draft_id: int, attempt: str) -> bool:
    identity = (db.engine, draft_id, attempt)
    with _ownership_lock:
        _reap_owners()
        return any(identity in owned for owned in _owners.values())


def claim(draft_id: int, attempt: str) -> bool:
    draft = _locked(draft_id)
    meta = metadata(draft) if draft else {}
    job = meta.get("job") or {}
    if (draft is None or draft.status != AiDraft.STATUS_RUNNING
            or job.get("attempt") != attempt or job.get("state") != "queued"):
        db.session.rollback()
        return False
    job.update(state="running", heartbeat=_now())
    _save(draft, meta)
    with _ownership_lock:
        owned = _owners.get(threading.current_thread())
        db.session.commit()
        if owned:
            owned[-1] = (db.engine, draft_id, attempt)
    return True


def _publication_due(job: dict, now: float) -> bool:
    until = job.get("publication_until")
    return not isinstance(until, (int, float)) or not now < until <= now + LEASE_SECONDS


def publish_once(draft_id: int, dispatch: Callable[[int, str], object]) -> bool:
    """Reserve a bounded lease, then dispatch the pinned queued attempt.

    All publishers must use this helper with the raw two-argument dispatcher.
    Publication is at least once: process death or timeout permits a replay,
    while claim fences execution. A failed dispatch releases its own lease.
    """
    draft = _locked(draft_id)
    meta = metadata(draft) if draft else {}
    job = meta.get("job") or {}
    attempt = job.get("attempt")
    now = _now()
    if (draft is None or draft.status != AiDraft.STATUS_RUNNING
            or job.get("state") != "queued" or not isinstance(attempt, str) or not attempt
            or not _publication_due(job, now)):
        db.session.rollback()
        return False
    publication_id = uuid.uuid4().hex
    job.update(publication_id=publication_id, publication_until=now + LEASE_SECONDS)
    _save(draft, meta)
    db.session.commit()
    try:
        dispatch(draft_id, attempt)
    except BaseException:
        try:
            db.session.rollback()
            draft = _locked(draft_id)
            meta = metadata(draft) if draft else {}
            job = meta.get("job") or {}
            if job.get("attempt") == attempt and job.get("publication_id") == publication_id:
                job.pop("publication_id", None)
                job.pop("publication_until", None)
                _save(draft, meta)
            db.session.commit()
        except Exception:
            db.session.rollback()
            logger.exception("Could not release AI publication lease for draft=%s", draft_id)
        raise
    return True


def touch(draft_id: int, attempt: str, event: dict | None = None) -> None:
    draft = _locked(draft_id)
    meta = metadata(draft) if draft else {}
    job = meta.get("job") or {}
    if (draft is None or draft.status != AiDraft.STATUS_RUNNING
            or job.get("attempt") != attempt or job.get("state") != "running"):
        db.session.rollback()
        raise AttemptStopped("Generation attempt cancelled or superseded")
    job["heartbeat"] = _now()
    if event is not None:
        meta["progress"] = event
    _save(draft, meta)
    db.session.commit()


def finish(draft_id: int, attempt: str, *, output=None, result_meta=None, error=None) -> bool:
    draft = _locked(draft_id)
    meta = metadata(draft) if draft else {}
    job = meta.get("job") or {}
    if (draft is None or draft.status != AiDraft.STATUS_RUNNING
            or job.get("attempt") != attempt or job.get("state") != "running"):
        db.session.rollback()
        return False
    meta.update(result_meta or {})
    job.update(state="failed" if error else "complete", heartbeat=_now())
    _save(draft, meta)
    draft.status = AiDraft.STATUS_ERROR if error else AiDraft.STATUS_PENDING
    draft.error = str(error or "")
    if error is None:
        draft.output_json = json.dumps(output, ensure_ascii=False, indent=2)
    db.session.commit()
    return True


def cancel(draft_id: int) -> AiDraft:
    draft = _locked(draft_id)
    if draft is None or draft.status != AiDraft.STATUS_RUNNING:
        db.session.rollback()
        raise ValueError("Only running generation can be cancelled")
    meta = metadata(draft)
    meta.setdefault("job", {}).update(state="cancelled", heartbeat=_now())
    _save(draft, meta)
    draft.status = AiDraft.STATUS_CANCELLED
    db.session.commit()
    return draft


def retry(draft_id: int) -> AiDraft:
    draft = _locked(draft_id)
    if draft is None or draft.status not in (AiDraft.STATUS_ERROR, AiDraft.STATUS_CANCELLED):
        db.session.rollback()
        raise ValueError("Only failed or cancelled generation can be retried")
    prepare(draft)
    db.session.commit()
    return draft


def _recovery_page() -> list[AiDraft]:
    key = "ai_recovery_cursor"
    db.session.execute(insert(Setting).values(key=key, value="0")
                       .on_conflict_do_nothing(index_elements=[Setting.key]))
    setting = Setting.query.filter_by(key=key).populate_existing().with_for_update().one()
    try:
        cursor = max(0, int(setting.value))
    except (TypeError, ValueError):
        cursor = 0
    candidates = (AiDraft.query.filter_by(status=AiDraft.STATUS_RUNNING)
                  .order_by(AiDraft.id).populate_existing())
    rows = (candidates.filter(AiDraft.id > cursor).limit(200)
            .with_for_update(skip_locked=True).all())
    if cursor and len(rows) < 200:
        rows.extend(candidates.filter(AiDraft.id <= cursor).limit(200 - len(rows))
                    .with_for_update(skip_locked=True).all())
    setting.value = str(rows[-1].id) if rows else "0"
    return rows


def recover(publish: Callable[[int], object], *, startup: bool = False) -> int:
    """Reconcile at most 200 drafts per circular scan before publication.

    Startup must run before consumers. Periodic scans preserve attempts owned
    by a live local slot, even during a slow provider call. The publisher must
    call publish_once; the return count includes failed publication callbacks.
    """
    now = _now()
    with _ownership_lock:
        _reap_owners()
    rows = _recovery_page()
    queued = []
    for draft in rows:
        meta = metadata(draft)
        job = meta.get("job") or {}
        heartbeat = job.get("heartbeat")
        expired = not isinstance(heartbeat, (int, float)) or now - heartbeat > LEASE_SECONDS
        attempt = job.get("attempt")
        if startup:
            prepare(draft)
            queued.append(draft.id)
        elif job.get("state") == "queued":
            if not isinstance(attempt, str) or not attempt:
                prepare(draft)
                queued.append(draft.id)
            elif _publication_due(job, now):
                queued.append(draft.id)
        elif expired and not _is_active(draft.id, attempt):
            prepare(draft)
            queued.append(draft.id)
    db.session.commit()
    for draft_id in queued:
        try:
            publish(draft_id)
        except Exception:
            db.session.rollback()
            logger.exception("AI recovery publication failed for draft=%s", draft_id)
    return len(queued)


def install_checkpoint(callback):
    return _checkpoint.set(callback)


def reset_checkpoint(token) -> None:
    _checkpoint.reset(token)


def checkpoint() -> None:
    callback = _checkpoint.get()
    if callback is not None:
        callback()
