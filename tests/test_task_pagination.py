"""Pagination guards that do not need a database.

The real queries cannot run here (no PostgreSQL), so these tests pin the two
things that are pure logic or pure structure:

  1. ``clamp_limit`` never lets a caller-supplied value escape its bounds and
     never raises -- a bad ``?limit=`` in a pasted URL must not 500 the page.
  2. ``list_tasks`` and ``count_tasks`` both build on ``_scoped_query``. If one
     of them ever grew its own filter chain, the UI would render "共 N 条" next
     to a list filtered differently -- a wrong number nobody would question.
     That is a source-level invariant, so it is checked with the AST.
"""

import ast
import inspect
import os

import pytest

from app.services import task_service


class TestClampLimit:
    def test_default_when_missing(self):
        assert task_service.clamp_limit(None) == task_service.DEFAULT_LIST_LIMIT
        assert task_service.clamp_limit("") == task_service.DEFAULT_LIST_LIMIT

    def test_garbage_falls_back_instead_of_raising(self):
        for bad in ("abc", "1e5", "  ", "12abc", [], {}, object()):
            assert task_service.clamp_limit(bad) == task_service.DEFAULT_LIST_LIMIT

    def test_explicit_default_is_honoured(self):
        assert task_service.clamp_limit("nope", default=7) == 7

    def test_ceiling(self):
        assert task_service.clamp_limit(10 ** 9) == task_service.MAX_LIST_LIMIT
        assert (task_service.clamp_limit(str(task_service.MAX_LIST_LIMIT + 1))
                == task_service.MAX_LIST_LIMIT)

    def test_non_positive_falls_back_to_default(self):
        # Not clamped up to 1: `?limit=0` is nonsense, and honouring it as "one
        # row" would render a 1-row list beside a total of thousands, which
        # reads as data loss. Falling back to the default is the safer reading
        # of an obviously bad parameter.
        assert task_service.clamp_limit(0) == task_service.DEFAULT_LIST_LIMIT
        assert task_service.clamp_limit(-5) == task_service.DEFAULT_LIST_LIMIT
        assert task_service.clamp_limit(0, default=7) == 7

    def test_passthrough_in_range(self):
        assert task_service.clamp_limit(50) == 50
        assert task_service.clamp_limit("50") == 50

    def test_bounds_are_sane_relative_to_each_other(self):
        assert 0 < task_service.DEFAULT_LIST_LIMIT <= task_service.MAX_LIST_LIMIT


def _calls_in(func):
    """Names of functions called directly inside ``func``."""
    src = inspect.getsource(func)
    tree = ast.parse(src.lstrip())
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            f = node.func
            if isinstance(f, ast.Name):
                names.add(f.id)
            elif isinstance(f, ast.Attribute):
                names.add(f.attr)
    return names


class TestScopedQueryIsShared:
    def test_list_tasks_uses_scoped_query(self):
        assert "_scoped_query" in _calls_in(task_service.list_tasks)

    def test_count_tasks_uses_scoped_query(self):
        assert "_scoped_query" in _calls_in(task_service.count_tasks)

    def test_neither_builds_its_own_filter_chain(self):
        # ``filter``/``filter_by`` belong in _scoped_query only. Seeing them in
        # either wrapper means the two can now disagree.
        for fn in (task_service.list_tasks, task_service.count_tasks):
            calls = _calls_in(fn)
            assert "filter" not in calls, fn.__name__
            assert "filter_by" not in calls, fn.__name__

    def test_count_tasks_does_not_limit(self):
        # A limit inside count_tasks would cap the total at the page size and
        # make "truncated" permanently False.
        assert "limit" not in _calls_in(task_service.count_tasks)


ROUTES = [
    ("app/routes/lanmatrix/tasks.py", "list_project_tasks"),
    ("app/routes/lanmatrix/admin_console.py", "admin_list_tasks"),
    ("app/routes/api_routes.py", "list_tasks"),
]


def _func_source(rel_path, func_name):
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    path = os.path.join(root, rel_path)
    if not os.path.exists(path):
        pytest.skip("not in this tree: %s" % rel_path)
    with open(path, "r", encoding="utf-8") as f:
        tree = ast.parse(f.read())
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == func_name:
            return node
    pytest.fail("%s not found in %s" % (func_name, rel_path))


class TestRoutesReportTruncation:
    """Every list endpoint must clamp, and must tell the client it clipped.

    An endpoint that clamps but stays silent is the original bug wearing a
    smaller number.
    """

    @pytest.mark.parametrize("rel_path,func_name", ROUTES)
    def test_clamps(self, rel_path, func_name):
        # Exact call names, NOT a substring search of ast.dump(): "clamp_limit"
        # is a substring of any typo'd variant, so a dump search would happily
        # accept a call that does not exist. (Found by mutation testing.)
        node = _func_source(rel_path, func_name)
        called = set()
        for n in ast.walk(node):
            if isinstance(n, ast.Call):
                f = n.func
                if isinstance(f, ast.Name):
                    called.add(f.id)
                elif isinstance(f, ast.Attribute):
                    called.add(f.attr)
        assert "clamp_limit" in called, "%s calls %s" % (func_name, sorted(called))

    @pytest.mark.parametrize("rel_path,func_name", ROUTES)
    def test_reports_total_and_truncated(self, rel_path, func_name):
        node = _func_source(rel_path, func_name)
        # Response keys arrive two ways in this codebase: ok({"total": ...})
        # builds a dict literal, jsonify(total=...) uses keywords. Scanning
        # only one style silently passes routes written in the other.
        keys = {n.value for n in ast.walk(node)
                if isinstance(n, ast.Constant) and isinstance(n.value, str)}
        keys |= {kw.arg for n in ast.walk(node) if isinstance(n, ast.Call)
                 for kw in n.keywords if kw.arg}
        assert "total" in keys, func_name
        assert "truncated" in keys, func_name
