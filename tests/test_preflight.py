"""Guards for the submit-preflight endpoint (Phase 11, list item #15).

These run without PostgreSQL: they read the route source and import the module
that owns the vocabulary. The point is not to re-test Flask but to pin the two
decisions that make the feature honest:

  * the stdlib list is *derived* from the running interpreter, never a literal
    copied into the source. A frozen copy drifts from whatever interpreter
    actually runs judge.py, and the failure mode of a stale copy is the worst
    kind -- a confident warning about a perfectly good module, which teaches
    people to ignore every warning the bar ever shows.

  * the endpoint is permission-checked. It is cheap and read-only, but it
    reports which models a project has registered.
"""

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
ROUTE = ROOT / "app" / "routes" / "lanmatrix" / "tasks.py"


def _source() -> str:
    return ROUTE.read_text(encoding="utf-8")


def _route_body() -> str:
    src = _source()
    i = src.find("def task_preflight")
    assert i != -1, (
        "task_preflight() not found in %s -- if it was renamed, update this test "
        "rather than deleting it" % ROUTE
    )
    j = src.find("\n@", i)
    return src[i:] if j == -1 else src[i:j]


class TestRoute:
    def test_route_is_registered(self):
        assert "tasks/preflight" in _source()

    def test_route_is_a_get(self):
        src = _source()
        i = src.find("def task_preflight")
        decorator = src[max(0, i - 400):i]
        assert "@bp.get(" in decorator or 'methods=["GET"]' in decorator

    def test_route_requires_upload_permission(self):
        # An open endpoint would leak the model inventory of any project.
        assert '_project_and_role(project_id, "task.upload")' in _route_body()

    def test_route_returns_both_vocabularies(self):
        # Asserted as `key: sorted(NAME)` rather than by the presence of the
        # bare tokens: `set()  # _STDLIB_NAMES` still contains "_STDLIB_NAMES",
        # and an empty stdlib list would make every stdlib import look missing.
        body = _route_body()
        assert re.search(r'"stdlib":\s*sorted\(_STDLIB_NAMES\)', body), (
            "the stdlib key must be built from the interpreter's own names"
        )
        assert re.search(r'"silver_roots":\s*sorted\(_SILVER_ROOTS\)', body), (
            "the silver_roots key must be built from the bundler's roots"
        )

    def test_route_adds_no_database_query(self):
        # The bar reports on facts list_project_tasks already returns; this
        # endpoint exists only to ship the interpreter's module vocabulary.
        body = _route_body()
        for token in (".query", "session.execute", "select("):
            assert token not in body, (
                "task_preflight should not query the database; found %r" % token
            )


class TestVocabularyIsDerived:
    def test_stdlib_names_are_not_hardcoded(self):
        # The derivation guard: if someone replaces the import with a literal
        # list, this fails.
        from app.runners import judge_bundler

        src = Path(judge_bundler.__file__).read_text(encoding="utf-8")
        m = re.search(r"_STDLIB_NAMES\s*=\s*(.+)", src)
        assert m, "_STDLIB_NAMES assignment not found"
        assign = m.group(1)
        assert "stdlib_module_names" in assign, (
            "_STDLIB_NAMES must be derived from sys.stdlib_module_names, not "
            "frozen into the source; found: %s" % assign.strip()
        )

    def test_route_reuses_judge_bundler_vocabulary(self):
        # Not a second, parallel copy living in the route module.
        src = _source()
        assert "_STDLIB_NAMES" in src
        assert "judge_bundler" in src

    def test_stdlib_names_are_populated(self):
        from app.runners.judge_bundler import _STDLIB_NAMES

        assert len(_STDLIB_NAMES) > 100
        for known in ("os", "sys", "json", "re", "pathlib"):
            assert known in _STDLIB_NAMES

    def test_stdlib_names_exclude_third_party(self):
        from app.runners.judge_bundler import _STDLIB_NAMES

        # If these leaked in, the bar would stay silent about a genuinely
        # missing dependency.
        for pkg in ("flask", "sqlalchemy", "numpy"):
            assert pkg not in _STDLIB_NAMES

    def test_silver_roots_are_populated(self):
        from app.runners.judge_bundler import _SILVER_ROOTS

        assert len(_SILVER_ROOTS) >= 1
        assert all(isinstance(x, str) and x for x in _SILVER_ROOTS)


class TestPayloadShape:
    """The client sorts and de-duplicates nothing; the payload must be usable."""

    def test_payload_is_json_serialisable(self):
        import json

        from app.runners.judge_bundler import _SILVER_ROOTS, _STDLIB_NAMES

        payload = {
            "stdlib": sorted(_STDLIB_NAMES),
            "silver_roots": sorted(_SILVER_ROOTS),
        }
        # sets are not JSON-serialisable; this pins the sorted() conversion.
        text = json.dumps(payload)
        assert '"os"' in text

    def test_route_converts_sets_to_lists(self):
        body = _route_body()
        # jsonify() cannot serialise a set: passing one through would 500 the
        # endpoint, and the bar would silently fall back to no import check.
        assert not re.search(r'"stdlib":\s*(?!sorted)\w*\(?_STDLIB_NAMES', body), (
            "the route must convert the name sets to sorted lists; a raw set "
            "is not JSON-serialisable and would 500"
        )
        assert body.count("sorted(") >= 2


class TestClientPairing:
    """The JS must not invent its own answer to a question the server owns."""

    JS = ROOT / "app" / "static" / "js" / "lanmatrix" / "preflight.js"
    TASKS_JS = ROOT / "app" / "static" / "js" / "lanmatrix" / "project_tasks.js"

    def test_js_has_no_hardcoded_stdlib_list(self):
        src = self.JS.read_text(encoding="utf-8")
        # A frozen client-side copy is exactly what this design avoids.
        for tell in ('"os"', "'os'", '"pathlib"', "'pathlib'"):
            assert tell not in src, (
                "preflight.js appears to carry its own stdlib list (%s); the "
                "vocabulary must come from the server" % tell
            )

    def test_js_skips_the_scan_without_vocabulary(self):
        src = self.TASKS_JS.read_text(encoding="utf-8")
        assert "pfVocab" in src
        # Running the scan with an empty vocabulary would flag every stdlib
        # import as missing.
        assert re.search(r"if \(!pfVocab[\s\S]{0,160}return;", src), (
            "project_tasks.js must bail out of the import scan when the server "
            "vocabulary is unavailable"
        )

    def test_unresolved_note_wording_still_exists_server_side(self):
        # The bar is a *preview* of this after-the-fact note. If the note is
        # removed or reworded, the preview should be revisited.
        svc = ROOT / "app" / "services" / "upload_service.py"
        assert "unresolved_local_imports" in svc.read_text(encoding="utf-8")


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
