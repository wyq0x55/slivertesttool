"""SQLAlchemy models for the LAN Test Matrix platform (PRD §9).

Runs on PostgreSQL. Custom field values are stored in a ``JSONB`` column (the
portable ``JSON().with_variant(JSONB, "postgresql")`` mapping resolves to JSONB
here), so high-frequency core fields stay first-class columns while dynamic
fields live in ``test_items.custom_values``.
"""

from __future__ import annotations

import datetime as _dt
import uuid
from typing import Any, Optional

from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.types import JSON

from ..extensions import db

# Portable JSONB: JSONB on PostgreSQL, plain JSON (TEXT) elsewhere.
JSONType = JSON().with_variant(JSONB, "postgresql")


def _utcnow() -> _dt.datetime:
    return _dt.datetime.now(_dt.timezone.utc)


def _iso(value: Optional[_dt.datetime]) -> Optional[str]:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=_dt.timezone.utc)
    return value.astimezone(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _uuid() -> str:
    return uuid.uuid4().hex


# --------------------------------------------------------------------------- #
# Users & membership
# --------------------------------------------------------------------------- #
class LMUser(db.Model):
    __tablename__ = "lm_users"

    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(64), unique=True, nullable=False, index=True)
    display_name = db.Column(db.String(120), nullable=False, default="")
    password_hash = db.Column(db.String(255), nullable=False, default="")
    email = db.Column(db.String(255), nullable=True)
    status = db.Column(db.String(16), nullable=False, default="active")  # active|disabled
    is_system_admin = db.Column(db.Boolean, nullable=False, default=False)
    must_change_password = db.Column(db.Boolean, nullable=False, default=False)
    failed_logins = db.Column(db.Integer, nullable=False, default=0)
    locked_until = db.Column(db.DateTime, nullable=True)
    last_login_at = db.Column(db.DateTime, nullable=True)
    created_at = db.Column(db.DateTime, nullable=False, default=_utcnow)
    updated_at = db.Column(db.DateTime, nullable=False, default=_utcnow, onupdate=_utcnow)

    def set_password(self, raw: str) -> None:
        from werkzeug.security import generate_password_hash
        self.password_hash = generate_password_hash(raw)

    def check_password(self, raw: str) -> bool:
        from werkzeug.security import check_password_hash
        if not self.password_hash:
            return False
        return check_password_hash(self.password_hash, raw)

    @property
    def is_active(self) -> bool:
        return self.status == "active"

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "username": self.username,
            "display_name": self.display_name or self.username,
            "email": self.email,
            "status": self.status,
            "is_system_admin": self.is_system_admin,
            "must_change_password": self.must_change_password,
            "last_login_at": _iso(self.last_login_at),
        }


class ProjectMember(db.Model):
    __tablename__ = "lm_project_members"
    __table_args__ = (
        db.UniqueConstraint("project_id", "user_id", name="uq_member_project_user"),
    )

    id = db.Column(db.Integer, primary_key=True)
    project_id = db.Column(
        db.Integer, db.ForeignKey("lm_projects.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    user_id = db.Column(
        db.Integer, db.ForeignKey("lm_users.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    # project_admin | editor | reviewer | reader
    role = db.Column(db.String(24), nullable=False, default="reader")
    created_at = db.Column(db.DateTime, nullable=False, default=_utcnow)

    user = db.relationship("LMUser")

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "user_id": self.user_id,
            "username": self.user.username if self.user else None,
            "display_name": self.user.display_name if self.user else None,
            "role": self.role,
        }


# --------------------------------------------------------------------------- #
# Projects
# --------------------------------------------------------------------------- #
class Project(db.Model):
    __tablename__ = "lm_projects"

    id = db.Column(db.Integer, primary_key=True)
    code = db.Column(db.String(32), unique=True, nullable=False, index=True)
    name = db.Column(db.String(100), nullable=False)
    description = db.Column(db.Text, nullable=False, default="")
    # draft | active | frozen | archived
    status = db.Column(db.String(16), nullable=False, default="draft", index=True)
    owner_id = db.Column(db.Integer, db.ForeignKey("lm_users.id"), nullable=True)
    created_by = db.Column(db.Integer, db.ForeignKey("lm_users.id"), nullable=True)
    created_at = db.Column(db.DateTime, nullable=False, default=_utcnow)
    updated_at = db.Column(db.DateTime, nullable=False, default=_utcnow, onupdate=_utcnow)
    deleted_at = db.Column(db.DateTime, nullable=True, index=True)

    # Test-Matrix (Japanese workbook) round-trip metadata, captured on import so
    # export can rebuild a byte-compatible workbook.
    tm_id_prefix = db.Column(db.String(64), nullable=True)
    tm_summary_sheet = db.Column(db.String(120), nullable=True)

    members = db.relationship(
        "ProjectMember", backref="project",
        cascade="all, delete-orphan", passive_deletes=True,
    )
    fields = db.relationship(
        "FieldDefinition", backref="project",
        cascade="all, delete-orphan", passive_deletes=True,
        order_by="FieldDefinition.display_order",
    )

    @property
    def is_editable(self) -> bool:
        return self.status in ("draft", "active") and self.deleted_at is None

    def to_dict(self, *, member_count: Optional[int] = None) -> dict:
        return {
            "id": self.id,
            "code": self.code,
            "name": self.name,
            "description": self.description,
            "status": self.status,
            "owner_id": self.owner_id,
            "member_count": member_count if member_count is not None else len(self.members),
            "created_at": _iso(self.created_at),
            "updated_at": _iso(self.updated_at),
            "deleted": self.deleted_at is not None,
        }


# --------------------------------------------------------------------------- #
# Field definitions (dynamic columns)
# --------------------------------------------------------------------------- #
class FieldDefinition(db.Model):
    __tablename__ = "lm_field_definitions"
    __table_args__ = (
        db.UniqueConstraint("project_id", "field_key", name="uq_field_project_key"),
    )

    id = db.Column(db.Integer, primary_key=True)
    project_id = db.Column(
        db.Integer, db.ForeignKey("lm_projects.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    field_key = db.Column(db.String(64), nullable=False)
    display_name = db.Column(db.String(120), nullable=False, default="")
    data_type = db.Column(db.String(24), nullable=False, default="text")
    # Which editor sheet/tab this column belongs to: test | const | lib.
    sheet = db.Column(db.String(16), nullable=False, default="test", index=True)
    is_system = db.Column(db.Boolean, nullable=False, default=False)
    is_required = db.Column(db.Boolean, nullable=False, default=False)
    is_readonly = db.Column(db.Boolean, nullable=False, default=False)
    default_value = db.Column(JSONType, nullable=True)
    validation_rule = db.Column(JSONType, nullable=True)
    option_source = db.Column(JSONType, nullable=True)   # {"options": [...]}
    help_text = db.Column(db.Text, nullable=False, default="")
    display_order = db.Column(db.Integer, nullable=False, default=0)
    is_active = db.Column(db.Boolean, nullable=False, default=True)
    created_at = db.Column(db.DateTime, nullable=False, default=_utcnow)
    updated_at = db.Column(db.DateTime, nullable=False, default=_utcnow, onupdate=_utcnow)

    @property
    def options(self) -> list:
        src = self.option_source or {}
        return list(src.get("options", []))

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "field_key": self.field_key,
            "display_name": self.display_name,
            "data_type": self.data_type,
            "sheet": self.sheet or "test",
            "is_system": self.is_system,
            "is_required": self.is_required,
            "is_readonly": self.is_readonly,
            "default_value": self.default_value,
            "validation_rule": self.validation_rule or {},
            "options": self.options,
            "help_text": self.help_text,
            "display_order": self.display_order,
            "is_active": self.is_active,
        }


# --------------------------------------------------------------------------- #
# Per-project plant models
#
# Model management now lives **inside a project** (each project owns its own
# ``.sil`` plant models) instead of a single global admin-registered list. A
# model is either:
#   * ``kind="path"``   -- a server-side absolute ``.sil`` path, opened in place;
#   * ``kind="bundle"`` -- a ``host.dll`` + ``host.sbs`` pair uploaded through the
#     web UI. The service materialises them into a per-project directory and
#     generates an empty ``.sil`` that adds a single module
#     ``<dll> -S <sbs>``; the dll and sbs sit next to the generated ``.sil`` so
#     Silver resolves the relative names against the model's own directory.
# --------------------------------------------------------------------------- #
class ProjectModel(db.Model):
    __tablename__ = "lm_project_models"
    __table_args__ = (
        db.UniqueConstraint("project_id", "name", name="uq_model_project_name"),
    )

    id = db.Column(db.Integer, primary_key=True)
    project_id = db.Column(
        db.Integer, db.ForeignKey("lm_projects.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    name = db.Column(db.String(120), nullable=False)
    # path | bundle
    kind = db.Column(db.String(16), nullable=False, default="path")
    # Absolute path to the ``.sil`` Silver opens (the registered path, or the
    # generated one inside ``bundle_dir``).
    sil_path = db.Column(db.String(1024), nullable=False, default="")
    # For ``bundle`` models: the server directory holding the generated ``.sil``
    # together with the uploaded dll + sbs (removed when the model is deleted).
    bundle_dir = db.Column(db.String(1024), nullable=True)
    created_by = db.Column(db.Integer, db.ForeignKey("lm_users.id"), nullable=True)
    created_at = db.Column(db.DateTime, nullable=False, default=_utcnow)

    def to_dict(self, *, include_path: bool = False) -> dict:
        import os
        entry = {
            "id": self.id,
            "name": self.name,
            "kind": self.kind,
            "exists": bool(self.sil_path) and os.path.isfile(self.sil_path),
            "created_at": _iso(self.created_at),
        }
        if include_path:
            entry["path"] = self.sil_path
        return entry


# --------------------------------------------------------------------------- #
# Test items
# --------------------------------------------------------------------------- #
class TestItemRow(db.Model):
    __tablename__ = "lm_test_items"
    __table_args__ = (
        db.Index("ix_item_project_case", "project_id", "case_id"),
        db.Index("ix_item_project_status", "project_id", "workflow_status"),
    )

    id = db.Column(db.Integer, primary_key=True)
    uuid = db.Column(db.String(32), nullable=False, default=_uuid, index=True)
    project_id = db.Column(
        db.Integer, db.ForeignKey("lm_projects.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    row_order = db.Column(db.Integer, nullable=False, default=0, index=True)
    # Which editor sheet/tab this row belongs to: test | const | lib.
    sheet = db.Column(db.String(16), nullable=False, default="test", index=True)

    case_id = db.Column(db.String(128), nullable=False, default="")
    title = db.Column(db.Text, nullable=False, default="")
    module = db.Column(db.String(128), nullable=True)
    precondition = db.Column(db.Text, nullable=False, default="")
    test_steps = db.Column(db.Text, nullable=False, default="")
    expected_result = db.Column(db.Text, nullable=False, default="")
    actual_result = db.Column(db.Text, nullable=False, default="")
    result = db.Column(db.String(24), nullable=False, default="Not Tested")
    priority = db.Column(db.String(24), nullable=True)
    owner_id = db.Column(db.Integer, db.ForeignKey("lm_users.id"), nullable=True)
    tags = db.Column(JSONType, nullable=True)          # list[str]
    comment = db.Column(db.Text, nullable=False, default="")
    custom_values = db.Column(JSONType, nullable=True)  # {field_key: value}
    workflow_status = db.Column(db.String(24), nullable=False, default="Draft", index=True)

    version = db.Column(db.Integer, nullable=False, default=1)
    created_by = db.Column(db.Integer, db.ForeignKey("lm_users.id"), nullable=True)
    created_at = db.Column(db.DateTime, nullable=False, default=_utcnow)
    updated_by = db.Column(db.Integer, db.ForeignKey("lm_users.id"), nullable=True)
    updated_at = db.Column(db.DateTime, nullable=False, default=_utcnow, onupdate=_utcnow)
    deleted_at = db.Column(db.DateTime, nullable=True, index=True)

    # New unified ("identity") protocol field keys that alias onto an existing
    # first-class column, so the Test-Matrix editor vocabulary is stored in real
    # columns (searchable / sortable / indexed) instead of the ``custom_values``
    # JSONB bag. Kept as a named map so query helpers and the boot-time data
    # migration (``_migrate_testitem_field_keys``) share one source of truth.
    _FIELD_ALIASES = {
        "test_name": "title",
        "remark": "comment",
    }

    _SYSTEM_COLUMN = {
        "case_id": "case_id", "title": "title", "module": "module",
        "precondition": "precondition", "test_steps": "test_steps",
        "expected_result": "expected_result", "actual_result": "actual_result",
        "result": "result", "priority": "priority", "owner": "owner_id",
        "tags": "tags", "comment": "comment", "workflow_status": "workflow_status",
        # Unified-protocol aliases (test_name -> title, remark -> comment).
        **_FIELD_ALIASES,
    }

    # NOT NULL string columns: a cleared/blank value must become "" (never NULL),
    # so draft rows and cell-clearing don't violate the schema.
    _NOT_NULL_STR_COLUMNS = frozenset({
        "case_id", "title", "precondition", "test_steps", "expected_result",
        "actual_result", "result", "comment", "workflow_status",
    })

    def get_field(self, field_key: str) -> Any:
        col = self._SYSTEM_COLUMN.get(field_key)
        if col is not None:
            return getattr(self, col)
        return (self.custom_values or {}).get(field_key)

    def set_field(self, field_key: str, value: Any) -> None:
        col = self._SYSTEM_COLUMN.get(field_key)
        if col is not None:
            if value is None and col in self._NOT_NULL_STR_COLUMNS:
                value = ""
            setattr(self, col, value)
            return
        cv = dict(self.custom_values or {})
        cv[field_key] = value
        self.custom_values = cv

    def to_dict(self) -> dict:
        data = {
            "id": self.id,
            "uuid": self.uuid,
            "row_order": self.row_order,
            "sheet": self.sheet or "test",
            "case_id": self.case_id,
            "title": self.title,
            "module": self.module,
            "precondition": self.precondition,
            "test_steps": self.test_steps,
            "expected_result": self.expected_result,
            "actual_result": self.actual_result,
            "result": self.result,
            "priority": self.priority,
            "owner": self.owner_id,
            "tags": list(self.tags or []),
            "comment": self.comment,
            "workflow_status": self.workflow_status,
            "version": self.version,
            "updated_at": _iso(self.updated_at),
            "updated_by": self.updated_by,
        }
        # Surface the unified-protocol aliases (test_name/remark) that the editor
        # reads, sourced from their first-class columns. ``custom_values`` is
        # overlaid afterwards, so a row not yet touched by the boot migration
        # (value still in JSONB under the new key) keeps rendering correctly
        # during a rolling upgrade.
        for alias, col in self._FIELD_ALIASES.items():
            data[alias] = getattr(self, col)
        data.update(self.custom_values or {})
        return data


# --------------------------------------------------------------------------- #
# Comments
# --------------------------------------------------------------------------- #
class CellComment(db.Model):
    __tablename__ = "lm_cell_comments"

    id = db.Column(db.Integer, primary_key=True)
    project_id = db.Column(db.Integer, db.ForeignKey("lm_projects.id", ondelete="CASCADE"),
                           nullable=False, index=True)
    test_item_id = db.Column(db.Integer, db.ForeignKey("lm_test_items.id", ondelete="CASCADE"),
                             nullable=False, index=True)
    field_key = db.Column(db.String(64), nullable=False)
    content = db.Column(db.Text, nullable=False, default="")
    created_by = db.Column(db.Integer, db.ForeignKey("lm_users.id"), nullable=True)
    created_at = db.Column(db.DateTime, nullable=False, default=_utcnow)
    edited_at = db.Column(db.DateTime, nullable=True)
    deleted_at = db.Column(db.DateTime, nullable=True)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "test_item_id": self.test_item_id,
            "field_key": self.field_key,
            "content": self.content,
            "created_by": self.created_by,
            "created_at": _iso(self.created_at),
            "edited_at": _iso(self.edited_at),
        }


# --------------------------------------------------------------------------- #
# Audit log
# --------------------------------------------------------------------------- #
class AuditLog(db.Model):
    __tablename__ = "lm_audit_logs"
    __table_args__ = (
        db.Index("ix_audit_project_time", "project_id", "created_at"),
        db.Index("ix_audit_batch", "batch_id"),
    )

    id = db.Column(db.Integer, primary_key=True)
    request_id = db.Column(db.String(48), nullable=True)
    batch_id = db.Column(db.String(48), nullable=True, index=True)
    actor_id = db.Column(db.Integer, nullable=True)
    action = db.Column(db.String(48), nullable=False)
    object_type = db.Column(db.String(32), nullable=False, default="")
    object_id = db.Column(db.String(48), nullable=True)
    project_id = db.Column(db.Integer, nullable=True, index=True)
    old_value = db.Column(JSONType, nullable=True)
    new_value = db.Column(JSONType, nullable=True)
    client_ip = db.Column(db.String(64), nullable=True)
    result = db.Column(db.String(16), nullable=False, default="success")
    error_summary = db.Column(db.String(255), nullable=True)
    created_at = db.Column(db.DateTime, nullable=False, default=_utcnow, index=True)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "request_id": self.request_id,
            "batch_id": self.batch_id,
            "actor_id": self.actor_id,
            "action": self.action,
            "object_type": self.object_type,
            "object_id": self.object_id,
            "project_id": self.project_id,
            "old_value": self.old_value,
            "new_value": self.new_value,
            "client_ip": self.client_ip,
            "result": self.result,
            "error_summary": self.error_summary,
            "created_at": _iso(self.created_at),
        }


# --------------------------------------------------------------------------- #
# Import / export jobs
# --------------------------------------------------------------------------- #
class DataJob(db.Model):
    __tablename__ = "lm_data_jobs"

    id = db.Column(db.Integer, primary_key=True)
    project_id = db.Column(db.Integer, db.ForeignKey("lm_projects.id", ondelete="CASCADE"),
                           nullable=False, index=True)
    job_type = db.Column(db.String(16), nullable=False)  # import | export
    status = db.Column(db.String(16), nullable=False, default="pending")
    original_filename = db.Column(db.String(255), nullable=True)
    stored_filename = db.Column(db.String(255), nullable=True)
    parameters = db.Column(JSONType, nullable=True)
    preview = db.Column(JSONType, nullable=True)
    total_count = db.Column(db.Integer, nullable=False, default=0)
    success_count = db.Column(db.Integer, nullable=False, default=0)
    error_count = db.Column(db.Integer, nullable=False, default=0)
    result_file_path = db.Column(db.String(512), nullable=True)
    created_by = db.Column(db.Integer, nullable=True)
    created_at = db.Column(db.DateTime, nullable=False, default=_utcnow)
    started_at = db.Column(db.DateTime, nullable=True)
    finished_at = db.Column(db.DateTime, nullable=True)
    expires_at = db.Column(db.DateTime, nullable=True)

    def to_dict(self, *, with_preview: bool = False) -> dict:
        data = {
            "id": self.id,
            "project_id": self.project_id,
            "job_type": self.job_type,
            "status": self.status,
            "original_filename": self.original_filename,
            "total_count": self.total_count,
            "success_count": self.success_count,
            "error_count": self.error_count,
            "created_at": _iso(self.created_at),
            "finished_at": _iso(self.finished_at),
        }
        if with_preview:
            data["preview"] = self.preview
            data["parameters"] = self.parameters
        return data


# --------------------------------------------------------------------------- #
# Collaboration (CRDT) persistence
#
# Append-only log of Yjs/CRDT updates for a project's collaborative document,
# written by the single collab server (run_collab). One row == one Y update.
# ``seq`` is a per-project monotonic counter; periodic compaction merges the
# whole log into a single squashed update at ``seq = 1`` (see PgYStore).
# This is the PostgreSQL-authoritative equivalent of y-leveldb / y-redis
# persistence (design doc §5.2 / §7.2).
# --------------------------------------------------------------------------- #
class CollabDoc(db.Model):
    __tablename__ = "lm_collab_doc"
    __table_args__ = (
        db.UniqueConstraint("project_id", "seq", name="uq_collab_project_seq"),
    )

    id = db.Column(db.Integer, primary_key=True)
    project_id = db.Column(
        db.Integer, db.ForeignKey("lm_projects.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    seq = db.Column(db.Integer, nullable=False)
    update = db.Column(db.LargeBinary, nullable=False)
    doc_metadata = db.Column(db.LargeBinary, nullable=True)
    ts = db.Column(db.Float, nullable=False, default=0.0)
    created_at = db.Column(db.DateTime, nullable=False, default=_utcnow)


# --------------------------------------------------------------------------- #
# Cross-process collaboration presence heartbeat (design doc §1.6 / §12.3).
#
# The collab server (run_collab) owns exactly one row per project and refreshes
# ``connections`` + ``updated_at`` on a heartbeat while a room has live clients;
# it drops ``connections`` to 0 when the room is evicted. The web process (which
# cannot see the collab server's in-memory rooms) reads this table to decide
# whether a project is in "collaborative mode" — i.e. whether the CRDT
# materializer is the single authoritative writer and direct REST row mutations
# must step aside (see ``app/collab/presence.py`` and the REST guard).
#
# A row is only treated as "active" when ``connections > 0`` AND ``updated_at``
# is fresher than COLLAB_PRESENCE_TTL_SECONDS, so a crashed collab server
# naturally lets the project fall back to classic REST writes.
# --------------------------------------------------------------------------- #
class CollabPresence(db.Model):
    __tablename__ = "lm_collab_presence"

    project_id = db.Column(
        db.Integer, db.ForeignKey("lm_projects.id", ondelete="CASCADE"),
        primary_key=True,
    )
    connections = db.Column(db.Integer, nullable=False, default=0)
    updated_at = db.Column(db.DateTime, nullable=False,
                           default=_utcnow, onupdate=_utcnow)
