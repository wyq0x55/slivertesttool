"""Query-string parsing guards for the /api/v1 blueprints.

Before this, six routes did a bare ``int(request.args.get("page", 1))``. A
pasted or hand-edited URL with ``?page=abc`` raised an uncaught ValueError,
which Flask turns into a **500** -- the server reporting an internal fault for
what is entirely the caller's typo. ``?filter=notjson`` did the same via
``json.loads``.

These tests are deliberately PG-free: they exercise the helpers under a bare
``Flask`` request context, so they run in environments with no database (where
every ``create_app()`` test errors out on connection refused) and still cover
the logic that decides 400-vs-500.
"""
from __future__ import annotations

import pytest
from flask import Flask

from app.routes.lanmatrix._base import arg_int, arg_json
from app.services.lanmatrix.errors import RequestParamError, ServiceError

app = Flask(__name__)


def ctx(query: str):
    """A request context with the given raw query string."""
    return app.test_request_context("/?" + query)


# --------------------------------------------------------------------------- #
# arg_int -- the omitted case
# --------------------------------------------------------------------------- #
def test_absent_yields_default():
    with ctx(""):
        assert arg_int("page", 7) == 7


def test_empty_string_yields_default():
    with ctx("page="):
        assert arg_int("page", 7) == 7


def test_whitespace_only_yields_default():
    with ctx("page=%20%20"):
        assert arg_int("page", 7) == 7


def test_default_is_not_bounds_checked():
    """An omitted parameter must never produce a 400.

    Bounds exist to validate *caller* input. Applying them to our own default
    would answer a request that never mentioned `page` with
    "参数 page 不能小于 1", blaming the caller for our misconfiguration.
    """
    with ctx(""):
        assert arg_int("page", 0, minimum=1) == 0


# --------------------------------------------------------------------------- #
# arg_int -- the happy path
# --------------------------------------------------------------------------- #
def test_parses_valid_int():
    with ctx("page=12"):
        assert arg_int("page", 1) == 12


def test_tolerates_surrounding_whitespace():
    with ctx("page=%20%2012%20"):
        assert arg_int("page", 1) == 12


def test_minimum_is_inclusive():
    with ctx("page=1"):
        assert arg_int("page", 5, minimum=1) == 1


def test_maximum_is_inclusive():
    with ctx("page_size=200"):
        assert arg_int("page_size", 50, minimum=1, maximum=200) == 200


def test_negative_allowed_when_no_minimum():
    with ctx("offset=-3"):
        assert arg_int("offset", 0) == -3


# --------------------------------------------------------------------------- #
# arg_int -- the rejections (each of these used to be a 500)
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("raw", ["abc", "1.5", "0x10", "1e3", "--1", "1,000", "null"])
def test_unparseable_raises(raw):
    with ctx("page=" + raw):
        with pytest.raises(RequestParamError):
            arg_int("page", 1)


def test_below_minimum_raises():
    with ctx("page=0"):
        with pytest.raises(RequestParamError):
            arg_int("page", 1, minimum=1)


def test_negative_below_minimum_raises():
    with ctx("page=-5"):
        with pytest.raises(RequestParamError):
            arg_int("page", 1, minimum=1)


def test_above_maximum_raises():
    """Clamping silently would re-introduce the Phase 6 failure: the caller
    asks for 100000 rows, gets 200, and has no way to know."""
    with ctx("page_size=100000"):
        with pytest.raises(RequestParamError):
            arg_int("page_size", 50, minimum=1, maximum=200)


# --------------------------------------------------------------------------- #
# The exception must actually render as a 400, not just be raised
# --------------------------------------------------------------------------- #
def test_is_a_service_error_so_the_registered_handler_catches_it():
    """RequestParamError subclasses ServiceError on purpose.

    ``register_common`` registers a handler for ServiceError; inheriting means
    the 400 mapping needs no new plumbing, and -- crucially -- no blanket
    ValueError handler, which would dress genuine internal bugs up as 400s.
    """
    assert issubclass(RequestParamError, ServiceError)


def test_carries_validation_error_code_and_param():
    with ctx("page=abc"):
        with pytest.raises(RequestParamError) as ei:
            arg_int("page", 1)
    exc = ei.value
    assert exc.code == "VALIDATION_ERROR"
    assert exc.details == {"param": "page"}
    assert exc.param == "page"


def test_maps_to_status_400_through_the_shared_handler():
    from app.routes.lanmatrix._base import _service_err
    with app.test_request_context("/"):
        _resp, status = _service_err(RequestParamError("坏参数", param="page"))
    assert status == 400


def test_message_names_the_parameter_and_the_bad_value():
    """A 400 that does not say which parameter was wrong is barely better than
    the 500 it replaced."""
    with ctx("page_size=abc"):
        with pytest.raises(RequestParamError) as ei:
            arg_int("page_size", 50)
    msg = str(ei.value)
    assert "page_size" in msg
    assert "abc" in msg


# --------------------------------------------------------------------------- #
# arg_json -- ?filter= was the second 500
# --------------------------------------------------------------------------- #
def test_json_absent_yields_default():
    with ctx(""):
        assert arg_json("filter", []) == []


def test_json_empty_yields_default():
    with ctx("filter="):
        assert arg_json("filter", []) == []


def test_json_parses_array():
    with ctx("filter=%5B%7B%22f%22%3A1%7D%5D"):
        assert arg_json("filter", []) == [{"f": 1}]


@pytest.mark.parametrize("raw", ["abc", "%7B", "%5B1%2C%5D", "%27single%27"])
def test_json_malformed_raises(raw):
    with ctx("filter=" + raw):
        with pytest.raises(RequestParamError):
            arg_json("filter", [])


def test_json_error_carries_param():
    with ctx("filter=notjson"):
        with pytest.raises(RequestParamError) as ei:
            arg_json("filter", [])
    assert ei.value.details == {"param": "filter"}
    assert ei.value.code == "VALIDATION_ERROR"


# --------------------------------------------------------------------------- #
# The routes must actually use the helpers
# --------------------------------------------------------------------------- #
def test_no_route_parses_query_ints_bare():
    """A regression net: the helpers are worthless if a new route goes back to
    ``int(request.args.get(...))``. me.py is exempt -- it wraps its own parse in
    try/except and reports a ``truncated`` flag, a documented tolerant case.
    """
    import os
    import re
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    routes = os.path.join(here, "app", "routes")
    offenders = []
    for root, _dirs, files in os.walk(routes):
        for fn in files:
            if not fn.endswith(".py"):
                continue
            path = os.path.join(root, fn)
            with open(path, encoding="utf-8") as fh:
                for i, line in enumerate(fh, 1):
                    if line.lstrip().startswith("#"):
                        continue
                    if re.search(r"int\(request\.args", line):
                        if fn == "me.py":
                            continue
                        offenders.append("%s:%d" % (fn, i))
    assert offenders == [], "bare int(request.args) reintroduced: %s" % offenders


def _route_sources():
    import os
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    routes = os.path.join(here, "app", "routes")
    for root, _dirs, files in os.walk(routes):
        for fn in sorted(files):
            if fn.endswith(".py"):
                path = os.path.join(root, fn)
                with open(path, encoding="utf-8") as fh:
                    yield fn, fh.read()


def test_every_page_size_has_a_ceiling():
    """Derived guard: any ``arg_int("page_size", ...)`` must pass ``maximum=``.

    Without a ceiling the value reaches the service layer, which clamps it
    silently -- the caller asks for 100000 rows, gets 500, and is never told.
    Written to scan the routes rather than name them, so a *new* paginated
    endpoint that forgets its ceiling fails this test on its own.
    """
    import re
    missing = []
    found = 0
    for fn, src in _route_sources():
        for m in re.finditer(r'arg_int\(\s*"page_size".*?\)', src, re.S):
            found += 1
            if "maximum=" not in m.group(0):
                missing.append("%s: %s" % (fn, " ".join(m.group(0).split())))
    assert found >= 3, "expected to find the known page_size call sites, saw %d" % found
    assert missing == [], "page_size without a ceiling: %s" % missing


def test_every_page_arg_has_a_floor():
    """``page=0`` or ``page=-1`` is meaningless; each call site must say so."""
    import re
    missing = []
    found = 0
    for fn, src in _route_sources():
        for m in re.finditer(r'arg_int\(\s*"page".*?\)', src, re.S):
            found += 1
            if "minimum=" not in m.group(0):
                missing.append("%s: %s" % (fn, " ".join(m.group(0).split())))
    assert found >= 3, "expected to find the known page call sites, saw %d" % found
    assert missing == [], "page without a floor: %s" % missing


def test_no_route_parses_query_json_bare():
    import os
    import re
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    routes = os.path.join(here, "app", "routes")
    offenders = []
    for root, _dirs, files in os.walk(routes):
        for fn in files:
            if not fn.endswith(".py"):
                continue
            with open(os.path.join(root, fn), encoding="utf-8") as fh:
                for i, line in enumerate(fh, 1):
                    if line.lstrip().startswith("#"):
                        continue
                    if re.search(r"json\.loads\(\s*(request\.args|filters)", line):
                        offenders.append("%s:%d" % (fn, i))
    assert offenders == [], "bare json.loads on a query param: %s" % offenders
