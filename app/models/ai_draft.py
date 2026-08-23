"""AI generation drafts (人工只审核的落点).

Every AI scenario produces *drafts*, never direct writes: a draft stores the
model's output together with what it was generated from, waits in
``pending`` for a human decision, and only ``approve`` applies it through the
same service layer the web editor uses. ``rejected`` drafts are kept for
audit / prompt tuning.

Scenarios (``scenario`` values):
    viewpoint  设计书 → 测试观点行（正例/反例/排列组合展开）
    procedure  观点行 + 源码上下文 → 测试手顺 steps JSON
    sbs        源码 + 既有 SBS → SBS 草稿 / 增量补变量
    lib        人工提议 + 被标注手顺 → lib 共通函数 + 手顺改写
    failure    失败用例日志 + 观点 → 差异分析草稿（挂 CellComment）
"""

from __future__ import annotations

from ..extensions import db


def _utcnow():  # keep the same convention as the other lanmatrix models
    import datetime
    return datetime.datetime.utcnow()


class AiDraft(db.Model):
    __tablename__ = "lm_ai_drafts"
    __table_args__ = (
        db.Index("ix_ai_draft_project_scenario", "project_id", "scenario"),
        db.Index("ix_ai_draft_status", "status"),
    )

    SCENARIOS = ("viewpoint", "procedure", "sbs", "lib", "failure")
    STATUS_PENDING = "pending"
    STATUS_APPROVED = "approved"
    STATUS_REJECTED = "rejected"
    STATUS_ERROR = "error"

    id = db.Column(db.Integer, primary_key=True)
    project_id = db.Column(
        db.Integer, db.ForeignKey("lm_projects.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    scenario = db.Column(db.String(24), nullable=False)

    # pending | approved | rejected | error. ``error`` means generation itself
    # failed (provider unreachable / retries exhausted); it is terminal and the
    # caller simply retries with a new draft.
    status = db.Column(db.String(16), nullable=False, default="pending", index=True)
    # What the scenario was invoked with (source snippets, item ids, options).
    # Kept verbatim so a reviewer can see what the model actually saw.
    input_json = db.Column(db.Text, nullable=False, default="")
    # The model's validated output (already schema-checked; JSON-encoded).
    output_json = db.Column(db.Text, nullable=False, default="")
    # Generation log: provider, model, per-round validation feedback. Review
    # trust comes from the machine validation record, not just the result.
    meta_json = db.Column(db.Text, nullable=False, default="")
    error = db.Column(db.Text, nullable=False, default="")

    created_by = db.Column(db.Integer, db.ForeignKey("lm_users.id"), nullable=True)
    created_at = db.Column(db.DateTime, nullable=False, default=_utcnow, index=True)
    reviewed_by = db.Column(db.Integer, db.ForeignKey("lm_users.id"), nullable=True)
    reviewed_at = db.Column(db.DateTime, nullable=True)
    review_note = db.Column(db.Text, nullable=False, default="")
    # What applying produced (created item ids etc.) for traceability.
    applied_result_json = db.Column(db.Text, nullable=False, default="")

    def to_dict(self, *, include_payload: bool = True) -> dict:
        import json as _json

        def _load(raw: str):
            try:
                return _json.loads(raw) if raw else None
            except ValueError:
                return raw

        entry = {
            "id": self.id,
            "project_id": self.project_id,
            "scenario": self.scenario,
            "status": self.status,
            # The summary (list view) omits payloads but keeps a truncated
            # error so a failed generation is visible without fetching each
            # draft — an error the reviewer must open one-by-one to see is an
            # error nobody sees.
            "error": (self.error[:200] + "…") if len(self.error) > 200 else (self.error or None),
            "created_by": self.created_by,
            "created_at": _utcnow_iso(self.created_at),
            "reviewed_by": self.reviewed_by,
            "reviewed_at": _utcnow_iso(self.reviewed_at),
            "review_note": self.review_note,
        }
        if include_payload:
            entry["input"] = _load(self.input_json)
            entry["output"] = _load(self.output_json)
            entry["meta"] = _load(self.meta_json)
            entry["applied_result"] = _load(self.applied_result_json)
        return entry


def _utcnow_iso(value) -> str:
    if value is None:
        return ""
    return value.replace(microsecond=0).isoformat() + "Z"
