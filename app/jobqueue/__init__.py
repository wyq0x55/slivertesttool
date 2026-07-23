"""Background task queue (Huey / SqlHuey on PostgreSQL).

Named ``jobqueue`` rather than ``queue`` to avoid shadowing the Python standard
library ``queue`` module.
"""

from __future__ import annotations

from .huey_app import huey

__all__ = ["huey"]
