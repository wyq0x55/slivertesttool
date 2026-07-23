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
    # Original file name of the admin-configured .sil model (display only; the
    # file itself is stored under Config.MODEL_DIR as ``active.sil``).
    # Retained for backward compatibility with older single-model deployments.
    SIL_MODEL_NAME = "sil_model_name"
    # JSON array of admin-registered server-side ``.sil`` model paths, e.g.
    # ``[{"name": "PlantA", "path": "/opt/models/plant_a.sil"}, ...]``. Testers
    # pick one of these by name when submitting; the file is never uploaded.
    SIL_MODELS = "sil_models"

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Setting {self.key}={self.value}>"
