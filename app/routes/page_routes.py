"""Legacy HTML page routes (redirects only).

The standalone submit / tasks / admin pages have been folded into the LAN Test
Matrix (lanmatrix): Upload Tasks are now **per-project** (members only) and fully
featured — folder upload, live log streaming, and judge-result viewing — while
administration is a session-gated console for System Administrators (no
``ADMIN_TOKEN``). These legacy routes remain only as redirects so old bookmarks
keep working and the previously-unscoped ``/tasks`` list can no longer leak
cross-project tasks.
"""

from __future__ import annotations

from flask import Blueprint, redirect, url_for

page_bp = Blueprint("pages", __name__)


@page_bp.get("/")
def index():
    return redirect(url_for("lanmatrix_pages.projects"))


@page_bp.get("/tasks")
def task_list():
    return redirect(url_for("lanmatrix_pages.projects"))


@page_bp.get("/tasks/<task_key>")
def task_detail(task_key: str):
    return redirect(url_for("lanmatrix_pages.projects"))


@page_bp.get("/admin")
def admin():
    return redirect(url_for("lanmatrix_pages.admin_console"))
