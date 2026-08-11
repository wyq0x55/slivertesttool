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

    @property
    def is_bootstrap_admin(self) -> bool:
        """The single bootstrap administrator (username == LM_ADMIN_USER).

        Ordinary accounts can be granted ``is_system_admin`` from the admin
        console, but only this bootstrap account may reach destructive,
        whole-database surfaces such as the PostgreSQL management console.
        """
        if not self.is_system_admin:
            return False
        admin_user = None
        try:  # prefer the live app config; fall back to the static default
            from flask import current_app
            admin_user = current_app.config.get("LM_ADMIN_USER")
        except Exception:
            admin_user = None
        if not admin_user:
            from ..config import Config
            admin_user = Config.LM_ADMIN_USER
        return (self.username or "").lower() == (admin_user or "").lower()

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "username": self.username,
            "display_name": self.display_name or self.username,
            "email": self.email,
            "status": self.status,
            "is_system_admin": self.is_system_admin,
            "is_bootstrap_admin": self.is_bootstrap_admin,
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

    # Which verdicts require a reviewer's sign-off before they count as final.
    # Stored per project because the answer is a team policy, not a product
    # decision: a safety-critical project reviews every PASS, an exploratory one
    # reviews nothing. Shape: ``{"pass": bool, "untestable": bool}``; a missing
    # key means "not required", so an untouched project keeps its old behaviour.
    review_required_on = db.Column(JSONType, nullable=True)

    # Per-テスト区分 reviewer routing, in priority order:
    # ``[{"category": "5", "reviewer_id": 7}, ...]``. A test matrix is
    # partitioned by 区分 and each 区分 has its own feature owner, so routing the
    # whole project to one person produces either a bottleneck or a rubber
    # stamp. Matching lives in ``services.lanmatrix.review_routes`` (pure).
    review_routes = db.Column(JSONType, nullable=True)

    # The single person reviews are routed to when no 区分 rule matched and the
    # row has no reviewer of its own. Deliberately ONE reviewer rather than a
    # pool: with a pool, every request is addressed to everybody and therefore
    # owned by nobody, and the queue only gets cleared when two people happen to
    # do it at once. One named reviewer who can reassign is the accountable
    # version.
    #
    # ``use_alter`` + an explicit name because ``lm_users`` and ``lm_projects``
    # already reference each other (owner_id / created_by); without it the two
    # CREATE TABLEs cannot be ordered.
    default_reviewer_id = db.Column(
        db.Integer,
        db.ForeignKey("lm_users.id", ondelete="SET NULL",
                      use_alter=True, name="fk_project_default_reviewer"),
        nullable=True,
    )

    # Review policy defaults, applied when ``review_required_on`` is unset.
    # ``Untestable`` defaults to ON: "this cannot be tested" is a claim that
    # silently removes a case from the evidence base, which is exactly the claim
    # that deserves a second pair of eyes.
    REVIEW_DEFAULTS = {"pass": False, "untestable": True}

    def review_policy(self) -> dict:
        """Effective ``{verdict_bucket: required}`` review policy."""
        policy = dict(self.REVIEW_DEFAULTS)
        policy.update({k: bool(v)
                       for k, v in (self.review_required_on or {}).items()
                       if k in policy})
        return policy

    def review_route_rules(self) -> list:
        """Normalised per-テスト区分 routing rules, in priority order."""
        # Imported lazily: the model layer must not depend on the service layer
        # at import time (services import models).
        from ..services.lanmatrix.review_routes import normalise_routes
        return normalise_routes(self.review_routes or [])

    def reviewer_for_category(self, category) -> Optional[int]:
        """Reviewer routed to ``category``, or ``None`` when no rule matches."""
        from ..services.lanmatrix.review_routes import match_reviewer
        return match_reviewer(self.review_routes or [], category)

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
            # Exposed so the editor can grey out structural controls instead of
            # letting the user click them into a PROJECT_LOCKED error. Derived
            # from status here rather than re-implemented in JS, so there is one
            # definition of "editable".
            "is_editable": self.is_editable,
            "owner_id": self.owner_id,
            "default_reviewer_id": self.default_reviewer_id,
            "review_routes": self.review_route_rules(),
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
    # Soft delete. Deleting a field used to wipe its value from every row on the
    # spot, which made the single most destructive action in the product also
    # the only irreversible one. The values now stay put until the recycle bin
    # expires the field for real.
    deleted_at = db.Column(db.DateTime, nullable=True, index=True)
    deleted_by = db.Column(db.Integer, db.ForeignKey("lm_users.id"), nullable=True)

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
    # Free-form but *validated* release label of the plant model (e.g. "v1.2.0",
    # "RC3_20260810"). It is stamped onto every test row this model produces a
    # verdict for (``version_label`` / バージョン) and is the grouping key of the
    # dashboard's per-version charts, so it must stay a clean token: the service
    # layer enforces ``LM_MODEL_VERSION_PATTERN`` and records every change in the
    # audit log. Nullable so pre-existing models keep working un-versioned.
    version = db.Column(db.String(64), nullable=True)
    # Human note describing what changed in this version (release note).
    version_note = db.Column(db.Text, nullable=False, default="")
    # Retiring a model must not orphan the history that references it, so an
    # obsolete model is *deprecated* (hidden from the pickers, still resolvable)
    # rather than deleted.
    deprecated_at = db.Column(db.DateTime, nullable=True)
    # path | bundle
    kind = db.Column(db.String(16), nullable=False, default="path")
    # Absolute path to the ``.sil`` Silver opens (the registered path, or the
    # generated one inside ``bundle_dir``).
    sil_path = db.Column(db.String(1024), nullable=False, default="")
    # For ``bundle`` models: the server directory holding the generated ``.sil``
    # together with the uploaded dll + sbs (removed when the model is deleted).
    bundle_dir = db.Column(db.String(1024), nullable=True)
    # The one model a project runs its tests with by default. At most one row per
    # project is current; task submit/run fall back to it when no model is named.
    is_current = db.Column(db.Boolean, nullable=False, default=False)
    created_by = db.Column(db.Integer, db.ForeignKey("lm_users.id"), nullable=True)
    created_at = db.Column(db.DateTime, nullable=False, default=_utcnow)

    def to_dict(self, *, include_path: bool = False) -> dict:
        import os

        from ..services import project_model_service as _pms

        version = self.version or ""
        entry = {
            "id": self.id,
            "name": self.name,
            "version": version,
            # ``name@version`` is the identity every other surface should show
            # and submit. Precomputed here so a call site cannot assemble it
            # differently (and pin the wrong build) by hand.
            "ref": _pms.format_ref(self.name, version),
            "ref_short": _pms.format_ref(self.name, version, short=True),
            "version_short": _pms.short_version(version),
            "is_git_sha": _pms.is_git_sha(version),
            "version_note": self.version_note or "",
            "deprecated": self.deprecated_at is not None,
            "deprecated_at": _iso(self.deprecated_at),
            "kind": self.kind,
            "exists": bool(self.sil_path) and os.path.isfile(self.sil_path),
            "is_current": bool(self.is_current),
            "created_at": _iso(self.created_at),
        }
        if include_path:
            entry["path"] = self.sil_path
        return entry


# --------------------------------------------------------------------------- #
# SBS revision history
# --------------------------------------------------------------------------- #
# Every save of a bundle model's ``.sbs`` file appends a snapshot here so the
# UI can browse / preview / restore prior versions. The optimistic-lock check
# compares the client's ``base_version`` (sha256) against the current on-disk
# sha before writing. History is pruned to the most recent 50 rows per model.
class SbsRevision(db.Model):
    __tablename__ = "lm_sbs_revisions"
    __table_args__ = (
        db.Index("ix_sbs_model_time", "model_id", "created_at"),
    )

    id = db.Column(db.Integer, primary_key=True)
    project_id = db.Column(
        db.Integer, db.ForeignKey("lm_projects.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    model_id = db.Column(
        db.Integer, db.ForeignKey("lm_project_models.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    filename = db.Column(db.String(255), nullable=False, default="")
    content = db.Column(db.Text, nullable=False, default="")
    sha256 = db.Column(db.String(64), nullable=False, default="")
    size = db.Column(db.Integer, nullable=False, default=0)
    author_id = db.Column(db.Integer, db.ForeignKey("lm_users.id"), nullable=True)
    created_at = db.Column(db.DateTime, nullable=False, default=_utcnow, index=True)

    def to_dict(self, *, include_content: bool = False) -> dict:
        entry = {
            "id": self.id,
            "filename": self.filename,
            "sha256": self.sha256,
            "size": self.size,
            "author_id": self.author_id,
            "created_at": _iso(self.created_at),
        }
        if include_content:
            entry["content"] = self.content
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
    # Wide enough for BOTH the server default (``uuid4().hex`` = 32 chars) and a
    # client/CRDT-minted canonical UUID (36 chars, dashed). The collab layer
    # persists the row under the doc's own uuid verbatim (and writes id/version
    # back keyed by that uuid), so this must not be narrower than 36 or those
    # inserts overflow with ``DataError: value too long for character varying``.
    uuid = db.Column(db.String(64), nullable=False, default=_uuid, index=True)
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

    # --- Review sign-off ---------------------------------------------------
    # A verdict is a claim; review is what turns it into an accepted result.
    # Kept in dedicated columns rather than reusing ``workflow_status`` (whose
    # "Draft" default means something else entirely) so the two state machines
    # cannot corrupt each other.
    #
    # States: "" (no review needed / not requested) -> pending -> approved
    #         | rejected. A rejected row goes back to pending on the next run.
    review_status = db.Column(db.String(16), nullable=False, default="", index=True)
    reviewer_id = db.Column(db.Integer, db.ForeignKey("lm_users.id"), nullable=True,
                            index=True)
    # Why the reviewer approved or (especially) rejected. Mandatory for a
    # rejection and for anything touching ``Untestable``.
    review_note = db.Column(db.Text, nullable=False, default="")
    review_requested_at = db.Column(db.DateTime, nullable=True)
    # Who produced the verdict that is under review, i.e. who must be told about
    # the decision. This cannot be derived from ``updated_by``: the run
    # write-back writes evidence onto the row without touching it, so
    # ``updated_by`` is whoever last hand-edited the matrix -- very often the
    # reviewer themselves, whose own decision is then suppressed as a
    # self-notification and never reaches the person who ran the test.
    review_requested_by = db.Column(db.Integer, db.ForeignKey("lm_users.id"),
                                    nullable=True, index=True)
    reviewed_at = db.Column(db.DateTime, nullable=True)
    # The verdict that was under review when the request was raised. The row's
    # ``result`` can be overwritten by a later run, so without this a reviewer
    # could unknowingly approve a verdict that no longer exists.
    review_verdict = db.Column(db.String(24), nullable=False, default="")

    version = db.Column(db.Integer, nullable=False, default=1)
    created_by = db.Column(db.Integer, db.ForeignKey("lm_users.id"), nullable=True)
    created_at = db.Column(db.DateTime, nullable=False, default=_utcnow)
    updated_by = db.Column(db.Integer, db.ForeignKey("lm_users.id"), nullable=True)
    updated_at = db.Column(db.DateTime, nullable=False, default=_utcnow, onupdate=_utcnow)
    deleted_at = db.Column(db.DateTime, nullable=True, index=True)
    # Rows are by far the most frequently deleted thing in the product, so a
    # recycle bin that cannot say who deleted one answers the question people
    # actually arrive with ("was that me, or do I need to go ask someone?")
    # for every kind except the common one.
    deleted_by = db.Column(db.Integer, db.ForeignKey("lm_users.id"), nullable=True)

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
# Test run records (append-only execution history)
# --------------------------------------------------------------------------- #
# A test row's ``result`` / ``executor`` / ``exec_date`` columns only ever hold
# the LAST run, which is all a spreadsheet can express. Every question the
# project dashboard asks -- "how did v1.2 compare with v1.1?", "how fast are we
# burning through the plan?", "when did this case last pass?" -- needs the runs
# that came before, so each finished run also appends one immutable row here.
#
# This table is the single source of truth for the dashboard; the daily metrics
# table is only a cache derived from it and can be rebuilt at any time.
class TestRunRecord(db.Model):
    __tablename__ = "lm_test_run_records"
    __table_args__ = (
        db.Index("ix_runrec_project_time", "project_id", "executed_at"),
        db.Index("ix_runrec_project_test", "project_id", "test_id"),
        db.Index("ix_runrec_project_version", "project_id", "model_version"),
    )

    id = db.Column(db.Integer, primary_key=True)
    project_id = db.Column(
        db.Integer, db.ForeignKey("lm_projects.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    # Row identity is kept BOTH ways on purpose: ``row_uuid`` survives a row
    # being renamed, ``test_id`` survives a row being deleted and re-created.
    row_uuid = db.Column(db.String(64), nullable=True, index=True)
    test_id = db.Column(db.String(128), nullable=False, default="", index=True)
    # The Task that produced this record (``task_key``, e.g. "T000123").
    task_key = db.Column(db.String(16), nullable=False, default="")
    # Judge verdict exactly as mirrored onto the row (PASS/FAIL/ERROR/...).
    verdict = db.Column(db.String(24), nullable=False, default="")
    # Normalised bucket used by every aggregate, so the dashboard never has to
    # re-implement verdict parsing: pass | fail | error | untestable | cancelled.
    outcome = db.Column(db.String(16), nullable=False, default="", index=True)
    # Model identity at execution time. Denormalised (copied, not FK'd) so
    # renaming or deleting a model can never rewrite history.
    model_name = db.Column(db.String(120), nullable=False, default="")
    model_version = db.Column(db.String(64), nullable=False, default="")
    executor_id = db.Column(db.Integer, db.ForeignKey("lm_users.id"), nullable=True)
    # Display name captured at execution time (a user may be renamed later).
    executor_name = db.Column(db.String(120), nullable=False, default="")
    executed_at = db.Column(db.DateTime, nullable=False, default=_utcnow, index=True)
    # Local calendar date (LM_DISPLAY_TZ) the run is reported under -- the same
    # value written into the row's 実施日 column, kept here so the burn-up chart
    # buckets by the date the user sees rather than by UTC.
    executed_on = db.Column(db.String(10), nullable=False, default="", index=True)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "row_uuid": self.row_uuid,
            "test_id": self.test_id,
            "task_key": self.task_key,
            "verdict": self.verdict,
            "outcome": self.outcome,
            "model_name": self.model_name,
            "model_version": self.model_version,
            "executor_id": self.executor_id,
            "executor_name": self.executor_name,
            "executed_at": _iso(self.executed_at),
            "executed_on": self.executed_on,
        }


# --------------------------------------------------------------------------- #
# In-app notifications
# --------------------------------------------------------------------------- #
# Two events genuinely need to reach a person who is not looking at the page:
# "the run you submitted finished" and "you have been asked to review this".
# Without them a reviewer only discovers assigned work by chance.
#
# Deliberately in-app only (no email/WebSocket): on a LAN tool a bell with a
# count and a 30s poll is the whole requirement, and it has no SMTP dependency,
# no delivery failures and no extra process.
class Notification(db.Model):
    __tablename__ = "lm_notifications"
    __table_args__ = (
        # The unread badge and the dropdown are the only two queries.
        db.Index("ix_notif_user_unread", "user_id", "is_read", "created_at"),
        db.Index("ix_notif_group", "user_id", "group_key", "is_read"),
    )

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(
        db.Integer, db.ForeignKey("lm_users.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    # task.finished | review.assigned | review.approved | review.rejected
    type = db.Column(db.String(32), nullable=False, default="", index=True)
    title = db.Column(db.String(200), nullable=False, default="")
    body = db.Column(db.Text, nullable=False, default="")
    project_id = db.Column(db.Integer, nullable=True, index=True)
    # Where clicking the notification takes the user.
    link_url = db.Column(db.String(500), nullable=False, default="")
    ref_type = db.Column(db.String(32), nullable=False, default="")
    ref_id = db.Column(db.String(64), nullable=False, default="")

    # Collapsing key, defaulting to ``type:project:ref_id`` -- unique per
    # referenced object. Events sharing a key inside LM_NOTIFY_GROUP_SECONDS
    # (0 by default, i.e. never) merge into the newest unread row and ``count``
    # is incremented. Merging is off by default because a merged row carries a
    # single ``link_url``: "×50" announced 50 events and opened one of them.
    group_key = db.Column(db.String(120), nullable=False, default="", index=True)
    count = db.Column(db.Integer, nullable=False, default=1)

    is_read = db.Column(db.Boolean, nullable=False, default=False, index=True)
    # When the row was read. The retention sweep ages rows by THIS, not by
    # ``created_at``: ageing by creation time makes an old notification vanish
    # the instant it is finally read.
    read_at = db.Column(db.DateTime, nullable=True, index=True)
    # Explicitly filed away by the user. Archived rows leave the unread list and
    # the collapsing window, but stay readable in history until the retention
    # window (or an explicit "clear history") removes them.
    archived_at = db.Column(db.DateTime, nullable=True, index=True)
    created_at = db.Column(db.DateTime, nullable=False, default=_utcnow, index=True)
    updated_at = db.Column(db.DateTime, nullable=False, default=_utcnow,
                           onupdate=_utcnow)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "type": self.type,
            "title": self.title,
            "body": self.body,
            "project_id": self.project_id,
            "link_url": self.link_url,
            "ref_type": self.ref_type,
            "ref_id": self.ref_id,
            "count": self.count or 1,
            "is_read": bool(self.is_read),
            "archived": self.archived_at is not None,
            "read_at": _iso(self.read_at),
            "archived_at": _iso(self.archived_at),
            "created_at": _iso(self.created_at),
            "updated_at": _iso(self.updated_at),
        }


# --------------------------------------------------------------------------- #
# Server -> CRDT row write-back queue
# --------------------------------------------------------------------------- #
# The worker process finalises a run and needs to publish the verdict onto the
# matching row. When the project is in collaborative mode the ``Y.Doc`` -- not
# the database -- is the authoritative copy of a row, and the materializer
# reconciles Doc -> DB in one direction only. A direct DB write would therefore
# be silently reverted by the next flush.
#
# The worker cannot touch the Y.Doc itself (it lives in the separate collab
# process), so it leaves the intent here instead. The collab server drains this
# queue for its live rooms and applies each payload into the Y.Doc inside the
# materializer's ``suppressed()`` block. Rows for projects that are not
# collaborative are never queued at all -- the worker writes them straight to
# the database.
class RowWriteback(db.Model):
    __tablename__ = "lm_row_writebacks"
    __table_args__ = (
        db.Index("ix_writeback_pending", "project_id", "applied_at"),
    )

    id = db.Column(db.Integer, primary_key=True)
    project_id = db.Column(
        db.Integer, db.ForeignKey("lm_projects.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    sheet = db.Column(db.String(16), nullable=False, default="test")
    row_uuid = db.Column(db.String(64), nullable=False, default="")
    # {field_key: value} -- always primitives, never nested structures.
    payload = db.Column(JSONType, nullable=True)
    created_at = db.Column(db.DateTime, nullable=False, default=_utcnow, index=True)
    # NULL while pending; set once the collab server has applied it to the Doc.
    applied_at = db.Column(db.DateTime, nullable=True)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "project_id": self.project_id,
            "sheet": self.sheet,
            "row_uuid": self.row_uuid,
            "payload": dict(self.payload or {}),
            "created_at": _iso(self.created_at),
            "applied_at": _iso(self.applied_at),
        }


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
