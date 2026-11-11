"""Personal API tokens: non-interactive credentials for the CLI and agents."""

from __future__ import annotations

import datetime as _dt
from typing import Optional

from ..extensions import db


def _iso(value: Optional[_dt.datetime]) -> Optional[str]:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=_dt.timezone.utc)
    return value.isoformat().replace("+00:00", "Z")


class ApiToken(db.Model):
    """A bearer credential acting as its owner. Only a SHA-256 of the secret is stored."""

    __tablename__ = "lm_api_tokens"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("lm_users.id", ondelete="CASCADE"),
                        nullable=False, index=True)
    name = db.Column(db.String(64), nullable=False)
    token_hash = db.Column(db.String(64), nullable=False, unique=True, index=True)
    prefix = db.Column(db.String(16), nullable=False)
    created_at = db.Column(db.DateTime(timezone=True), nullable=False)
    expires_at = db.Column(db.DateTime(timezone=True), nullable=True)
    last_used_at = db.Column(db.DateTime(timezone=True), nullable=True)
    revoked_at = db.Column(db.DateTime(timezone=True), nullable=True)

    def to_dict(self) -> dict:
        return {"id": self.id, "name": self.name, "prefix": self.prefix,
                "created_at": _iso(self.created_at), "expires_at": _iso(self.expires_at),
                "last_used_at": _iso(self.last_used_at), "revoked_at": _iso(self.revoked_at)}
