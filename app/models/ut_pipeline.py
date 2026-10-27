"""Unit-test pipeline models (ut-agent generation → LLVM → check → repair → review).

One project binds to one ut-agent ProjectManifest and index CSV
(:class:`UtPipelineConfig`).  The index rows form the 実施一覧
(:class:`UtTarget`).  Selected targets are queued as a :class:`UtBatch`; each
target in it becomes a :class:`UtJob` that walks the state machine in
:mod:`app.services.ut_pipeline.pipeline`.  Every CSV a job ever executed — from
the generator, the local LLM or a human — is an immutable :class:`UtAttempt`
with its origin, execution evidence and machine verdict, so a reviewer sees
exactly what produced the candidate being approved.
"""

from __future__ import annotations

from .lanmatrix import JSONType, _iso, _utcnow
from ..extensions import db


class UtPipelineConfig(db.Model):
    __tablename__ = "ut_pipeline_configs"

    id = db.Column(db.Integer, primary_key=True)
    project_id = db.Column(
        db.Integer, db.ForeignKey("lm_projects.id", ondelete="CASCADE"),
        nullable=False, unique=True,
    )
    # ut-agent ProjectManifest (locks the explicit test policy / interface contract).
    manifest_path = db.Column(db.String(1024), nullable=False, default="")
    # Five-column project index (HOYU/<project>.csv) and the Soft/Product root.
    index_csv = db.Column(db.String(1024), nullable=False, default="")
    product_root = db.Column(db.String(1024), nullable=False, default="")
    execution_backend = db.Column(db.String(16), nullable=False, default="clang")
    stub_mode = db.Column(db.String(16), nullable=False, default="deterministic")
    stub_model = db.Column(db.String(128), nullable=False, default="")
    stub_max_rounds = db.Column(db.Integer, nullable=False, default=2)
    host_clang = db.Column(db.String(1024), nullable=False, default="clang")
    host_llvm_cov = db.Column(db.String(1024), nullable=False, default="llvm-cov")
    host_llvm_profdata = db.Column(db.String(1024), nullable=False, default="llvm-profdata")
    host_profile_runtime = db.Column(db.String(1024), nullable=False, default="")
    max_repair_rounds = db.Column(db.Integer, nullable=False, default=5)
    # Optional model override for repair; empty uses the global AI model.
    repair_model = db.Column(db.String(128), nullable=False, default="")
    updated_by = db.Column(db.Integer, db.ForeignKey("lm_users.id", ondelete="SET NULL"), nullable=True)
    updated_at = db.Column(db.DateTime, nullable=False, default=_utcnow, onupdate=_utcnow)

    def to_dict(self) -> dict:
        return {
            "project_id": self.project_id,
            "manifest_path": self.manifest_path,
            "index_csv": self.index_csv,
            "product_root": self.product_root,
            "execution_backend": self.execution_backend,
            "stub_mode": self.stub_mode,
            "stub_model": self.stub_model,
            "stub_max_rounds": self.stub_max_rounds,
            "host_clang": self.host_clang,
            "host_llvm_cov": self.host_llvm_cov,
            "host_llvm_profdata": self.host_llvm_profdata,
            "host_profile_runtime": self.host_profile_runtime,
            "max_repair_rounds": self.max_repair_rounds,
            "repair_model": self.repair_model,
            "updated_at": _iso(self.updated_at),
        }


class UtTarget(db.Model):
    """One function row of the 実施一覧, imported from the project index."""

    __tablename__ = "ut_targets"
    __table_args__ = (
        db.UniqueConstraint("project_id", "source_rel", "function", name="uq_ut_target"),
    )

    id = db.Column(db.Integer, primary_key=True)
    project_id = db.Column(
        db.Integer, db.ForeignKey("lm_projects.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    row_no = db.Column(db.Integer, nullable=False)
    callcnt = db.Column(db.Integer, nullable=False, default=0)
    source_name = db.Column(db.String(256), nullable=False)
    function = db.Column(db.String(256), nullable=False)
    source_rel = db.Column(db.String(1024), nullable=False)
    unit_rel = db.Column(db.String(1024), nullable=False)
    # False once a later import no longer lists the row; history is kept.
    in_index = db.Column(db.Boolean, nullable=False, default=True)
    imported_at = db.Column(db.DateTime, nullable=False, default=_utcnow)
    latest_job_id = db.Column(db.Integer, nullable=True)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "row_no": self.row_no,
            "callcnt": self.callcnt,
            "source_name": self.source_name,
            "function": self.function,
            "source_rel": self.source_rel,
            "unit_rel": self.unit_rel,
            "in_index": self.in_index,
            "imported_at": _iso(self.imported_at),
            "latest_job_id": self.latest_job_id,
        }


class UtBatch(db.Model):
    __tablename__ = "ut_batches"

    STATUS_GENERATING = "generating"
    STATUS_RUNNING = "running"
    STATUS_DONE = "done"
    STATUS_FAILED = "failed"

    id = db.Column(db.Integer, primary_key=True)
    project_id = db.Column(
        db.Integer, db.ForeignKey("lm_projects.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    label = db.Column(db.String(128), nullable=False, default="")
    status = db.Column(db.String(16), nullable=False, default=STATUS_GENERATING)
    # Frozen config snapshot + generator provenance + constraint pack.
    config_snapshot = db.Column(JSONType, nullable=True)
    provenance = db.Column(JSONType, nullable=True)
    constraints = db.Column(JSONType, nullable=True)
    error = db.Column(db.Text, nullable=False, default="")
    created_by = db.Column(db.Integer, db.ForeignKey("lm_users.id", ondelete="SET NULL"), nullable=True)
    created_at = db.Column(db.DateTime, nullable=False, default=_utcnow)
    finished_at = db.Column(db.DateTime, nullable=True)

    def to_dict(self) -> dict:
        snapshot = self.constraints or {}
        keys = {"coverage_policy", "test_rules_ref", "effective_test_policy", "test_policy",
                "test_policy_sha256"}
        return {
            "id": self.id,
            "project_id": self.project_id,
            "label": self.label,
            "status": self.status,
            "provenance": self.provenance,
            "constraints": {key: value for key, value in snapshot.items() if key in keys},
            "error": self.error or None,
            "created_by": self.created_by,
            "created_at": _iso(self.created_at),
            "finished_at": _iso(self.finished_at),
        }


class GateCode:
    """Why a job stopped at a human gate; the reason text says the details.

    The client maps each code to the next step a person should take, so a
    new code needs a hint in ut_common.js.
    """

    MACHINE_PASS = "MACHINE_PASS"                    # awaiting_review
    OPEN_GAPS_EXHAUSTED = "OPEN_GAPS_EXHAUSTED"      # PASS, missing cases open, no rounds left
    ROUNDS_EXHAUSTED = "ROUNDS_EXHAUSTED"
    NOT_ROW_FIXABLE = "NOT_ROW_FIXABLE"              # expected values / format: rows cannot help
    HUMAN_REVISION_FAILED = "HUMAN_REVISION_FAILED"
    CANDIDATE_REJECTED = "CANDIDATE_REJECTED"        # model rows refused before execution
    MODEL_STALLED = "MODEL_STALLED"                  # model proposed nothing; input unchanged
    CHECK_BLOCKED = "CHECK_BLOCKED"
    CONFIGURATION_CONFLICT = "CONFIGURATION_CONFLICT"
    REPORT_INCOMPLETE = "REPORT_INCOMPLETE"
    INTEGRITY_ERROR = "INTEGRITY_ERROR"
    UNKNOWN_VERDICT = "UNKNOWN_VERDICT"
    NO_TESTCSV = "NO_TESTCSV"
    PROMPT_PREFIX_CHANGED = "PROMPT_PREFIX_CHANGED"
    PROMPT_BUDGET_EXCEEDED = "PROMPT_BUDGET_EXCEEDED"
    ENV_BLOCKED = "ENV_BLOCKED"                      # blocked
    GENERATION_FAILED = "GENERATION_FAILED"          # error ...
    TOOL_ERROR = "TOOL_ERROR"
    MODEL_UNAVAILABLE = "MODEL_UNAVAILABLE"
    INTERNAL_ERROR = "INTERNAL_ERROR"
    INTERRUPTED = "INTERRUPTED"
    STOPPED = "STOPPED"                              # stopped


class UtJob(db.Model):
    """One target inside one batch; owns the pipeline state."""

    __tablename__ = "ut_jobs"

    QUEUED = "queued"
    GENERATING = "generating"
    EXECUTING = "executing"
    REPAIRING = "repairing"
    AWAITING_REVIEW = "awaiting_review"
    NEEDS_HUMAN = "needs_human"
    BLOCKED = "blocked"
    ERROR = "error"
    APPROVED = "approved"
    REJECTED = "rejected"
    # Halted on request.  Keeps its candidate and evidence and accepts every
    # human action, but waits on nobody, so it is outside the pending queues.
    STOPPED = "stopped"

    ACTIVE = (QUEUED, GENERATING, EXECUTING, REPAIRING)
    # States a human must act on.  Every machine outcome lands in one of these.
    HUMAN_GATES = (AWAITING_REVIEW, NEEDS_HUMAN, BLOCKED, ERROR)
    # States that accept revision / review / retry.
    ACTIONABLE = HUMAN_GATES + (STOPPED,)
    FINAL = (APPROVED, REJECTED)

    id = db.Column(db.Integer, primary_key=True)
    batch_id = db.Column(
        db.Integer, db.ForeignKey("ut_batches.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    project_id = db.Column(
        db.Integer, db.ForeignKey("lm_projects.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    target_id = db.Column(
        db.Integer, db.ForeignKey("ut_targets.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    state = db.Column(db.String(24), nullable=False, default=QUEUED, index=True)
    # LLM repair rounds consumed / allowed (snapshot of the project config).
    repair_rounds = db.Column(db.Integer, nullable=False, default=0)
    max_repair_rounds = db.Column(db.Integer, nullable=False, default=5)
    generation_status = db.Column(db.String(32), nullable=False, default="")
    # Generator TestCsv; the baseline reference every revision is checked against.
    reference_csv = db.Column(db.String(1024), nullable=False, default="")
    generation = db.Column(JSONType, nullable=True)
    current_attempt_id = db.Column(db.Integer, nullable=True)
    last_verdict = db.Column(db.String(16), nullable=False, default="")
    # Why the job stopped at a human gate (rounds exhausted, blocked, error...).
    reason = db.Column(db.Text, nullable=False, default="")
    # The same as a GateCode, for routing the next step; empty off a gate.
    gate_code = db.Column(db.String(32), nullable=False, default="")
    created_at = db.Column(db.DateTime, nullable=False, default=_utcnow)
    updated_at = db.Column(db.DateTime, nullable=False, default=_utcnow, onupdate=_utcnow)
    reviewed_by = db.Column(db.Integer, db.ForeignKey("lm_users.id", ondelete="SET NULL"), nullable=True)
    reviewed_at = db.Column(db.DateTime, nullable=True)
    review_note = db.Column(db.Text, nullable=False, default="")
    # A stop request for a job whose worker is inside an external call
    # (generation, LLVM execution, model).  The worker honours it at the next step
    # boundary, so the running call finishes and its evidence is recorded.
    stop_requested_at = db.Column(db.DateTime, nullable=True)
    stop_requested_by = db.Column(db.Integer, db.ForeignKey("lm_users.id", ondelete="SET NULL"), nullable=True)

    target = db.relationship("UtTarget", lazy="select")

    @property
    def superseded_by(self) -> int | None:
        """The newer job of the same target, when this one is no longer its latest."""
        latest = self.target.latest_job_id if self.target else None
        return latest if latest and latest != self.id else None

    def to_dict(self, *, attempts: list | None = None) -> dict:
        entry = {
            "id": self.id,
            "batch_id": self.batch_id,
            "project_id": self.project_id,
            "target": self.target.to_dict() if self.target else None,
            "state": self.state,
            "repair_rounds": self.repair_rounds,
            "max_repair_rounds": self.max_repair_rounds,
            "generation_status": self.generation_status,
            "execution_backend": "clang",
            "has_reference": bool(self.reference_csv),
            "current_attempt_id": self.current_attempt_id,
            "last_verdict": self.last_verdict or None,
            "reason": self.reason or None,
            "gate_code": self.gate_code or None,
            "created_at": _iso(self.created_at),
            "updated_at": _iso(self.updated_at),
            "reviewed_by": self.reviewed_by,
            "reviewed_at": _iso(self.reviewed_at),
            "review_note": self.review_note,
            "stop_requested": self.stop_requested_at is not None and self.state in self.ACTIVE,
            "superseded_by": self.superseded_by,
        }
        if attempts is not None:
            entry["generation"] = self.generation
            entry["attempts"] = [attempt.to_dict() for attempt in attempts]
        return entry


class UtAttempt(db.Model):
    """One immutable candidate CSV and what the machine said about it."""

    __tablename__ = "ut_attempts"
    __table_args__ = (db.UniqueConstraint("job_id", "seq", name="uq_ut_attempt_seq"),)

    ORIGIN_GENERATOR = "GENERATOR"
    ORIGIN_LLM = "LLM"
    ORIGIN_HUMAN = "HUMAN"
    # Appended outputs proven by the typed implementation oracle, then re-executed.
    ORIGIN_HOST_DERIVED = "HOST_DERIVED"

    id = db.Column(db.Integer, primary_key=True)
    job_id = db.Column(
        db.Integer, db.ForeignKey("ut_jobs.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    seq = db.Column(db.Integer, nullable=False)
    origin = db.Column(db.String(16), nullable=False)
    # Empty when the reviser produced nothing executable (recorded, not run).
    csv_path = db.Column(db.String(1024), nullable=False, default="")
    csv_sha256 = db.Column(db.String(64), nullable=False, default="")
    execution = db.Column(JSONType, nullable=True)
    check = db.Column(JSONType, nullable=True)
    verdict = db.Column(db.String(16), nullable=False, default="")
    # LLM: model, usage, prompt digest, rationale and declared changes.
    reviser = db.Column(JSONType, nullable=True)
    note = db.Column(db.Text, nullable=False, default="")
    created_by = db.Column(db.Integer, db.ForeignKey("lm_users.id", ondelete="SET NULL"), nullable=True)
    created_at = db.Column(db.DateTime, nullable=False, default=_utcnow)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "seq": self.seq,
            "origin": self.origin,
            "csv_sha256": self.csv_sha256 or None,
            "has_csv": bool(self.csv_path),
            "verdict": self.verdict or None,
            "check": self.check,
            "execution": _execution_summary(self.execution),
            "reviser": self.reviser,
            "note": self.note,
            "created_by": self.created_by,
            "created_at": _iso(self.created_at),
        }


def _execution_summary(execution: dict | None) -> dict | None:
    """Drop the bulky per-cell execution detail; the check already lists conflicts."""
    if not execution:
        return None
    keep = ("status", "exit_code", "diagnostic", "blocked_reasons", "coverage", "sandbox",
            "evidence_source", "counts_as_target_evidence", "delivery_validated", "reason",
            "stub_generation")
    summary = {key: execution.get(key) for key in keep if key in execution}
    # The per-line report itself is fetched on demand (it carries source text).
    summary["has_coverage_report"] = isinstance(execution.get("coverage_report"), dict)
    return summary
