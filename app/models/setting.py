"""Key/value settings table.

Holds runtime-adjustable values that must be shared between the web and worker
processes -- most importantly the license limit and the live in-use counter
that together form the cross-process license gate.
"""

from __future__ import annotations

from ..extensions import db


class Setting(db.Model):
    __tablename__ = "app_settings"

    key = db.Column(db.String(64), primary_key=True)
    value = db.Column(db.String(255), nullable=False, default="")

    # Well-known keys.
    LICENSE_LIMIT = "license_limit"
    LICENSE_INUSE = "license_inuse"
    LICENSE_DRAINING = "license_draining"
    def __repr__(self) -> str:  # pragma: no cover
        return f"<Setting {self.key}={self.value}>"
