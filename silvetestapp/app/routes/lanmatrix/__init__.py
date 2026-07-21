"""LAN Test Matrix ``/api/v1`` blueprints (split from the former God module)."""

from .auth import bp as auth_bp
from .projects_items import bp as projects_bp
from .tasks import bp as tasks_bp
from .admin_db import bp as admin_db_bp
from .admin_console import bp as admin_console_bp

BLUEPRINTS = (auth_bp, projects_bp, tasks_bp, admin_db_bp, admin_console_bp)

__all__ = [
    "auth_bp", "projects_bp", "tasks_bp", "admin_db_bp", "admin_console_bp",
    "BLUEPRINTS",
]
