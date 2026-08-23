"""AI draft API tests (need the PostgreSQL test database via ``app_ctx``).

Covers the HTTP surface: settings masking, draft creation with a scripted
provider, listing/scoping, the review decisions, and the apply path for the
procedure scenario (steps written through ``items_service``). The pure
generation logic is covered DB-free in ``test_ai_unit.py``.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.services.ai import provider  # noqa: E402


def _login(client, user_id):
    with client.session_transaction() as sess:
        sess["lm_user_id"] = user_id
        sess["csrf_token"] = "test-csrf"
    return {"X-CSRF-Token": "test-csrf"}


def _admin(client):
    with client.application.app_context():
        from app.models import LMUser
        admin = LMUser.query.filter_by(is_system_admin=True).first()
        assert admin is not None
        return admin.id


@pytest.fixture()
def project_env(app_ctx):
    """A project with Test-Matrix fields provisioned + the admin as editor."""
    with app_ctx.app_context():
        from app.extensions import db
        from app.models import LMUser
        from app.services.lanmatrix import fields as fld
        from app.services.lanmatrix import fields_service, projects_service
        from app.services.lanmatrix.users_service import add_member

        admin = LMUser.query.filter_by(is_system_admin=True).first()
        project = projects_service.create_project(
            admin, code="AIP", name="AI 集成测试项目")
        fields_service.ensure_fields(admin, project, fld.TEST_FIELDS)
        db.session.commit()
        pid = project.id
    yield pid
    with app_ctx.app_context():
        from app.extensions import db as _db
        from app.models import Project
        row = _db.session.get(Project, pid)
        if row is not None:
            _db.session.delete(row)
            _db.session.commit()


def _configure_ai(app_ctx):
    with app_ctx.app_context():
        from app.extensions import db
        from app.models.setting import Setting
        for key, value in (("ai_api_base", "https://llm.example.com/v1"),
                           ("ai_api_key", "sk-test"),
                           ("ai_model", "test-model")):
            row = db.session.get(Setting, key)
            if row is None:
                db.session.add(Setting(key=key, value=value))
            else:
                row.value = value
        db.session.commit()


_GOOD_VIEWPOINT = {
    "module_id": "MDL-100",
    "viewpoints": [
        {"case_id": "MDL100-01", "title": "超速警告·正例", "kind": "normal",
         "precondition": "IG ON", "condition": "veh_speed > 100",
         "expected": "warn_flag = 1", "variables": ["veh_speed"]},
    ],
}


class TestAiSettings:
    def test_masked_for_admin(self, client):
        headers = _login(client, _admin(client))
        resp = client.get("/api/v1/ai/settings", headers=headers)
        assert resp.status_code == 200
        data = resp.get_json()["data"]
        assert data["ai_api_key"] in ("***set***", "***unset***")
        assert "sk-" not in json.dumps(data)

    def test_requires_admin(self, client, app_ctx):
        with app_ctx.app_context():
            from app.extensions import db
            from app.models import LMUser
            user = LMUser(username="plain", display_name="plain",
                          is_system_admin=False)
            db.session.add(user)
            db.session.commit()
            uid = user.id
        resp = client.get("/api/v1/ai/settings", headers=_login(client, uid))
        assert resp.status_code == 403


class TestDraftLifecycle:
    def test_unconfigured_returns_503(self, client, project_env):
        headers = _login(client, _admin(client))
        resp = client.post("/api/v1/ai/drafts", headers=headers, json={
            "scenario": "viewpoint", "project_id": project_env,
            "payload": {"doc_text": "x"},
        })
        assert resp.status_code == 503

    def test_create_list_get_reject(self, client, app_ctx, project_env,
                                    monkeypatch):
        _configure_ai(app_ctx)
        monkeypatch.setattr(
            provider, "chat",
            lambda *a, **k: json.dumps(_GOOD_VIEWPOINT, ensure_ascii=False))
        headers = _login(client, _admin(client))

        resp = client.post("/api/v1/ai/drafts", headers=headers, json={
            "scenario": "viewpoint", "project_id": project_env,
            "payload": {"doc_text": "模块 MDL-100：超速警告"},
        })
        assert resp.status_code == 201, resp.get_data(as_text=True)
        draft = resp.get_json()["data"]
        assert draft["status"] == "pending"
        assert draft["output"]["module_id"] == "MDL-100"

        listed = client.get(
            f"/api/v1/ai/drafts?project_id={project_env}&status=pending",
            headers=headers).get_json()["data"]
        assert any(d["id"] == draft["id"] for d in listed)

        full = client.get(f"/api/v1/ai/drafts/{draft['id']}",
                          headers=headers).get_json()["data"]
        assert full["input"]["doc_text"].startswith("模块")

        # Reject without a note is refused; with a note it lands.
        resp = client.post(f"/api/v1/ai/drafts/{draft['id']}/reject",
                           headers=headers, json={})
        assert resp.status_code == 400
        resp = client.post(f"/api/v1/ai/drafts/{draft['id']}/reject",
                           headers=headers, json={"note": "观点粒度太粗"})
        assert resp.status_code == 200
        assert client.get(f"/api/v1/ai/drafts/{draft['id']}",
                          headers=headers).get_json()["data"]["status"] == "rejected"

    def test_unknown_scenario_400(self, client, app_ctx, project_env):
        _configure_ai(app_ctx)
        headers = _login(client, _admin(client))
        resp = client.post("/api/v1/ai/drafts", headers=headers, json={
            "scenario": "telepathy", "project_id": project_env,
            "payload": {},
        })
        assert resp.status_code == 400

    def test_generation_failure_keeps_error_draft(self, client, app_ctx,
                                                  project_env, monkeypatch):
        _configure_ai(app_ctx)
        monkeypatch.setattr(provider, "chat",
                            lambda *a, **k: (_ for _ in ()).throw(
                                provider.ProviderError("接口挂了")))
        headers = _login(client, _admin(client))
        resp = client.post("/api/v1/ai/drafts", headers=headers, json={
            "scenario": "viewpoint", "project_id": project_env,
            "payload": {"doc_text": "x"},
        })
        assert resp.status_code == 502
        drafts = client.get(
            f"/api/v1/ai/drafts?project_id={project_env}&status=error",
            headers=headers).get_json()["data"]
        assert drafts and "接口挂了" in drafts[0]["error"]


class TestApproveProcedure:
    def test_apply_writes_steps(self, client, app_ctx, project_env,
                                monkeypatch):
        _configure_ai(app_ctx)
        headers = _login(client, _admin(client))
        with app_ctx.app_context():
            from app.extensions import db
            from app.models import LMUser, Project
            from app.services.lanmatrix import items_service
            admin = LMUser.query.filter_by(is_system_admin=True).first()
            project = db.session.get(Project, project_env)
            item = items_service.create_item(
                admin, project, {"test_id": "MDL100-01",
                                 "test_name": "超速警告·正例"},
                draft=True, sheet="test")
            item_id = item.id
            db.session.commit()

        good_output = {
            "steps_doc": {
                "input_signals": [["车速", "veh_speed"]],
                "expected_signals": [["警告", "warn_flag"]],
                "steps": [{"no": 1, "purpose": "设定车速", "operation": "120",
                           "inputs": ["120"], "expecteds": ["1"],
                           "timing": "即時"}],
            },
            "missing_variables": [],
        }
        monkeypatch.setattr(
            provider, "chat",
            lambda *a, **k: json.dumps(good_output, ensure_ascii=False))
        resp = client.post("/api/v1/ai/drafts", headers=headers, json={
            "scenario": "procedure", "project_id": project_env,
            "payload": {"viewpoint": {"title": "超速警告·正例",
                                      "condition": "veh_speed > 100",
                                      "expected": "warn_flag = 1"},
                        "item_id": item_id,
                        "source_files": {"engine.c": "uint16_t veh_speed;"},
                        "sbs_variables": ["veh_speed", "warn_flag"]},
        })
        assert resp.status_code == 201, resp.get_data(as_text=True)
        draft_id = resp.get_json()["data"]["id"]

        resp = client.post(f"/api/v1/ai/drafts/{draft_id}/approve",
                           headers=headers)
        assert resp.status_code == 200, resp.get_data(as_text=True)
        assert resp.get_json()["data"]["applied"]["item_id"] == item_id

        with app_ctx.app_context():
            from app.extensions import db as _db
            from app.models import TestItemRow
            row = _db.session.get(TestItemRow, item_id)
            # Test-Matrix steps live in custom_values["steps"] (the provisioned
            # field), not the legacy test_steps column.
            steps = json.loads(row.get_field("steps"))
            assert steps["input_signals"][0][1] == "veh_speed"

        # Approving twice is refused (already processed).
        resp = client.post(f"/api/v1/ai/drafts/{draft_id}/approve",
                           headers=headers)
        assert resp.status_code == 409
