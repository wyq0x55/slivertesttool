"""User, membership and account-administration service (LAN Test Matrix).

Self-registration, project membership roles and system-admin account CRUD.
Split out of the former monolithic ``service`` module."""
from __future__ import annotations

import re as _re
from typing import Any, Optional

from ...extensions import db
from ...models import CellComment, LMUser, Project, ProjectMember, TestItemRow
from . import audit, settings
from .errors import ServiceError


# --------------------------------------------------------------------------- #
# Users & membership
# --------------------------------------------------------------------------- #
def get_user(user_id: int) -> Optional[LMUser]:
    return db.session.get(LMUser, user_id)


def get_user_by_name(username: str) -> Optional[LMUser]:
    return LMUser.query.filter_by(username=username).first()


def register_user(username: str, password: str, *, display_name: str = "",
                  email: str = "") -> LMUser:
    """Self-service registration for a LAN user.

    Validates the username against the configured whitelist pattern, enforces
    the minimum password length and username uniqueness, then creates a plain
    (non-admin) account. The account carries **no** project membership, so it
    can authenticate but sees nothing until an administrator grants it a project
    role — a safe default for an internal-network tool.

    The new account's ``status`` follows ``LM_REGISTRATION_DEFAULT_STATUS``:
    ``active`` lets it log in immediately, ``disabled`` leaves it pending until
    an admin activates it. Raises :class:`ServiceError` on any rule violation.
    """
    if not settings.ALLOW_REGISTRATION:
        raise ServiceError("用户注册功能已关闭", code="REGISTRATION_DISABLED")

    username = (username or "").strip()
    display_name = (display_name or "").strip()
    email = (email or "").strip()

    if not _re.fullmatch(settings.USERNAME_PATTERN, username):
        raise ServiceError(
            "用户名不合法（3-64 位，允许字母、数字、下划线、点、连字符）",
            code="VALIDATION_ERROR",
        )
    if len(password or "") < settings.PASSWORD_MIN_LEN:
        raise ServiceError(
            f"密码长度至少 {settings.PASSWORD_MIN_LEN} 位",
            code="VALIDATION_ERROR",
        )
    if get_user_by_name(username) is not None:
        raise ServiceError("用户名已存在", code="DUPLICATE")

    status = "active" if settings.REGISTRATION_DEFAULT_STATUS == "active" else "disabled"
    user = LMUser(
        username=username,
        display_name=display_name or username,
        email=email or None,
        status=status,
        is_system_admin=False,
        must_change_password=False,
    )
    user.set_password(password)
    db.session.add(user)
    db.session.commit()
    return user


def role_in_project(project_id: int, user: LMUser) -> Optional[str]:
    if user is None:
        return None
    if user.is_system_admin:
        return "project_admin"
    m = ProjectMember.query.filter_by(project_id=project_id, user_id=user.id).first()
    return m.role if m else None


def is_project_member(project_id: int, user: LMUser) -> bool:
    """True when ``user`` can act on the project (member or system admin)."""
    return role_in_project(project_id, user) is not None


# --------------------------------------------------------------------------- #
# Project membership management
# --------------------------------------------------------------------------- #
PROJECT_ROLES = ("project_admin", "editor", "reviewer", "reader")


def list_members(project_id: int) -> list[ProjectMember]:
    return (ProjectMember.query.filter_by(project_id=project_id)
            .order_by(ProjectMember.id.asc()).all())


def search_users(query: str = "", *, limit: int = 20,
                 active_only: bool = True) -> list[LMUser]:
    q = LMUser.query
    if active_only:
        q = q.filter(LMUser.status == "active")
    query = (query or "").strip()
    if query:
        like = f"%{query}%"
        q = q.filter(db.or_(LMUser.username.ilike(like),
                            LMUser.display_name.ilike(like)))
    return q.order_by(LMUser.username.asc()).limit(max(1, min(limit, 100))).all()


def add_member(actor: LMUser, project: Project, *, username: str = "",
               user_id: Optional[int] = None, role: str = "reader") -> ProjectMember:
    if role not in PROJECT_ROLES:
        raise ServiceError("角色无效", code="VALIDATION_ERROR")
    target = None
    if user_id is not None:
        target = get_user(int(user_id))
    elif username:
        target = get_user_by_name(username.strip())
    if target is None:
        raise ServiceError("找不到该用户", code="NOT_FOUND")
    if not target.is_active:
        raise ServiceError("该用户已被禁用", code="VALIDATION_ERROR")
    existing = ProjectMember.query.filter_by(
        project_id=project.id, user_id=target.id).first()
    if existing is not None:
        raise ServiceError("该用户已是项目成员", code="DUPLICATE")
    member = ProjectMember(project_id=project.id, user_id=target.id, role=role)
    db.session.add(member)
    audit.record("member.add", actor_id=actor.id, object_type="member",
                 object_id=target.id, project_id=project.id,
                 new_value={"user_id": target.id, "role": role})
    db.session.commit()
    return member


def update_member_role(actor: LMUser, project: Project, member_id: int,
                       role: str) -> ProjectMember:
    if role not in PROJECT_ROLES:
        raise ServiceError("角色无效", code="VALIDATION_ERROR")
    member = ProjectMember.query.filter_by(
        id=member_id, project_id=project.id).first()
    if member is None:
        raise ServiceError("成员不存在", code="NOT_FOUND")
    # Don't allow removing the last project admin's admin role.
    if member.role == "project_admin" and role != "project_admin":
        admins = ProjectMember.query.filter_by(
            project_id=project.id, role="project_admin").count()
        if admins <= 1:
            raise ServiceError("必须至少保留一名项目管理员", code="VALIDATION_ERROR")
    old = member.role
    member.role = role
    audit.record("member.update", actor_id=actor.id, object_type="member",
                 object_id=member.user_id, project_id=project.id,
                 old_value={"role": old}, new_value={"role": role})
    db.session.commit()
    return member


def remove_member(actor: LMUser, project: Project, member_id: int) -> None:
    member = ProjectMember.query.filter_by(
        id=member_id, project_id=project.id).first()
    if member is None:
        raise ServiceError("成员不存在", code="NOT_FOUND")
    if member.role == "project_admin":
        admins = ProjectMember.query.filter_by(
            project_id=project.id, role="project_admin").count()
        if admins <= 1:
            raise ServiceError("必须至少保留一名项目管理员", code="VALIDATION_ERROR")
    uid = member.user_id
    db.session.delete(member)
    audit.record("member.remove", actor_id=actor.id, object_type="member",
                 object_id=uid, project_id=project.id)
    db.session.commit()


# --------------------------------------------------------------------------- #
# Account (submitter) administration — system administrators only
# --------------------------------------------------------------------------- #
def list_users() -> list[LMUser]:
    return LMUser.query.order_by(LMUser.username.asc()).all()


def user_project_count(user_id: int) -> int:
    return ProjectMember.query.filter_by(user_id=user_id).count()


def admin_create_user(actor: LMUser, *, username: str, password: str,
                      display_name: str = "", email: str = "",
                      is_system_admin: bool = False,
                      status: str = "active") -> LMUser:
    username = (username or "").strip()
    if not _re.fullmatch(settings.USERNAME_PATTERN, username):
        raise ServiceError(
            "用户名不合法（3-64 位，允许字母、数字、下划线、点、连字符）",
            code="VALIDATION_ERROR")
    if len(password or "") < settings.PASSWORD_MIN_LEN:
        raise ServiceError(f"密码长度至少 {settings.PASSWORD_MIN_LEN} 位",
                           code="VALIDATION_ERROR")
    if get_user_by_name(username) is not None:
        raise ServiceError("用户名已存在", code="DUPLICATE")
    if status not in ("active", "disabled"):
        status = "active"
    user = LMUser(
        username=username, display_name=(display_name or "").strip() or username,
        email=(email or "").strip() or None, status=status,
        is_system_admin=bool(is_system_admin), must_change_password=False)
    user.set_password(password)
    db.session.add(user)
    audit.record("user.create", actor_id=actor.id, object_type="user",
                 object_id=username, new_value={"username": username,
                 "is_system_admin": bool(is_system_admin), "status": status})
    db.session.commit()
    return user


def admin_update_user(actor: LMUser, user_id: int,
                      changes: dict[str, Any]) -> LMUser:
    user = get_user(int(user_id))
    if user is None:
        raise ServiceError("用户不存在", code="NOT_FOUND")
    old = user.to_dict()
    if "display_name" in changes:
        user.display_name = (changes["display_name"] or "").strip() or user.username
    if "email" in changes:
        user.email = (changes["email"] or "").strip() or None
    if "status" in changes and changes["status"] in ("active", "disabled"):
        # Don't let an admin disable the last active system admin (or self-lock).
        if changes["status"] == "disabled" and user.is_system_admin:
            _guard_last_admin(user)
        user.status = changes["status"]
        if user.status == "active":
            user.failed_logins = 0
            user.locked_until = None
    if "is_system_admin" in changes:
        new_flag = bool(changes["is_system_admin"])
        if not new_flag and user.is_system_admin:
            _guard_last_admin(user)
        user.is_system_admin = new_flag
    if changes.get("password"):
        if len(changes["password"]) < settings.PASSWORD_MIN_LEN:
            raise ServiceError(f"密码长度至少 {settings.PASSWORD_MIN_LEN} 位",
                               code="VALIDATION_ERROR")
        user.set_password(changes["password"])
        user.must_change_password = False
        user.failed_logins = 0
        user.locked_until = None
    audit.record("user.update", actor_id=actor.id, object_type="user",
                 object_id=user.username, old_value=old, new_value=user.to_dict())
    db.session.commit()
    return user


def admin_delete_user(actor: LMUser, user_id: int) -> None:
    user = get_user(int(user_id))
    if user is None:
        raise ServiceError("用户不存在", code="NOT_FOUND")
    if user.id == actor.id:
        raise ServiceError("不能删除当前登录的账户", code="VALIDATION_ERROR")
    if user.is_system_admin:
        _guard_last_admin(user)
    _detach_user_references(user.id)
    username = user.username
    db.session.delete(user)
    audit.record("user.delete", actor_id=actor.id, object_type="user",
                 object_id=username)
    db.session.commit()


# Every foreign key that points at ``lm_users``. Membership rows are removed
# outright (a deleted user is no longer a member); authorship / ownership
# columns are nullified so historical projects, test items and comments survive
# the deletion instead of raising a ForeignKeyViolation.
def _detach_user_references(user_id: int) -> None:
    """Unified user-deletion strategy: clear all references to ``user_id``.

    Keeps the delete safe regardless of whether the database enforces
    ``ON DELETE SET NULL`` (Postgres does not, for these legacy FKs).
    """
    ProjectMember.query.filter_by(user_id=user_id).delete(
        synchronize_session=False)

    nullable_refs = (
        (Project, ("owner_id", "created_by")),
        (TestItemRow, ("owner_id", "created_by", "updated_by")),
        (CellComment, ("created_by",)),
    )
    for model, columns in nullable_refs:
        for column in columns:
            attr = getattr(model, column)
            model.query.filter(attr == user_id).update(
                {attr: None}, synchronize_session=False)


def _guard_last_admin(user: LMUser) -> None:
    """Raise if demoting/disabling ``user`` would leave no active system admin."""
    others = LMUser.query.filter(
        LMUser.id != user.id,
        LMUser.is_system_admin.is_(True),
        LMUser.status == "active",
    ).count()
    if others == 0:
        raise ServiceError("必须至少保留一名启用的系统管理员",
                           code="VALIDATION_ERROR")
