"""Bounded generation attempts with durable publication and fenced results."""

from __future__ import annotations

import datetime
import json
import threading
import uuid
from contextvars import ContextVar
from typing import Callable

from ...extensions import db
from ...models import AiDraft

AI_CONCURRENCY = 2
LEASE_SECONDS = 900
_slots = threading.BoundedSemaphore(AI_CONCURRENCY)
_checkpoint: ContextVar = ContextVar("ai_checkpoint", default=None)


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


def acquire_slot() -> bool:
    return _slots.acquire(blocking=False)


def release_slot() -> None:
    _slots.release()


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
    db.session.commit()
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


def recover(publish: Callable[[int], None], *, startup: bool = False) -> int:
    now = _now()
    rows = (AiDraft.query.filter_by(status=AiDraft.STATUS_RUNNING)
            .order_by(AiDraft.id).limit(200).populate_existing()
            .with_for_update(skip_locked=True).all())
    queued = []
    for draft in rows:
        meta = metadata(draft)
        job = meta.get("job") or {}
        heartbeat = job.get("heartbeat")
        expired = not isinstance(heartbeat, (int, float)) or now - heartbeat > LEASE_SECONDS
        if startup or expired:
            prepare(draft)
            queued.append(draft.id)
        elif job.get("state") == "queued":
            queued.append(draft.id)
    db.session.commit()
    for draft_id in queued:
        try:
            publish(draft_id)
        except Exception:
            db.session.rollback()
    return len(queued)


def install_checkpoint(callback):
    return _checkpoint.set(callback)


def reset_checkpoint(token) -> None:
    _checkpoint.reset(token)


def checkpoint() -> None:
    callback = _checkpoint.get()
    if callback is not None:
        callback()
