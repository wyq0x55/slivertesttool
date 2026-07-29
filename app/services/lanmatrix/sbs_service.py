"""In-app editor backend for a bundle model's ``.sbs`` file.

A ``bundle`` :class:`~app.models.lanmatrix.ProjectModel` stores its uploaded
``dll`` + ``sbs`` + ``pdb`` (plus the generated ``.sil``) side by side in
``bundle_dir``. The ``.sbs`` is passed to the already-built dll at *load* time
(``<dll> -S <sbs>``), so editing it changes runtime behaviour on the next run
without rebuilding the dll or regenerating the ``.sil``.

This module lets administrators/editors read and overwrite that ``.sbs`` from
the web UI with:

* **optimistic locking** -- the client sends the ``base_version`` (sha256) it
  loaded; a mismatch against the current on-disk sha raises :class:`SbsConflict`
  (HTTP 409) so a concurrent edit is never silently clobbered;
* a ``.sbs.bak`` **backup** of the previous on-disk content on every write;
* a **revision history** row per save (:class:`~app.models.lanmatrix.SbsRevision`),
  pruned to the most recent :data:`MAX_REVISIONS` per model;
* an **audit** entry (``sbs.edit`` / ``sbs.restore``).

The ``.sbs`` filename is not stored in the DB -- the single ``*.sbs`` inside
``bundle_dir`` is located by glob.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import List, Optional, Tuple

from ...extensions import db
from ...models import ProjectModel, SbsRevision
from . import audit
from .errors import ServiceError

# Keep only the most recent N revisions per model (user decision).
MAX_REVISIONS = 50
# Refuse absurdly large payloads (defensive; a .sbs is a small text spec).
MAX_SBS_BYTES = 4 * 1024 * 1024


class SbsError(ServiceError):
    """Invalid SBS edit request (missing model / not a bundle / no sbs)."""


class SbsConflict(ServiceError):
    """The on-disk .sbs changed since the client loaded it (optimistic lock)."""

    def __init__(self, server_data: dict):
        super().__init__(
            "该 SBS 文件已被其他人修改，请刷新后重新编辑。",
            code="SBS_CONFLICT", details=server_data)
        self.server_data = server_data


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", "surrogatepass")).hexdigest()


def _model(project_id: int, name: str) -> ProjectModel:
    row = (ProjectModel.query
           .filter_by(project_id=project_id, name=(name or "").strip())
           .first())
    if row is None:
        raise SbsError("模型不存在。", code="NOT_FOUND")
    if row.kind != "bundle" or not row.bundle_dir:
        raise SbsError("仅上传型（dll+sbs+pdb）模型的 SBS 可在线编辑。",
                       code="NOT_FOUND")
    return row


def _sbs_path(row: ProjectModel) -> Path:
    """The single ``*.sbs`` inside the model's bundle directory."""
    base = Path(row.bundle_dir)
    matches = sorted(base.glob("*.sbs"))
    if not matches:
        raise SbsError("该模型目录下找不到 .sbs 文件。", code="NOT_FOUND")
    return matches[0]


def _read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace")


# --------------------------------------------------------------------------- #
# Public API
# --------------------------------------------------------------------------- #
def read_sbs(project_id: int, name: str) -> dict:
    """Return ``{model, filename, content, version, size}`` for the model's sbs."""
    row = _model(project_id, name)
    path = _sbs_path(row)
    content = _read_text(path)
    return {
        "model": row.name,
        "model_id": row.id,
        "filename": path.name,
        "content": content,
        "version": _sha(content),
        "size": len(content.encode("utf-8", "surrogatepass")),
    }


def write_sbs(project_id: int, name: str, content: str, base_version: str,
              *, author_id: Optional[int] = None,
              client_ip: Optional[str] = None) -> dict:
    """Overwrite the model's ``.sbs`` under an optimistic lock.

    ``base_version`` is the sha256 the client loaded. If it no longer matches
    the on-disk sha, :class:`SbsConflict` is raised (the caller returns 409 with
    the current server content so the user can merge). On success the previous
    content is backed up to ``<sbs>.bak``, a revision row is appended, history
    is pruned to :data:`MAX_REVISIONS`, and an ``sbs.edit`` audit is written.
    """
    if content is None:
        raise SbsError("内容不能为空。")
    encoded = content.encode("utf-8", "surrogatepass")
    if len(encoded) > MAX_SBS_BYTES:
        raise SbsError("SBS 文件过大（超过 4 MB）。")

    row = _model(project_id, name)
    path = _sbs_path(row)
    current = _read_text(path)
    current_sha = _sha(current)

    if base_version and base_version != current_sha:
        # Someone else wrote since the client loaded -- refuse to clobber.
        raise SbsConflict({
            "filename": path.name,
            "content": current,
            "version": current_sha,
            "size": len(current.encode("utf-8", "surrogatepass")),
        })

    new_sha = _sha(content)
    if new_sha == current_sha:
        # No-op save: nothing changed on disk, don't spam history.
        return {
            "model": row.name, "model_id": row.id, "filename": path.name,
            "version": current_sha, "size": len(encoded), "unchanged": True,
        }

    # Backup previous content, then write atomically (tmp + replace).
    try:
        if path.exists():
            (path.parent / (path.name + ".bak")).write_text(
                current, encoding="utf-8")
        tmp = path.parent / (path.name + ".tmp")
        tmp.write_text(content, encoding="utf-8")
        tmp.replace(path)
    except OSError as exc:  # noqa: BLE001
        raise SbsError(f"写入 SBS 文件失败：{exc}") from exc

    rev = SbsRevision(
        project_id=project_id, model_id=row.id, filename=path.name,
        content=content, sha256=new_sha, size=len(encoded),
        author_id=author_id)
    db.session.add(rev)
    db.session.flush()
    _prune(row.id)

    audit.record(
        "sbs.edit", actor_id=author_id, object_type="sbs",
        object_id=row.id, project_id=project_id,
        old_value={"version": current_sha},
        new_value={"version": new_sha, "filename": path.name,
                   "revision_id": rev.id},
        client_ip=client_ip)
    db.session.commit()

    return {
        "model": row.name, "model_id": row.id, "filename": path.name,
        "version": new_sha, "size": len(encoded),
        "revision_id": rev.id,
    }


def list_revisions(project_id: int, name: str) -> List[dict]:
    """History (newest first), metadata only -- no content."""
    row = _model(project_id, name)
    revs = (SbsRevision.query
            .filter_by(model_id=row.id)
            .order_by(SbsRevision.created_at.desc(), SbsRevision.id.desc())
            .limit(MAX_REVISIONS)
            .all())
    current = _sha(_read_text(_sbs_path(row)))
    out = []
    for r in revs:
        d = r.to_dict()
        d["is_current"] = (r.sha256 == current)
        out.append(d)
    return out


def get_revision(project_id: int, name: str, revision_id: int) -> dict:
    """A single revision including its full content."""
    row = _model(project_id, name)
    rev = db.session.get(SbsRevision, int(revision_id))
    if rev is None or rev.model_id != row.id:
        raise SbsError("历史版本不存在。", code="NOT_FOUND")
    return rev.to_dict(include_content=True)


def restore_revision(project_id: int, name: str, revision_id: int,
                     *, author_id: Optional[int] = None,
                     client_ip: Optional[str] = None) -> dict:
    """Restore a past revision by saving its content as a new revision.

    Uses the current on-disk sha as ``base_version`` so it applies cleanly (a
    restore is an explicit overwrite; it still records a fresh history entry and
    ``.sbs.bak`` backup).
    """
    row = _model(project_id, name)
    rev = db.session.get(SbsRevision, int(revision_id))
    if rev is None or rev.model_id != row.id:
        raise SbsError("历史版本不存在。", code="NOT_FOUND")
    current_sha = _sha(_read_text(_sbs_path(row)))
    result = write_sbs(project_id, name, rev.content, current_sha,
                       author_id=author_id, client_ip=client_ip)
    result["restored_from"] = rev.id
    return result


def _prune(model_id: int) -> None:
    """Delete all but the most recent :data:`MAX_REVISIONS` rows for a model."""
    ids = (db.session.query(SbsRevision.id)
           .filter_by(model_id=model_id)
           .order_by(SbsRevision.created_at.desc(), SbsRevision.id.desc())
           .offset(MAX_REVISIONS)
           .all())
    stale = [i for (i,) in ids]
    if stale:
        (SbsRevision.query
         .filter(SbsRevision.id.in_(stale))
         .delete(synchronize_session=False))
