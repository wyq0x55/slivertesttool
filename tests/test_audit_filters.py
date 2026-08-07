"""Audit-log filter tests (P0-2).

These run without PostgreSQL. SQLAlchemy builds filter expressions from the
declarative model alone -- no connection needed -- so the filter *logic* is
genuinely covered here even though this environment cannot execute the query.
That is why ``audit_criteria`` exists as a separate pure function: it moves the
part that can be wrong into the part that can be tested.

What is deliberately NOT claimed: that the SQL returns the right rows. That
needs a database. What IS claimed: that each filter contributes exactly one
correctly-shaped predicate, that absent filters contribute nothing, and that
user input cannot smuggle LIKE wildcards into the search.
"""
import datetime
import re

import pytest
from flask import Flask

from app.models.lanmatrix import AuditLog
from app.routes.lanmatrix._base import arg_date, arg_int, arg_json, arg_str
from app.services.lanmatrix import comments_service as svc
from app.services.lanmatrix.errors import RequestParamError


def sql(expr):
    return str(expr)


def sql_of(crit):
    return [sql(c) for c in crit]


# --------------------------------------------------------------------------- #
# audit_criteria
# --------------------------------------------------------------------------- #
def test_project_scope_is_always_present():
    crit = svc.audit_criteria(5)
    assert len(crit) == 1
    assert "project_id" in sql(crit[0])


def test_no_filters_means_no_extra_predicates():
    # A bug that adds a stray predicate here would silently hide rows.
    assert len(svc.audit_criteria(5)) == 1


def test_each_filter_adds_exactly_one_predicate():
    base = len(svc.audit_criteria(5))
    for kwargs in (
        {"actor_id": 3},
        {"action": "item.update"},
        {"object_type": "item"},
        {"result": "failure"},
        {"date_from": datetime.datetime(2024, 1, 1)},
        {"date_to": datetime.datetime(2024, 1, 2)},
        {"q": "abc"},
    ):
        crit = svc.audit_criteria(5, **kwargs)
        assert len(crit) == base + 1, "%s did not add exactly one predicate" % kwargs


def test_all_filters_combine():
    crit = svc.audit_criteria(
        5, actor_id=3, action="a", object_type="item", result="success",
        date_from=datetime.datetime(2024, 1, 1),
        date_to=datetime.datetime(2024, 2, 1), q="x")
    assert len(crit) == 8


def test_actor_filter_targets_actor_column():
    assert "actor_id" in sql(svc.audit_criteria(5, actor_id=3)[1])


def test_actor_zero_is_not_treated_as_absent():
    # `if actor_id is not None` rather than `if actor_id` -- user id 0 is
    # unlikely but a falsy-check here is the classic way a filter goes missing.
    assert len(svc.audit_criteria(5, actor_id=0)) == 2


def test_date_from_is_a_lower_bound():
    s = sql(svc.audit_criteria(5, date_from=datetime.datetime(2024, 1, 1))[1])
    assert ">=" in s, s


def test_date_to_is_an_upper_bound():
    s = sql(svc.audit_criteria(5, date_to=datetime.datetime(2024, 1, 1))[1])
    assert "<=" in s, s


def test_date_bounds_are_not_swapped():
    crit = svc.audit_criteria(5, date_from=datetime.datetime(2024, 1, 1),
                              date_to=datetime.datetime(2024, 2, 1))
    assert ">=" in sql(crit[1]) and "<=" in sql(crit[2])


def test_empty_string_filters_are_ignored():
    # Query strings hand us "" constantly; treating it as a real value would
    # match nothing and look like "no records".
    crit = svc.audit_criteria(5, action="", object_type="", result="", q="")
    assert len(crit) == 1


def test_search_covers_object_action_and_error():
    s = sql(svc.audit_criteria(5, q="x")[1])
    for col in ("object_id", "action", "error_summary"):
        assert col in s, "search does not cover %s" % col


def test_search_is_an_or_not_an_and():
    s = sql(svc.audit_criteria(5, q="x")[1])
    assert " OR " in s, s


def test_search_escapes_percent():
    # Searching "100%" must not degrade into "everything starting with 100".
    crit = svc.audit_criteria(5, q="100%")
    s = sql(crit[1])
    assert "ESCAPE" in s.upper(), s


def test_search_escapes_underscore():
    crit = svc.audit_criteria(5, q="a_b")
    assert "ESCAPE" in sql(crit[1]).upper()


def test_search_escape_is_applied_to_the_bound_value():
    crit = svc.audit_criteria(5, q="100%_x")
    params = crit[1].compile().params
    vals = [v for v in params.values() if isinstance(v, str)]
    assert vals, "no bound parameters found"
    assert all("\\%" in v and "\\_" in v for v in vals), vals


def test_search_escapes_the_escape_character_first():
    # Escaping "%" before "\" would double-mangle a literal backslash.
    crit = svc.audit_criteria(5, q="a\\b")
    vals = [v for v in crit[1].compile().params.values() if isinstance(v, str)]
    assert all("\\\\b" in v for v in vals), vals


def test_search_is_case_insensitive_on_every_column():
    # Asserting merely that `lower(` appears somewhere is too weak: the search
    # is an OR of three columns, so one of them could silently lose ilike and
    # the check would still pass.
    s = sql(svc.audit_criteria(5, q="x")[1]).lower()
    assert s.count("lower(") >= 6, (
        "expected lower() on both sides of all 3 comparisons: %s" % s)


def test_search_escape_applies_to_every_column():
    s = sql(svc.audit_criteria(5, q="x")[1]).upper()
    assert s.count("ESCAPE") == 3, (
        "one of the three searched columns lost its ESCAPE clause: %s" % s)


def test_audit_results_whitelist():
    assert set(svc.AUDIT_RESULTS) == {"success", "failure"}


def test_audit_results_matches_model_default():
    default = AuditLog.__table__.c.result.default.arg
    assert default in svc.AUDIT_RESULTS


# --------------------------------------------------------------------------- #
# arg_date
# --------------------------------------------------------------------------- #
@pytest.fixture
def ctx():
    app = Flask(__name__)

    def _ctx(qs):
        return app.test_request_context("/?" + qs)

    return _ctx


def test_arg_date_parses_a_plain_date(ctx):
    with ctx("d=2024-05-01"):
        assert arg_date("d") == datetime.datetime(2024, 5, 1)


def test_arg_date_absent_returns_default(ctx):
    with ctx(""):
        assert arg_date("d") is None
        assert arg_date("d", "x") == "x"


def test_arg_date_blank_returns_default(ctx):
    with ctx("d="):
        assert arg_date("d") is None


def test_arg_date_end_of_day_includes_the_whole_day(ctx):
    # Without this, ?date_to=2024-05-01 means midnight and silently excludes
    # everything that happened on the day the user asked about.
    with ctx("d=2024-05-01"):
        v = arg_date("d", end_of_day=True)
    assert (v.hour, v.minute, v.second) == (23, 59, 59)


def test_arg_date_end_of_day_does_not_shift_the_date(ctx):
    with ctx("d=2024-05-01"):
        assert arg_date("d", end_of_day=True).date() == datetime.date(2024, 5, 1)


def test_arg_date_start_of_day_is_midnight(ctx):
    with ctx("d=2024-05-01"):
        v = arg_date("d")
    assert (v.hour, v.minute, v.second) == (0, 0, 0)


def test_arg_date_accepts_iso_timestamps(ctx):
    with ctx("d=2024-05-01T10:20:30"):
        assert arg_date("d") == datetime.datetime(2024, 5, 1, 10, 20, 30)


def test_arg_date_end_of_day_does_not_touch_an_explicit_time(ctx):
    # The user gave a precise instant; widening it to 23:59 would be wrong.
    with ctx("d=2024-05-01T10:20:30"):
        assert arg_date("d", end_of_day=True).hour == 10


def test_arg_date_rejects_garbage(ctx):
    with ctx("d=abc"):
        with pytest.raises(RequestParamError):
            arg_date("d")


def test_arg_date_rejects_an_impossible_date(ctx):
    with ctx("d=2024-02-31"):
        with pytest.raises(RequestParamError):
            arg_date("d")


def test_arg_date_rejects_wrong_separator(ctx):
    with ctx("d=2024/05/01"):
        with pytest.raises(RequestParamError):
            arg_date("d")


def test_arg_date_error_names_the_parameter(ctx):
    with ctx("d=abc"):
        with pytest.raises(RequestParamError) as e:
            arg_date("d")
    assert e.value.details.get("param") == "d"
    assert "d" in str(e.value)


def test_arg_date_tolerates_surrounding_whitespace(ctx):
    with ctx("d=%202024-05-01%20"):
        assert arg_date("d") == datetime.datetime(2024, 5, 1)


# --------------------------------------------------------------------------- #
# arg_str
# --------------------------------------------------------------------------- #
def test_arg_str_returns_the_value(ctx):
    with ctx("s=hello"):
        assert arg_str("s") == "hello"


def test_arg_str_absent_returns_default(ctx):
    with ctx(""):
        assert arg_str("s") is None
        assert arg_str("s", "d") == "d"


def test_arg_str_blank_returns_default(ctx):
    with ctx("s=%20%20"):
        assert arg_str("s") is None


def test_arg_str_trims(ctx):
    with ctx("s=%20hi%20"):
        assert arg_str("s") == "hi"


def test_arg_str_enforces_the_whitelist(ctx):
    with ctx("s=nope"):
        with pytest.raises(RequestParamError):
            arg_str("s", allowed=("a", "b"))


def test_arg_str_accepts_a_whitelisted_value(ctx):
    with ctx("s=a"):
        assert arg_str("s", allowed=("a", "b")) == "a"


def test_arg_str_whitelist_error_lists_the_options(ctx):
    # "无效参数" is useless; the message must say what *is* valid.
    with ctx("s=nope"):
        with pytest.raises(RequestParamError) as e:
            arg_str("s", allowed=("a", "b"))
    assert "a" in str(e.value) and "b" in str(e.value)


def test_arg_str_enforces_max_length(ctx):
    with ctx("s=" + "x" * 200):
        with pytest.raises(RequestParamError):
            arg_str("s", max_length=64)


def test_arg_str_allows_exactly_max_length(ctx):
    # Off-by-one: the boundary value must be accepted, not rejected.
    with ctx("s=" + "x" * 64):
        assert len(arg_str("s", max_length=64)) == 64


def test_arg_str_length_is_measured_after_trimming(ctx):
    with ctx("s=%20" + "x" * 64 + "%20"):
        assert len(arg_str("s", max_length=64)) == 64


# --------------------------------------------------------------------------- #
# Route wiring -- derived guards, so a future endpoint cannot quietly regress
# --------------------------------------------------------------------------- #
def _routes_src():
    import pathlib
    import app.routes.lanmatrix as pkg
    root = pathlib.Path(pkg.__file__).parent
    return (root / "projects_items.py").read_text(encoding="utf-8")


def _audit_route_body():
    """Source of the audit route handler.

    Fails with a clear message if the anchor stops matching -- a renamed
    handler must not silently turn every guard below into a no-op.
    """
    src = _routes_src()
    anchor = "def audit_logs(project_id):"
    assert anchor in src, (
        "cannot find %r; the audit route was renamed and these guards are "
        "no longer checking anything" % anchor)
    start = src.index(anchor)
    return src[start:start + 2000]


def test_audit_route_passes_every_filter_through():
    body = _audit_route_body()
    for kw in ("actor_id=", "action=", "object_type=", "result=",
               "date_from=", "date_to=", "q="):
        assert kw in body, "audit route drops the %s filter" % kw


def test_audit_route_bounds_the_free_text_search():
    # An unbounded LIKE term is a cheap way to make the database work hard.
    # Checking only that "max_length" appears somewhere is too weak -- the
    # other string filters have one, so `q` could lose its own unnoticed.
    body = _audit_route_body()
    m = re.search(r'q=arg_str\("q"[^)]*\)', body)
    assert m, "could not find the q filter in the audit route"
    assert "max_length" in m.group(0), "the free-text search is unbounded"


def test_audit_route_validates_the_result_whitelist():
    body = _audit_route_body()
    assert "AUDIT_RESULTS" in body, "result filter accepts arbitrary values"


def test_audit_route_makes_date_to_inclusive():
    body = _audit_route_body()
    assert "end_of_day=True" in body, (
        "date_to is exclusive; the last day of the range will look empty")


def test_audit_route_rejects_an_inverted_range():
    body = _audit_route_body()
    assert "date_from > date_to" in body


def test_is_filtered_false_for_project_scope_only():
    # The UI needs this to tell "nothing happened" from "your filter hid it".
    assert svc.is_filtered(svc.audit_criteria(5)) is False


def test_is_filtered_true_for_each_filter():
    for kwargs in ({"actor_id": 3}, {"action": "a"}, {"object_type": "i"},
                   {"result": "success"}, {"q": "x"},
                   {"date_from": datetime.datetime(2024, 1, 1)},
                   {"date_to": datetime.datetime(2024, 1, 1)}):
        assert svc.is_filtered(svc.audit_criteria(5, **kwargs)) is True, kwargs


def test_list_audit_reports_whether_filters_were_applied():
    import inspect
    assert "is_filtered" in inspect.getsource(svc.list_audit)


def test_actor_names_are_resolved_in_one_query():
    # A per-row lookup would be N+1 on every page load.
    import inspect
    src = inspect.getsource(svc._attach_actor_names)
    assert ".in_(" in src, "actor names are not batch-resolved"


def test_actor_name_resolution_is_wired_into_list_audit():
    import inspect
    assert "_attach_actor_names" in inspect.getsource(svc.list_audit)
