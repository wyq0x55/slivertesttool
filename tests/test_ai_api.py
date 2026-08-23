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


class FakeSequence:
    """Returns queued replies in order (provider.chat stand-in)."""

    def __init__(self, replies):
        self.replies = list(replies)

    def __call__(self, *args, **kwargs):
        return self.replies.pop(0)


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
        # Async flow: the worker (immediate mode in tests) records the error
        # on the draft itself; the POST returns the draft, not a 502.
        assert resp.status_code == 201, resp.get_data(as_text=True)
        assert resp.get_json()["data"]["status"] == "error"
        drafts = client.get(
            f"/api/v1/ai/drafts?project_id={project_env}&status=error",
            headers=headers).get_json()["data"]
        assert drafts and "接口挂了" in drafts[0]["error"]


class TestAiPage:
    def test_page_requires_login(self, client):
        resp = client.get("/lanmatrix/projects/1/ai")
        assert resp.status_code == 302

    def test_page_renders_for_member(self, client, project_env):
        headers = _login(client, _admin(client))
        resp = client.get(f"/lanmatrix/projects/{project_env}/ai",
                          headers=headers)
        assert resp.status_code == 200
        body = resp.get_data(as_text=True)
        assert "AI 草稿" in body
        assert "ai_drafts.js" in body


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

        plan_reply = json.dumps({"plans": [
            {"ref": "MDL100-01", "precond": {},
             "goal": {"veh_speed": "120"},
             "expected": {"warn_flag": "1"}, "notes": ""},
        ]}, ensure_ascii=False)
        batch_reply = json.dumps({"procedures": [
            {"ref": "MDL100-01", "steps": [
                {"no": 1, "purpose": "設定", "operation": "120",
                 "inputs": {"veh_speed": "120"},
                 "expecteds": {"warn_flag": "1"}, "timing": "即時"}],
             "missing_variables": []},
        ]}, ensure_ascii=False)
        monkeypatch.setattr(
            provider, "chat", FakeSequence([plan_reply, batch_reply]))
        resp = client.post("/api/v1/ai/drafts", headers=headers, json={
            "scenario": "procedure", "project_id": project_env,
            "payload": {
                "viewpoints": [{"ref": "MDL100-01", "title": "超速警告·正例",
                                "item_id": item_id}],
                "source_files": {"engine.c": "uint16_t veh_speed; /* 車速 */"},
                "sbs_variables": [["警告フラグ", "warn_flag"]]},
        })
        assert resp.status_code == 201, resp.get_data(as_text=True)
        draft_id = resp.get_json()["data"]["id"]

        resp = client.post(f"/api/v1/ai/drafts/{draft_id}/approve",
                           headers=headers)
        assert resp.status_code == 200, resp.get_data(as_text=True)
        applied = resp.get_json()["data"]["applied"]
        assert applied["applied"][0]["item_id"] == item_id

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


class TestUsageEndpoint:
    def test_usage_aggregates_per_project(self, client, app_ctx, project_env,
                                          monkeypatch):
        _configure_ai(app_ctx)

        class UsageFake:
            def __init__(self, reply):
                self.reply = reply

            def __call__(self, *a, usage=None, **k):
                if usage is not None:
                    usage["input_tokens"] = 500
                    usage["output_tokens"] = 50
                return self.reply

        monkeypatch.setattr(
            provider, "chat",
            UsageFake(json.dumps(_GOOD_VIEWPOINT, ensure_ascii=False)))
        headers = _login(client, _admin(client))
        resp = client.post("/api/v1/ai/drafts", headers=headers, json={
            "scenario": "viewpoint", "project_id": project_env,
            "payload": {"doc_text": "模块 MDL-100"},
        })
        assert resp.status_code == 201
        assert resp.get_json()["data"]["meta"]["usage"]["input_tokens"] == 500

        stats = client.get(
            f"/api/v1/ai/usage?project_id={project_env}",
            headers=headers).get_json()["data"]
        assert stats["totals"]["drafts"] >= 1
        assert stats["totals"]["input_tokens"] >= 500
        vp = stats["per_scenario"]["viewpoint"]
        assert vp["count"] >= 1 and vp["input_tokens"] >= 500
        assert list(stats["per_month"].values())[0]["count"] >= 1

    def test_usage_requires_project(self, client):
        headers = _login(client, _admin(client))
        resp = client.get("/api/v1/ai/usage", headers=headers)
        assert resp.status_code == 400


class TestPartialApprove:
    def _make_batch_draft(self, client, app_ctx, project_env, headers,
                          monkeypatch, item_ids):
        _configure_ai(app_ctx)
        plan_reply = json.dumps({"plans": [
            {"ref": f"R{i}", "precond": {}, "goal": {"veh_speed": "120"},
             "expected": {"warn_flag": "1"}} for i in (1, 2)
        ]}, ensure_ascii=False)
        batch_reply = json.dumps({"procedures": [
            {"ref": f"R{i}", "steps": [
                {"no": 1, "purpose": "設定", "operation": "120",
                 "inputs": {"veh_speed": "120"},
                 "expecteds": {"warn_flag": "1"}, "timing": "即時"}],
             "missing_variables": []} for i in (1, 2)
        ]}, ensure_ascii=False)
        monkeypatch.setattr(
            provider, "chat", FakeSequence([plan_reply, batch_reply]))
        resp = client.post("/api/v1/ai/drafts", headers=headers, json={
            "scenario": "procedure", "project_id": project_env,
            "payload": {
                "viewpoints": [
                    {"ref": "R1", "title": "正例1", "item_id": item_ids[0]},
                    {"ref": "R2", "title": "正例2", "item_id": item_ids[1]}],
                "source_files": {"engine.c":
                                 "uint16_t veh_speed; /* 車速 */"},
                "sbs_variables": [["警告フラグ", "warn_flag"]],
            },
        })
        assert resp.status_code == 201, resp.get_data(as_text=True)
        return resp.get_json()["data"]["id"]

    def test_approve_only_checked_refs(self, client, app_ctx, project_env,
                                       monkeypatch):
        headers = _login(client, _admin(client))
        with app_ctx.app_context():
            from app.extensions import db
            from app.models import LMUser, Project
            from app.services.lanmatrix import items_service
            admin = LMUser.query.filter_by(is_system_admin=True).first()
            project = db.session.get(Project, project_env)
            ids = [items_service.create_item(
                admin, project, {"test_id": f"P-{i}", "test_name": f"t{i}"},
                draft=True, sheet="test").id for i in (1, 2)]
            db.session.commit()

        draft_id = self._make_batch_draft(client, app_ctx, project_env,
                                          headers, monkeypatch, ids)
        resp = client.post(f"/api/v1/ai/drafts/{draft_id}/approve",
                           headers=headers, json={"refs": ["R1"]})
        assert resp.status_code == 200, resp.get_data(as_text=True)
        applied = resp.get_json()["data"]["applied"]
        assert [a["ref"] for a in applied["applied"]] == ["R1"]
        assert applied["skipped"] == [{"ref": "R2", "reason": "未勾选（部分通过）"}]

        with app_ctx.app_context():
            from app.extensions import db as _db
            from app.models import TestItemRow
            import json as _json
            r1 = _db.session.get(TestItemRow, ids[0])
            r2 = _db.session.get(TestItemRow, ids[1])
            assert _json.loads(r1.get_field("steps"))["steps"]
            assert not (r2.get_field("steps") or "").strip()

    def test_approve_all_when_refs_omitted(self, client, app_ctx, project_env,
                                           monkeypatch):
        headers = _login(client, _admin(client))
        with app_ctx.app_context():
            from app.extensions import db
            from app.models import LMUser, Project
            from app.services.lanmatrix import items_service
            admin = LMUser.query.filter_by(is_system_admin=True).first()
            project = db.session.get(Project, project_env)
            ids = [items_service.create_item(
                admin, project, {"test_id": f"A-{i}", "test_name": f"t{i}"},
                draft=True, sheet="test").id for i in (1, 2)]
            db.session.commit()

        draft_id = self._make_batch_draft(client, app_ctx, project_env,
                                          headers, monkeypatch, ids)
        resp = client.post(f"/api/v1/ai/drafts/{draft_id}/approve",
                           headers=headers)
        assert resp.status_code == 200
        applied = resp.get_json()["data"]["applied"]
        assert len(applied["applied"]) == 2
        assert applied["skipped"] == []


class TestInlineEdit:
    def test_edit_output_before_approve(self, client, app_ctx, project_env,
                                        monkeypatch):
        _configure_ai(app_ctx)
        monkeypatch.setattr(
            provider, "chat",
            lambda *a, **k: json.dumps(_GOOD_VIEWPOINT, ensure_ascii=False))
        headers = _login(client, _admin(client))
        resp = client.post("/api/v1/ai/drafts", headers=headers, json={
            "scenario": "viewpoint", "project_id": project_env,
            "payload": {"doc_text": "模块 MDL-100"},
        })
        draft_id = resp.get_json()["data"]["id"]

        edited = json.loads(json.dumps(_GOOD_VIEWPOINT))
        edited["viewpoints"][0]["title"] = "人工微调后的标题"
        resp = client.put(f"/api/v1/ai/drafts/{draft_id}", headers=headers,
                          json={"output": edited})
        assert resp.status_code == 200
        body = resp.get_json()["data"]
        assert body["output"]["viewpoints"][0]["title"] == "人工微调后的标题"
        assert body["meta"]["edited"] is True

        # The approval applies the edited output, not the original.
        resp = client.post(f"/api/v1/ai/drafts/{draft_id}/approve",
                           headers=headers)
        assert resp.status_code == 200
        with app_ctx.app_context():
            from app.models import TestItemRow
            row = TestItemRow.query.filter_by(
                project_id=project_env, deleted_at=None).order_by(
                TestItemRow.id.desc()).first()
            assert row.get_field("test_name") == "人工微调后的标题"

    def test_edit_refused_after_decision(self, client, app_ctx, project_env,
                                         monkeypatch):
        _configure_ai(app_ctx)
        monkeypatch.setattr(
            provider, "chat",
            lambda *a, **k: json.dumps(_GOOD_VIEWPOINT, ensure_ascii=False))
        headers = _login(client, _admin(client))

        resp = client.post("/api/v1/ai/drafts", headers=headers, json={
            "scenario": "viewpoint", "project_id": project_env,
            "payload": {"doc_text": "模块"},
        })
        draft_id = resp.get_json()["data"]["id"]
        client.post(f"/api/v1/ai/drafts/{draft_id}/approve", headers=headers)
        resp = client.put(f"/api/v1/ai/drafts/{draft_id}", headers=headers,
                          json={"output": {}})
        assert resp.status_code == 409


class TestSignalDictApi:
    def test_put_get_and_injection_into_generation(self, client, app_ctx,
                                                   project_env, monkeypatch):
        _configure_ai(app_ctx)
        headers = _login(client, _admin(client))
        resp = client.put("/api/v1/ai/signals", headers=headers, json={
            "project_id": project_env,
            "entries": [["実車速", "veh_speed", "uint16_t"],
                        ["警告フラグ", "warn_flag"]],
        })
        assert resp.status_code == 200, resp.get_data(as_text=True)
        assert resp.get_json()["data"]["count"] == 2

        listed = client.get(
            f"/api/v1/ai/signals?project_id={project_env}",
            headers=headers).get_json()["data"]
        assert ["実車速", "veh_speed", "uint16_t"] in listed

        # The dictionary rides into the payload server-side (highest-priority
        # registry source): the expanded steps header carries its display name.
        plan_reply = json.dumps({"plans": [
            {"ref": "R1", "precond": {}, "goal": {"veh_speed": "120"},
             "expected": {"warn_flag": "1"}}]}, ensure_ascii=False)
        batch_reply = json.dumps({"procedures": [
            {"ref": "R1", "steps": [
                {"no": 1, "purpose": "設定", "operation": "120",
                 "inputs": {"veh_speed": "120"},
                 "expecteds": {"warn_flag": "1"}, "timing": "即時"}],
             "missing_variables": []}]}, ensure_ascii=False)
        monkeypatch.setattr(
            provider, "chat", FakeSequence([plan_reply, batch_reply]))
        resp = client.post("/api/v1/ai/drafts", headers=headers, json={
            "scenario": "procedure", "project_id": project_env,
            "payload": {
                "viewpoints": [{"ref": "R1", "title": "t"}],
                "source_files": {"engine.c": "uint16_t veh_speed; /* 車速 */"},
                "sbs_variables": [["警告フラグ", "warn_flag"]],
            },
        })
        assert resp.status_code == 201, resp.get_data(as_text=True)
        header = (resp.get_json()["data"]["output"]["procedures"][0]
                  ["steps_doc"]["input_signals"][0])
        assert header == ["実車速", "veh_speed"]

    def test_rejects_duplicate_or_incomplete_rows(self, client, app_ctx,
                                                  project_env):
        headers = _login(client, _admin(client))
        resp = client.put("/api/v1/ai/signals", headers=headers, json={
            "project_id": project_env,
            "entries": [["表示", "path"], ["表示", "path"]],
        })
        assert resp.status_code == 400
        resp = client.put("/api/v1/ai/signals", headers=headers, json={
            "project_id": project_env,
            "entries": [["只有表示名"]],
        })
        assert resp.status_code == 400
