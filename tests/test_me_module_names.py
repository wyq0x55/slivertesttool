"""Every name ``me.py`` uses at runtime must actually exist.

The failure this pins down shipped as a 500 on ``/api/v1/me/notifications``:
``_notification_totals`` was called by three endpoints and defined by none. The
module imports cleanly -- Python only resolves a global when the line runs --
so nothing caught it until a user opened the notification bell.

Import-time smoke tests cannot catch this class of defect, and the endpoints in
question need PostgreSQL to exercise for real. A source-level check does catch
it, runs everywhere, and costs nothing.
"""

from __future__ import annotations

import ast
import builtins
import inspect
import pathlib

import pytest

from app.routes.lanmatrix import me as me_module
from app.services.lanmatrix import notification_service

ME_PATH = pathlib.Path(inspect.getfile(me_module))
ME_SOURCE = ME_PATH.read_text(encoding="utf-8")
ME_TREE = ast.parse(ME_SOURCE)


def _bound_names(tree: ast.AST) -> set[str]:
    """Everything the module itself can bind: defs, assignments, imports..."""
    out = set(dir(builtins))
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef,
                             ast.ClassDef)):
            out.add(node.name)
        elif isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
            out.add(node.id)
        elif isinstance(node, ast.arg):
            out.add(node.arg)
        elif isinstance(node, ast.alias):
            out.add((node.asname or node.name).split(".")[0])
        elif isinstance(node, ast.ExceptHandler) and node.name:
            out.add(node.name)
        elif isinstance(node, ast.Global):
            out.update(node.names)
    return out


def _private_helpers_called() -> set[str]:
    """Module-private helpers (``_foo(...)``) invoked anywhere in the file."""
    return {node.func.id for node in ast.walk(ME_TREE)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id.startswith("_")}


class TestNoUnresolvedNames:
    def test_every_loaded_name_is_bound(self):
        used = {n.id for n in ast.walk(ME_TREE)
                if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)}
        assert sorted(used - _bound_names(ME_TREE)) == []

    @pytest.mark.parametrize("name", sorted(_private_helpers_called()))
    def test_each_private_helper_exists_at_runtime(self, name):
        # Resolved against the imported module, so a helper that is defined but
        # (say) nested inside another function still fails here.
        assert hasattr(me_module, name), f"{name}() is called but never defined"
        assert callable(getattr(me_module, name))


class TestNotificationTotals:
    """The helper itself: shape, and that the endpoints keep using it."""

    def test_it_exists_and_is_callable(self):
        assert callable(me_module._notification_totals)

    def test_it_reports_both_tab_counters(self):
        # The panel renders 未读 and 历史 together. Returning one of them leaves
        # the other label stale until the user clicks it -- and archiving moves
        # a row between the two, so a single counter is wrong on both sides.
        tree = ast.parse(inspect.getsource(me_module._notification_totals))
        keys = {node.value for node in ast.walk(tree)
                if isinstance(node, ast.Constant) and isinstance(node.value, str)}
        assert {"unread", "history"} <= keys

    def test_it_uses_the_service_counters(self):
        source = inspect.getsource(me_module._notification_totals)
        assert "unread_count" in source
        assert "history_count" in source

    def test_the_service_actually_offers_them(self):
        # If a counter is ever renamed in the service, fail here rather than at
        # request time.
        assert callable(notification_service.unread_count)
        assert callable(notification_service.history_count)

    @pytest.mark.parametrize("endpoint", ["me_notifications",
                                          "me_notifications_archive",
                                          "me_notifications_clear_history"])
    def test_every_mutating_view_returns_the_totals(self, endpoint):
        # These three are exactly the paths that can change what the tabs show.
        fn = next(n for n in ME_TREE.body
                  if isinstance(n, ast.FunctionDef) and n.name == endpoint)
        called = {n.func.id for n in ast.walk(fn)
                  if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
        assert "_notification_totals" in called
