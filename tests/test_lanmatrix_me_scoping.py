"""Guards for the cross-project workspace endpoints (app/routes/lanmatrix/me.py).

These endpoints are the only place in the product that returns rows from more
than one project in a single response, so they are the only place a scoping
mistake leaks another team's data. The checks here are deliberately
source-level and database-free: the rest of the suite needs PostgreSQL, and a
guard that only runs when a database happens to be available is not a guard.
"""

from __future__ import annotations

import ast
import inspect
import pathlib

import pytest

from app.routes.lanmatrix import BLUEPRINTS, me as me_module


ME_SOURCE = pathlib.Path(inspect.getfile(me_module)).read_text(encoding="utf-8")
ME_TREE = ast.parse(ME_SOURCE)


def _func(name: str) -> ast.FunctionDef:
    for node in ME_TREE.body:
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"function {name} not found in me.py")


def _decorator_names(fn: ast.FunctionDef) -> set[str]:
    out = set()
    for dec in fn.decorator_list:
        node = dec.func if isinstance(dec, ast.Call) else dec
        if isinstance(node, ast.Name):
            out.add(node.id)
        elif isinstance(node, ast.Attribute):
            out.add(node.attr)
    return out


def test_blueprint_is_registered():
    """An unregistered blueprint fails silently as a 404, not an import error."""
    assert me_module.bp in BLUEPRINTS


def test_blueprint_has_csrf_and_error_handlers():
    assert "register_common(bp)" in ME_SOURCE


@pytest.mark.parametrize("name", ["me_overview", "me_tasks"])
def test_endpoints_require_login(name):
    assert "login_required" in _decorator_names(_func(name))


@pytest.mark.parametrize("name", ["me_overview", "me_tasks"])
def test_endpoints_are_read_only(name):
    """The workspace is a read surface; a POST/PATCH here would bypass the
    per-project permission checks that every write path performs."""
    decorators = _decorator_names(_func(name))
    assert "get" in decorators
    for verb in ("post", "put", "patch", "delete"):
        assert verb not in decorators


def test_scope_comes_from_the_shared_membership_filter():
    """Scoping must reuse ``service.list_projects``. Reimplementing the
    membership rule here is how the two views drift apart."""
    assert "service.list_projects(g.user)" in ME_SOURCE


def test_mine_filter_never_matches_the_submitter_label():
    """``Task.submitter`` is a free-text display label and is not unique, so
    filtering "my tasks" on it would show one user another user's work. Only
    the ``submitter_id`` foreign key may be used."""
    for node in ast.walk(ME_TREE):
        if isinstance(node, ast.Attribute) and node.attr == "submitter":
            value = node.value
            if isinstance(value, ast.Name) and value.id == "Task":
                raise AssertionError(
                    "me.py reads Task.submitter; use Task.submitter_id instead"
                )
    assert "Task.submitter_id == g.user.id" in ME_SOURCE


def test_task_query_is_always_restricted_to_visible_projects():
    """Every Task query in this module must be bounded by the visible project
    id set before any user-supplied filter is applied."""
    assert ME_SOURCE.count("Task.project_id.in_(") >= 2
    # An explicit ?project_id= must be intersected with the visible set rather
    # than replacing it, so an id the user cannot see returns nothing.
    assert "if want in by_id:" in ME_SOURCE


def test_result_set_is_capped():
    """A system admin can see every project; an uncapped cross-project query
    would pull the whole task table into memory."""
    assert me_module._MAX_TASKS <= 1000
    assert "limit + 1" in ME_SOURCE, "need one extra row to detect truncation"


def test_search_escapes_like_wildcards():
    """Unescaped % in a LIKE turns a search box into a full-table scan."""
    assert r'replace("%", r"\%")' in ME_SOURCE


def test_capabilities_come_from_the_permission_matrix():
    """The workspace ships per-project capability flags so its rows can offer
    the same buttons as the project task list. They must be derived from
    ``permissions.can`` -- a hand-written role check here would let the UI offer
    a 删除 the API then refuses (or hide one the user is entitled to)."""
    assert 'permissions.can("task.delete"' in ME_SOURCE
    assert "_capabilities" in ME_SOURCE


def test_role_lookup_is_restricted_to_the_current_user():
    """The bulk role query replaces per-project lookups; forgetting the user
    predicate would hand out every member's role for every project."""
    assert "ProjectMember.user_id == g.user.id" in ME_SOURCE
    assert "ProjectMember.project_id.in_(project_ids)" in ME_SOURCE


def test_role_lookup_is_a_single_query():
    """One query for all projects: a system admin sees every project, so a
    per-project lookup would turn one page load into dozens of round trips."""
    assert "def _roles_in(" in ME_SOURCE
    assert "role_in_project(" not in ME_SOURCE
