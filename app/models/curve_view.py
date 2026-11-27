"""Saved variable-curve layouts, shared by everyone in a project."""

from __future__ import annotations

import datetime as _dt

from ..extensions import db


def _utcnow() -> _dt.datetime:
    return _dt.datetime.now(_dt.timezone.utc)


class CurveView(db.Model):
    """A named signal set + lane layout for one test case in its book.

    Lanes are stored by variable *name* (CSV header), not by column position,
    so a view keeps working across attempts whose signal lists differ.
    """

    __tablename__ = "curve_views"

    id = db.Column(db.Integer, primary_key=True)
    project_id = db.Column(db.Integer, db.ForeignKey("lm_projects.id", ondelete="CASCADE"),
                           nullable=False, index=True)
    book_id = db.Column(db.Integer, nullable=True)
    test_id = db.Column(db.String(128), nullable=False, index=True)
    name = db.Column(db.String(80), nullable=False)
    layout = db.Column(db.JSON, nullable=False)
    created_by = db.Column(db.Integer, db.ForeignKey("lm_users.id", ondelete="SET NULL"))
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=_utcnow)
    updated_at = db.Column(db.DateTime(timezone=True), nullable=False, default=_utcnow, onupdate=_utcnow)

    __table_args__ = (
        db.Index("uq_curve_view_name", project_id, test_id, name, unique=True,
                 postgresql_where=book_id.is_(None), sqlite_where=book_id.is_(None)),
        db.Index("uq_curve_view_book_name", project_id, book_id, test_id, name, unique=True,
                 postgresql_where=book_id.is_not(None), sqlite_where=book_id.is_not(None)),
    )

    def to_dict(self) -> dict:
        return {"id": self.id, "test_id": self.test_id, "name": self.name, "layout": self.layout,
                "created_by": self.created_by,
                "updated_at": self.updated_at.isoformat() if self.updated_at else None}
