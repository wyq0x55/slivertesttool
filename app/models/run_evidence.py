"""Trusted digests for filesystem evidence, committed with the task attempt."""

from ..extensions import db


class RunEvidence(db.Model):
    __tablename__ = "run_evidence"

    task_id = db.Column(db.Integer, db.ForeignKey("tasks.id", ondelete="CASCADE"), primary_key=True)
    run_count = db.Column(db.Integer, primary_key=True)
    manifest_sha256 = db.Column(db.String(64), nullable=False)
    outcome_sha256 = db.Column(db.String(64), nullable=False, default="")
    finalised = db.Column(db.Boolean, nullable=False, default=False)
