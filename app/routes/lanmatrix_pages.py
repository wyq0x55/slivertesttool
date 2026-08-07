"""HTML page routes for the LAN Test Matrix editor.

These render thin shells; all data flows through ``/api/v1`` via fetch so the
same server-side authority applies. Auth state lives in the session cookie
(HttpOnly, SameSite=Lax).
"""

from __future__ import annotations

import os

from flask import (Blueprint, current_app, redirect, render_template, request,
                   session, url_for)

from ..services.lanmatrix import permissions, service, settings

pages_bp = Blueprint(
    "lanmatrix_pages", __name__, url_prefix="/lanmatrix",
    template_folder="../templates",
)


def _current_user():
    uid = session.get("lm_user_id")
    if uid is None:
        return None
    user = service.get_user(uid)
    return user if (user and user.is_active) else None


@pages_bp.get("/login")
def login():
    if _current_user() is not None:
        return redirect(url_for("lanmatrix_pages.projects"))
    return render_template(
        "lanmatrix/login.html", allow_registration=settings.ALLOW_REGISTRATION)


@pages_bp.get("/register")
def register():
    """Self-service registration page for LAN users."""
    if _current_user() is not None:
        return redirect(url_for("lanmatrix_pages.projects"))
    if not settings.ALLOW_REGISTRATION:
        return redirect(url_for("lanmatrix_pages.login"))
    return render_template(
        "lanmatrix/register.html",
        password_min_len=settings.PASSWORD_MIN_LEN,
        needs_approval=(settings.REGISTRATION_DEFAULT_STATUS != "active"),
    )


@pages_bp.get("/logout")
def logout():
    """Convenience GET logout for links in the shared navigation."""
    session.clear()
    return redirect(url_for("lanmatrix_pages.login"))


@pages_bp.get("/")
def home():
    if _current_user() is None:
        return redirect(url_for("lanmatrix_pages.login"))
    return redirect(url_for("lanmatrix_pages.workspace"))


@pages_bp.get("/home")
def workspace():
    """Cross-project workspace — the post-login landing page.

    Previously login dropped straight into the project *list*, which answers
    "what projects exist" but not "what needs my attention", so the user had to
    pick a project and drill in before seeing anything actionable. This page
    aggregates across every project the user can see.
    """
    user = _current_user()
    if user is None:
        return redirect(url_for("lanmatrix_pages.login"))
    return render_template("lanmatrix/home.html", user=user.to_dict())


@pages_bp.get("/admin/db")
def admin_db():
    """PostgreSQL management console — bootstrap admin account only."""
    user = _current_user()
    if user is None:
        return redirect(url_for("lanmatrix_pages.login"))
    if not user.is_bootstrap_admin:
        return redirect(url_for("lanmatrix_pages.projects"))
    return render_template("lanmatrix/admin_db.html", user=user.to_dict())


@pages_bp.get("/admin")
def admin_console():
    """System administration console (accounts, models, license, tasks).

    Replaces the ADMIN_TOKEN-gated admin page: authority now comes from the
    logged-in System Administrator account, so no separate token is needed.
    """
    user = _current_user()
    if user is None:
        return redirect(url_for("lanmatrix_pages.login"))
    if not user.is_system_admin:
        return redirect(url_for("lanmatrix_pages.projects"))
    return render_template("lanmatrix/admin.html", user=user.to_dict())


@pages_bp.get("/projects")
def projects():
    user = _current_user()
    if user is None:
        return redirect(url_for("lanmatrix_pages.login"))
    return render_template("lanmatrix/projects.html", user=user.to_dict())


def _univer_bundle_available() -> bool:
    """True when the Vite-built Univer Sheets bundle has been vendored.

    Univer Sheets is the primary editing engine for the Test Matrix (spreadsheet
    copy/paste, block paste, batch cell fill). When the bundle is present the
    editor loads it; when it is missing, grid.js shows a banner and degrades to
    the built-in offline grid so the app is never bricked. Build it with
    ``cd frontend && npm install && npm run build``.
    """
    bundle = os.path.join(
        current_app.root_path, "static", "vendor", "univer", "univer.full.umd.js")
    return os.path.isfile(bundle)


def _collab_bundle_available() -> bool:
    """True when the Vite-built Yjs collaboration bundle has been vendored.

    Gates the optional real-time (multi-user) editing layer. When present the
    editor loads it and, if it can also reach the collab WebSocket server
    (``run_collab.py``) and mint a room token, drives edits through the shared
    Y.Doc; otherwise it silently stays on the classic REST + polling path. Build
    it with ``cd frontend && npm install && npm run build`` (or ``build:collab``).
    """
    bundle = os.path.join(
        current_app.root_path, "static", "vendor", "collab", "collab.umd.js")
    return os.path.isfile(bundle)


@pages_bp.get("/projects/<int:project_id>")
def editor(project_id: int):
    user = _current_user()
    if user is None:
        return redirect(url_for("lanmatrix_pages.login"))
    return render_template(
        "lanmatrix/editor.html", user=user.to_dict(), project_id=project_id,
        univer_available=_univer_bundle_available(),
        collab_available=_collab_bundle_available())


@pages_bp.get("/projects/<int:project_id>/tasks")
def project_tasks(project_id: int):
    """Per-project Upload Tasks page (test execution). Members only; the API
    enforces membership, the page is a thin shell."""
    user = _current_user()
    if user is None:
        return redirect(url_for("lanmatrix_pages.login"))
    return render_template(
        "lanmatrix/project_tasks.html", user=user.to_dict(),
        project_id=project_id,
        univer_available=_univer_bundle_available())


@pages_bp.get("/projects/<int:project_id>/settings")
def project_settings(project_id: int):
    """Entry point for the project settings area.

    The sidebar links here rather than to a specific tab so that the rail
    carries one 设置 destination instead of five siblings. There is no page of
    its own: an empty landing screen whose only content is the tab strip would
    cost every user an extra click, so this redirects to the first tab.
    """
    user = _current_user()
    if user is None:
        return redirect(url_for("lanmatrix_pages.login"))
    return redirect(url_for("lanmatrix_pages.fields", project_id=project_id))


@pages_bp.get("/projects/<int:project_id>/members")
def members(project_id: int):
    user = _current_user()
    if user is None:
        return redirect(url_for("lanmatrix_pages.login"))
    return render_template(
        "lanmatrix/members.html", user=user.to_dict(), project_id=project_id)


@pages_bp.get("/projects/<int:project_id>/fields")
def fields(project_id: int):
    user = _current_user()
    if user is None:
        return redirect(url_for("lanmatrix_pages.login"))
    return render_template(
        "lanmatrix/fields.html", user=user.to_dict(), project_id=project_id)


@pages_bp.get("/projects/<int:project_id>/models")
def models_page(project_id: int):
    user = _current_user()
    if user is None:
        return redirect(url_for("lanmatrix_pages.login"))
    return render_template(
        "lanmatrix/models.html", user=user.to_dict(), project_id=project_id)


@pages_bp.get("/projects/<int:project_id>/audit")
def audit_page(project_id: int):
    user = _current_user()
    if user is None:
        return redirect(url_for("lanmatrix_pages.login"))
    return render_template(
        "lanmatrix/audit.html", user=user.to_dict(), project_id=project_id)


@pages_bp.get("/projects/<int:project_id>/trash")
def trash_page(project_id: int):
    user = _current_user()
    if user is None:
        return redirect(url_for("lanmatrix_pages.login"))
    # The capabilities are resolved here rather than guessed in JavaScript: the
    # API enforces them regardless, and a "彻底删除" button that always 403s is
    # worse than no button at all.
    role = service.role_in_project(project_id, user)
    admin = user.is_system_admin
    return render_template(
        "lanmatrix/trash.html", user=user.to_dict(), project_id=project_id,
        can_restore=permissions.can("trash.restore", role,
                                    is_system_admin=admin),
        can_purge=permissions.can("trash.purge", role, is_system_admin=admin))
