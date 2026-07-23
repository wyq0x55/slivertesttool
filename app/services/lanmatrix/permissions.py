"""RBAC permission checks (PRD §5). Server-side authority; the UI only hides.

Roles: system_admin (global), plus per-project roles project_admin / editor /
reviewer / reader. Permissions are coarse capability flags checked by the API
before any state change.
"""

from __future__ import annotations

from typing import Optional

# Capability -> set of project roles allowed (system_admin always allowed).
_MATRIX: dict[str, frozenset[str]] = {
    "project.view": frozenset({"project_admin", "editor", "reviewer", "reader"}),
    "project.edit": frozenset({"project_admin"}),
    "project.freeze": frozenset({"project_admin"}),
    "project.members": frozenset({"project_admin"}),
    "field.manage": frozenset({"project_admin"}),
    "model.manage": frozenset({"project_admin"}),
    "item.view": frozenset({"project_admin", "editor", "reviewer", "reader"}),
    "item.create": frozenset({"project_admin", "editor"}),
    "item.edit": frozenset({"project_admin", "editor"}),
    "item.delete": frozenset({"project_admin", "editor"}),
    "item.batch": frozenset({"project_admin", "editor"}),
    "item.batch_all": frozenset({"project_admin"}),
    "item.review": frozenset({"project_admin", "reviewer"}),
    "comment.add": frozenset({"project_admin", "editor", "reviewer"}),
    "import.run": frozenset({"project_admin", "editor"}),
    "import.replace": frozenset({"project_admin"}),
    "export.run": frozenset({"project_admin", "editor", "reviewer", "reader"}),
    "audit.view": frozenset({"project_admin"}),
    # Test-execution tasks (Upload Tasks). Membership of the project is what
    # matters: any member may view, upload/run, cancel and download; only a
    # project admin may delete other members' tasks.
    "task.view": frozenset({"project_admin", "editor", "reviewer", "reader"}),
    "task.upload": frozenset({"project_admin", "editor", "reviewer", "reader"}),
    "task.cancel": frozenset({"project_admin", "editor", "reviewer", "reader"}),
    "task.download": frozenset({"project_admin", "editor", "reviewer", "reader"}),
    "task.delete": frozenset({"project_admin"}),
}


class PermissionDenied(Exception):
    """Raised when the actor lacks a capability."""


def can(capability: str, role: Optional[str], *, is_system_admin: bool = False) -> bool:
    if is_system_admin:
        return True
    if role is None:
        return False
    allowed = _MATRIX.get(capability)
    if allowed is None:
        return False
    return role in allowed


def require(capability: str, role: Optional[str], *, is_system_admin: bool = False) -> None:
    if not can(capability, role, is_system_admin=is_system_admin):
        raise PermissionDenied(capability)
