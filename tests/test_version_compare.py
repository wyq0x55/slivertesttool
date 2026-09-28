"""Read-only version compare for the project dashboard.

Uses a private SQLite file, same as tests/test_dashboard.py. Do not point this
module at DATABASE_URL, TEST_DATABASE_URL, or the application .env.
"""
from __future__ import annotations

import importlib
import os
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
_HTML = _ROOT / "app" / "templates" / "lanmatrix" / "dashboard.html"
_JS = _ROOT / "app" / "static" / "js" / "lanmatrix" / "dashboard.js"

_CHANGES = (
    "only_left",
    "only_right",
    "unchanged",
    "pass_to_fail",
    "fail_to_pass",
    "other",
)


@pytest.fixture(scope="module")
def compare_app():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    old = {name: os.environ.get(name) for name in ("DATABASE_URL", "TEST_DATABASE_URL", "SECRET_KEY")}
    os.environ.pop("TEST_DATABASE_URL", None)
    os.environ["DATABASE_URL"] = f"sqlite:///{path}"
    os.environ["SECRET_KEY"] = "version-compare-test"

    import app.config as config_mod
    import app as app_pkg

    importlib.reload(config_mod)
    importlib.reload(app_pkg)

    application = app_pkg.create_app()
    from app.bootstrap import bootstrap_app

    bootstrap_app(application)
    with application.app_context():
        yield application

    for name, value in old.items():
        if value is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = value
    try:
        os.unlink(path)
    except OSError:
        pass


@pytest.fixture
def env(compare_app):
    from app.extensions import db
    from app.models import LMUser, Project, ProjectMember, TestRunRecord

    suffix = datetime.utcnow().strftime("%H%M%S%f")
    reader = LMUser(username=f"vcr{suffix}", display_name="Reader", password_hash="x")
    outsider = LMUser(username=f"vco{suffix}", display_name="Outsider", password_hash="x")
    db.session.add_all([reader, outsider])
    db.session.commit()

    project = Project(code=f"C{suffix}"[:16], name="Compare", owner_id=reader.id)
    other = Project(code=f"X{suffix}"[:16], name="Other", owner_id=reader.id)
    db.session.add_all([project, other])
    db.session.commit()
    db.session.add(ProjectMember(project_id=project.id, user_id=reader.id, role="reader"))
    db.session.commit()

    client = compare_app.test_client()
    bundle = {
        "app": compare_app,
        "client": client,
        "project": project,
        "other": other,
        "reader": reader,
        "outsider": outsider,
    }
    yield bundle

    TestRunRecord.query.filter(
        TestRunRecord.project_id.in_([project.id, other.id])
    ).delete(synchronize_session=False)
    ProjectMember.query.filter(
        ProjectMember.project_id.in_([project.id, other.id])
    ).delete(synchronize_session=False)
    db.session.delete(project)
    db.session.delete(other)
    db.session.delete(reader)
    db.session.delete(outsider)
    db.session.commit()


def _add(env, *, test_id, model_name, outcome, model_version="", row_uuid=None,
         hours=0, task_key="T000999"):
    from app.extensions import db
    from app.models import TestRunRecord

    base = datetime(2026, 3, 1, 9, 0, 0)
    db.session.add(TestRunRecord(
        project_id=env["project"].id,
        row_uuid=row_uuid,
        test_id=test_id,
        task_key=task_key,
        verdict=str(outcome).upper(),
        outcome=outcome,
        model_name=model_name,
        model_version=model_version,
        executor_id=env["reader"].id,
        executor_name="Reader",
        executed_at=base + timedelta(hours=hours),
        executed_on="2026-03-01",
    ))
    db.session.commit()


def _compare(project_id, left, right, **kwargs):
    from app.services.lanmatrix import dashboard_service as ds

    return ds.compare_versions(project_id, left, right, **kwargs)


def _login(client, user_id):
    with client.session_transaction() as sess:
        sess["lm_user_id"] = user_id


def test_route_is_registered(compare_app):
    rules = [
        rule for rule in compare_app.url_map.iter_rules()
        if rule.rule == "/api/v1/projects/<int:project_id>/version-compare"
    ]
    assert len(rules) == 1
    rule = rules[0]
    assert "GET" in rule.methods
    assert rule.endpoint == "lanmatrix_projects.version_compare"
    view = compare_app.view_functions[rule.endpoint]
    assert view.__name__ == "version_compare"


def test_reader_can_compare_and_strangers_cannot(env):
    _add(env, test_id="TC-1", model_name="ecu", model_version="v1",
         outcome="pass", row_uuid="row-1", hours=1)
    _add(env, test_id="TC-1", model_name="ecu", model_version="v2",
         outcome="fail", row_uuid="row-1", hours=2)
    client = env["client"]
    url = f"/api/v1/projects/{env['project'].id}/version-compare"

    anon = client.get(url, query_string={"left": "ecu@v1", "right": "ecu@v2"})
    assert anon.status_code == 401

    _login(client, env["outsider"].id)
    denied = client.get(url, query_string={"left": "ecu@v1", "right": "ecu@v2"})
    assert denied.status_code == 403

    _login(client, env["reader"].id)
    missing = client.get(url, query_string={"left": "ecu@v1"})
    assert missing.status_code == 400
    absent = client.get(
        f"/api/v1/projects/{env['other'].id + 99999}/version-compare",
        query_string={"left": "ecu@v1", "right": "ecu@v2"},
    )
    assert absent.status_code == 404

    allowed = client.get(url, query_string={"left": "ecu@v1", "right": "ecu@v2"})
    assert allowed.status_code == 200
    body = allowed.get_json()
    assert body["success"] is True
    data = body["data"]
    assert set(data) == {
        "left", "right", "summary", "page", "page_size", "total", "items",
    }
    assert data["left"] == "ecu@v1"
    assert data["right"] == "ecu@v2"
    assert data["summary"]["pass_to_fail"] == 1
    assert data["items"][0]["change"] == "pass_to_fail"
    assert "task_key" not in data["items"][0]

    leaked = client.get(
        f"/api/v1/projects/{env['other'].id}/version-compare",
        query_string={"left": "ecu@v1", "right": "ecu@v2"},
    )
    assert leaked.status_code == 403


def test_classifies_left_to_right(env):
    cases = [
        ("TC-ONLY-L", "pass", None, "only_left"),
        ("TC-ONLY-R", None, "fail", "only_right"),
        ("TC-SAME", "fail", "fail", "unchanged"),
        ("TC-P2F", "pass", "fail", "pass_to_fail"),
        ("TC-P2E", "pass", "error", "pass_to_fail"),
        ("TC-F2P", "fail", "pass", "fail_to_pass"),
        ("TC-E2P", "error", "pass", "fail_to_pass"),
        ("TC-F2E", "fail", "error", "other"),
        ("TC-P2U", "pass", "untestable", "other"),
        ("TC-U2P", "untestable", "pass", "other"),
    ]
    for test_id, left, right, _change in cases:
        if left is not None:
            _add(env, test_id=test_id, model_name="ecu", model_version="v1",
                 outcome=left, row_uuid=f"{test_id}-L", hours=1)
        if right is not None:
            _add(env, test_id=test_id, model_name="ecu", model_version="v2",
                 outcome=right, row_uuid=f"{test_id}-R", hours=2)

    data = _compare(env["project"].id, "ecu@v1", "ecu@v2", page=1, page_size=50)
    assert set(data["summary"]) == set(_CHANGES)
    found = {item["test_id"]: item for item in data["items"]}
    assert set(found) == {test_id for test_id, *_rest in cases}
    for test_id, left, right, change in cases:
        item = found[test_id]
        assert item["change"] == change
        assert item["left_outcome"] == left
        assert item["right_outcome"] == right
        assert set(item) == {
            "test_id", "left_outcome", "right_outcome", "change", "row_uuid",
        }
    assert data["summary"] == {
        "only_left": 1,
        "only_right": 1,
        "unchanged": 1,
        "pass_to_fail": 2,
        "fail_to_pass": 2,
        "other": 3,
    }
    assert data["total"] == 10
    assert sum(data["summary"].values()) == data["total"]


def test_latest_non_cancelled_run_wins(env):
    _add(env, test_id="TC-NEW", model_name="ecu", model_version="v1",
         outcome="fail", row_uuid="old-fail", hours=1, task_key="T-OLD")
    _add(env, test_id="TC-NEW", model_name="ecu", model_version="v1",
         outcome="pass", row_uuid="new-pass", hours=2, task_key="T-NEW")
    _add(env, test_id="TC-NEW", model_name="ecu", model_version="v1",
         outcome="cancelled", row_uuid="cancelled-row", hours=4, task_key="T-CANCEL")
    _add(env, test_id="TC-NEW", model_name="ecu", model_version="v2",
         outcome="pass", row_uuid="right-row", hours=3, task_key="T-RIGHT")
    _add(env, test_id="TC-GONE", model_name="ecu", model_version="v1",
         outcome="cancelled", row_uuid="gone-row", hours=1, task_key="T-GONE")
    _add(env, test_id="TC-TIE", model_name="ecu", model_version="v1",
         outcome="pass", row_uuid="tie-pass", hours=5, task_key="T-TIE1")
    _add(env, test_id="TC-TIE", model_name="ecu", model_version="v1",
         outcome="error", row_uuid="tie-error", hours=5, task_key="T-TIE2")
    _add(env, test_id="TC-TIE", model_name="ecu", model_version="v2",
         outcome="pass", row_uuid="tie-right", hours=5, task_key="T-TIE3")

    data = _compare(env["project"].id, "ecu@v1", "ecu@v2")
    found = {item["test_id"]: item for item in data["items"]}
    assert "TC-GONE" not in found
    kept = found["TC-NEW"]
    assert kept["left_outcome"] == "pass"
    assert kept["right_outcome"] == "pass"
    assert kept["change"] == "unchanged"
    assert kept["row_uuid"] == "right-row"
    assert "T-CANCEL" not in str(kept)
    tie = found["TC-TIE"]
    assert tie["left_outcome"] == "error"
    assert tie["change"] == "fail_to_pass"
    assert tie["row_uuid"] == "tie-right"


def test_same_version_label_does_not_merge_models(env):
    _add(env, test_id="TC-1", model_name="alpha", model_version="1",
         outcome="pass", row_uuid="alpha-row", hours=1)
    _add(env, test_id="TC-1", model_name="beta", model_version="1",
         outcome="fail", row_uuid="beta-row", hours=2)
    _add(env, test_id="TC-AT", model_name="pkg@core", model_version="9",
         outcome="pass", row_uuid="at-row", hours=1)

    from app.services.lanmatrix import dashboard_service as ds

    chart = ds.by_version(env["project"].id)
    assert set(chart) == {"versions", "series", "folded"}
    assert set(chart["series"]) == {
        "pass", "fail", "error", "untestable", "cancelled",
    }

    data = _compare(env["project"].id, "alpha@1", "beta@1")
    assert data["total"] == 1
    item = data["items"][0]
    assert item["test_id"] == "TC-1"
    assert item["change"] == "pass_to_fail"
    assert item["left_outcome"] == "pass"
    assert item["right_outcome"] == "fail"
    assert item["row_uuid"] == "beta-row"

    named = _compare(env["project"].id, "pkg@core@9", "beta@1")
    at_item = next(row for row in named["items"] if row["test_id"] == "TC-AT")
    assert at_item["change"] == "only_left"
    assert at_item["left_outcome"] == "pass"
    assert at_item["row_uuid"] == "at-row"


def test_bare_name_does_not_match_a_versioned_model(env):
    _add(env, test_id="TC-1", model_name="legacy", model_version="",
         outcome="pass", row_uuid="bare-row", hours=1)
    _add(env, test_id="TC-1", model_name="legacy", model_version="2",
         outcome="fail", row_uuid="ver-row", hours=3)
    _add(env, test_id="TC-ONLY-BARE", model_name="legacy", model_version="",
         outcome="error", row_uuid="bare-only", hours=2)

    data = _compare(env["project"].id, "legacy", "legacy@2")
    found = {item["test_id"]: item for item in data["items"]}
    assert found["TC-1"]["change"] == "pass_to_fail"
    assert found["TC-1"]["left_outcome"] == "pass"
    assert found["TC-1"]["right_outcome"] == "fail"
    assert found["TC-1"]["row_uuid"] == "ver-row"
    assert found["TC-ONLY-BARE"]["change"] == "only_left"
    assert found["TC-ONLY-BARE"]["right_outcome"] is None
    assert data["left"] == "legacy"
    assert data["right"] == "legacy@2"


def test_summary_ignores_pagination(env):
    for index, outcome in enumerate(("pass", "fail", "error")):
        _add(env, test_id=f"TC-{index}", model_name="ecu", model_version="v1",
             outcome=outcome, row_uuid=f"L-{index}", hours=index + 1)
    _add(env, test_id="TC-0", model_name="ecu", model_version="v2",
         outcome="fail", row_uuid="R-0", hours=4)
    _add(env, test_id="TC-1", model_name="ecu", model_version="v2",
         outcome="pass", row_uuid="R-1", hours=5)

    first = _compare(env["project"].id, "ecu@v1", "ecu@v2", page=1, page_size=1)
    second = _compare(env["project"].id, "ecu@v1", "ecu@v2", page=2, page_size=1)
    whole = _compare(env["project"].id, "ecu@v1", "ecu@v2", page=1, page_size=20)
    assert first["summary"] == second["summary"] == whole["summary"]
    assert first["total"] == second["total"] == whole["total"] == 3
    assert len(first["items"]) == len(second["items"]) == 1
    assert first["items"][0]["test_id"] != second["items"][0]["test_id"]
    assert {item["test_id"] for item in whole["items"]} == {"TC-0", "TC-1", "TC-2"}
    assert sum(first["summary"].values()) == 3
    assert first["page"] == 1
    assert second["page"] == 2
    assert first["page_size"] == 1


def test_other_project_runs_stay_out(env):
    from app.extensions import db
    from app.models import TestRunRecord

    _add(env, test_id="TC-1", model_name="ecu", model_version="v1",
         outcome="pass", row_uuid="mine", hours=1)
    db.session.add(TestRunRecord(
        project_id=env["other"].id,
        row_uuid="foreign",
        test_id="TC-1",
        task_key="T-FOREIGN",
        verdict="FAIL",
        outcome="fail",
        model_name="ecu",
        model_version="v2",
        executor_id=env["reader"].id,
        executor_name="Reader",
        executed_at=datetime(2026, 3, 1, 12, 0, 0),
        executed_on="2026-03-01",
    ))
    db.session.commit()

    data = _compare(env["project"].id, "ecu@v1", "ecu@v2")
    assert data["total"] == 1
    assert data["items"][0]["change"] == "only_left"
    assert data["items"][0]["row_uuid"] == "mine"
    assert data["summary"]["pass_to_fail"] == 0


def test_dashboard_page_exposes_compare_table():
    html = _HTML.read_text(encoding="utf-8")
    js = _JS.read_text(encoding="utf-8")
    assert 'id="lm-vc-table"' in html
    assert "version-compare" in js
    assert "row_uuid" in js
    assert "task_key" not in js
    assert "export" not in js.lower() or "version-compare" not in js.lower().split("export")[0]
