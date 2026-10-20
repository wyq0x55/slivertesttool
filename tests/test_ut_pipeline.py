"""Unit-test pipeline (実施一覧 → generate → WinAMS → check → LLM repair → review).

ut-agent is replaced by a scripted bridge so the platform-side state machine
is exercised end to end over HTTP; the real bridge contract is tested in the
ut-agent repository.  Huey runs in immediate mode, so each request drives the
affected job to its next human gate before returning.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.services.ai import provider  # noqa: E402
from app.services.ut_pipeline import pipeline  # noqa: E402
from app.services.ut_pipeline.bridge import BridgeError  # noqa: E402

GOOD = b'mod,"a.c/f","f",1,1\r\n#COMMENT,x,ret\r\n,0,1\r\n'
BAD = b'mod,"a.c/f","f",1,1\r\n#COMMENT,x,ret\r\n,0,BAD\r\n'


class FakeBridge:
    """Scripted ut-agent: a CSV containing BAD fails execution, others pass."""

    def __init__(self, tmp: Path, *, generated=GOOD, execution_status="EXECUTED",
                 generate_error=None):
        self.tmp = tmp
        self.generated = generated
        self.execution_status = execution_status
        self.generate_error = generate_error
        self.executions = 0
        self.stub_builds = None
        self.source_rel = None
        self.blocked_reason = {"code": "ASSET_MISSING", "path": "AMS_Setting"}

    def index(self, manifest, index_csv, product_root):
        return [
            {"row": 1, "callcnt": 1, "source_name": "a.c", "function": "f",
             "source_rel": "src/a.c", "unit_rel": "WinAMS/src/a.c/f"},
            {"row": 2, "callcnt": 2, "source_name": "a.c", "function": "g",
             "source_rel": "src/a.c", "unit_rel": "WinAMS/src/a.c/g"},
        ]

    def constraints(self, manifest):
        return {"baseline": {"id": "psd-rebuild", "version": "1.1"},
                "rules": [{"rule_id": "psd.4.compare", "meaning": "境界值"}],
                "revision_rules": ["列不得改动"], "coverage_policy": {"c1": True}}

    def generate(self, manifest, index_csv, product_root, out, functions):
        if self.generate_error:
            raise BridgeError(self.generate_error)
        out.mkdir(parents=True, exist_ok=True)
        units = []
        for name in functions:
            unit = {"function": name, "status": "VALIDATED", "testcsv": None, "error": None}
            if self.generated is not None:
                path = out / f"{name}.csv"
                path.write_bytes(self.generated)
                unit["testcsv"] = str(path)
            else:
                unit.update(status="UNSUPPORTED", error="loop construct")
            units.append(unit)
        return {"ok": True, "provenance": {"baseline_ref": "psd-rebuild@1.1"}, "units": units}

    def execute(self, unit_dir, candidate, out, stub_builds=None, source_rel=""):
        self.executions += 1
        self.stub_builds = stub_builds
        self.source_rel = source_rel
        out.mkdir(parents=True)
        evidence = {"status": self.execution_status, "unit_dir": str(unit_dir)}
        if self.execution_status == "BLOCKED":
            evidence["blocked_reasons"] = [self.blocked_reason]
        (out / "execution.json").write_text(json.dumps(evidence), encoding="utf-8")
        return evidence

    def check(self, candidate, *, manifest, reference=None, previous=None, execution=None):
        if execution is None:
            return {"verdict": "FORMAT_OK", "issues": []}
        evidence = json.loads(Path(execution).read_text(encoding="utf-8"))
        if evidence["status"] == "BLOCKED":
            return {"verdict": "BLOCKED", "issues": [{
                "code": "EXECUTION_BLOCKED", "severity": "error", "repairable": False,
                "message": "env", "detail": evidence["blocked_reasons"]}]}
        if b"BAD" in Path(candidate).read_bytes():
            return {"verdict": "FAIL", "issues": [{
                "code": "ORACLE_MISMATCH", "severity": "error", "repairable": True,
                "message": "1 mismatch", "detail": [{"row": 0, "column": "ret"}]}]}
        return {"verdict": "PASS", "issues": []}


def _llm_reply(csv_bytes):
    text = None if csv_bytes is None else csv_bytes.decode("cp932")
    return json.dumps({"csv": text, "rationale": "fix", "changes": []})


@pytest.fixture()
def env(app_ctx, monkeypatch, tmp_path):
    from app.jobqueue import tasks

    monkeypatch.setattr(tasks, "_worker_app", app_ctx)
    with app_ctx.app_context():
        from app.extensions import db
        from app.models import LMUser
        from app.services.ai import config as ai_config
        from app.services.lanmatrix import projects_service

        admin = LMUser.query.filter_by(is_system_admin=True).first()
        project = projects_service.create_project(
            admin, code="UTP", name="UT 流水线", project_type="unit_test")
        db.session.commit()
        ai_config.update_ai_config({"ai_api_base": "http://127.0.0.1:9/v1",
                                    "ai_api_key": "local", "ai_model": "qwen3.8-27b-gsq"})
        pid, uid = project.id, admin.id
    client = app_ctx.test_client()
    with client.session_transaction() as sess:
        sess["lm_user_id"] = uid
        sess["csrf_token"] = "t"
    bridge = FakeBridge(tmp_path)
    monkeypatch.setattr(pipeline, "get_bridge", lambda: bridge)

    class Env:
        pass

    e = Env()
    e.client, e.pid, e.bridge, e.headers = client, pid, bridge, {"X-CSRF-Token": "t"}
    e.base = f"/api/v1/projects/{pid}/ut"

    def call(method, path, **kwargs):
        resp = getattr(client, method)(e.base + path, headers=e.headers, **kwargs)
        return resp.status_code, resp.get_json()

    e.call = call
    return e


def _configure(env, rounds=5):
    status, body = env.call("put", "/config", json={
        "manifest_path": "m.json", "index_csv": "i.csv", "product_root": "P",
        "work_root": "W", "max_repair_rounds": rounds, "repair_model": "qwen3.8-27b-gsq"})
    assert status == 200, body
    status, body = env.call("post", "/targets/import")
    assert status == 200 and body["data"] == {"total": 2, "added": 2, "removed": 0}
    _, body = env.call("get", "/targets")
    return {t["function"]: t["id"] for t in body["data"]}


def _enqueue(env, *target_ids):
    status, body = env.call("post", "/batches", json={"target_ids": list(target_ids)})
    assert status == 202, body
    _, jobs = env.call("get", "/jobs", query_string={"batch_id": body["data"]["id"]})
    return [job["id"] for job in jobs["data"]]


def _job(env, job_id):
    status, body = env.call("get", f"/jobs/{job_id}")
    assert status == 200, body
    return body["data"]


def test_queue_requires_binding(env):
    status, body = env.call("post", "/batches", json={"target_ids": [1]})
    assert status == 400 and body["error"]["code"] == "UT_NOT_CONFIGURED"


def test_page_shell_renders(env):
    resp = env.client.get(f"/lanmatrix/projects/{env.pid}/ut")
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    assert "实施一览" in html and "js/lanmatrix/ut_pipeline.js" in html


def test_project_navigation_and_settings_keep_their_type(env, app_ctx):
    from app.extensions import db
    from app.models import LMUser
    from app.services.lanmatrix import projects_service

    with app_ctx.app_context():
        admin = LMUser.query.filter_by(is_system_admin=True).first()
        silver = projects_service.create_project(admin, code="SIL", name="Silver")
        db.session.commit()
        silver_id = silver.id

    ut = f"/lanmatrix/projects/{env.pid}"
    silver = f"/lanmatrix/projects/{silver_id}"
    for suffix in ("/members", "/audit", "/settings/binding", "/tasks"):
        response = env.client.get(ut + suffix)
        assert response.status_code == 200
        html = response.get_data(as_text=True)
        assert f'href="{ut}/tasks"' in html
        assert f'href="{ut}/ut"' in html
        assert ">测试矩阵</span>" not in html
        if suffix not in ("/tasks",):
            assert ">项目绑定</a>" in html
            assert ">模型管理</a>" not in html
            assert ">回收站</a>" not in html

    for suffix in ("/settings", "/fields", "/models", "/trash"):
        response = env.client.get(ut + suffix)
        assert response.status_code == 302
        assert response.headers["Location"] == ut + "/settings/binding"

    for suffix in ("/members", "/fields", "/models", "/tasks", "/dashboard"):
        response = env.client.get(silver + suffix)
        assert response.status_code == 200
        html = response.get_data(as_text=True)
        assert ">测试矩阵</span>" in html
        assert ">实施一览</span>" not in html
    assert env.client.get(silver + "/ut").headers["Location"] == silver
    assert env.client.get(silver + "/settings").headers["Location"] == silver + "/fields"
    response = env.client.get(ut + "/dashboard")
    assert response.status_code == 200
    html = response.get_data(as_text=True)
    assert "机器通过" in html and "审核通过" in html
    assert 'id="lm-version-compare"' not in html
    assert "js/lanmatrix/dashboard.js" in html


def test_project_shell_does_not_offer_an_outsider_ut_actions(env, app_ctx):
    from app.extensions import db
    from app.models import LMUser

    with app_ctx.app_context():
        outsider = LMUser(username="outsider", display_name="Outsider", password_hash="x")
        db.session.add(outsider)
        db.session.commit()
        outsider_id = outsider.id
    with env.client.session_transaction() as session:
        session["lm_user_id"] = outsider_id
    response = env.client.get(f"/lanmatrix/projects/{env.pid}/ut")
    assert response.status_code == 200
    html = response.get_data(as_text=True)
    assert "无法访问项目" in html
    assert 'id="ut-enqueue"' not in html


def test_review_rejects_a_candidate_replaced_since_the_page_loaded(env):
    targets = _configure(env)
    [job_id] = _enqueue(env, targets["f"])
    original = _job(env, job_id)["current_attempt_id"]
    status, body = env.call("post", f"/jobs/{job_id}/revision",
                           json={"csv": GOOD.decode(), "note": "new candidate"})
    assert status == 202, body
    current = _job(env, job_id)["current_attempt_id"]
    assert current != original

    status, body = env.call("post", f"/jobs/{job_id}/review", json={
        "decision": "approve", "expected_attempt_id": None,
    })
    assert status == 409, body
    assert _job(env, job_id)["state"] == "awaiting_review"

    status, body = env.call("post", f"/jobs/{job_id}/review", json={
        "decision": "approve", "expected_attempt_id": original,
    })
    assert status == 409, body
    assert body["error"]["code"] == "VERSION_CONFLICT"
    assert _job(env, job_id)["state"] == "awaiting_review"

    status, body = env.call("post", f"/jobs/{job_id}/review", json={
        "decision": "approve", "expected_attempt_id": current,
    })
    assert status == 200, body
    assert _job(env, job_id)["state"] == "approved"


@pytest.mark.parametrize("role,expected", [
    ("reader", (False, False, False)),
    ("editor", (True, False, False)),
    ("reviewer", (False, True, False)),
    ("project_admin", (True, True, True)),
])
def test_project_shell_exposes_authoritative_action_capabilities(env, app_ctx, role, expected):
    import re

    from app.extensions import db
    from app.models import LMUser, ProjectMember

    with app_ctx.app_context():
        member = LMUser(username=role, display_name=role, password_hash="x")
        db.session.add(member)
        db.session.flush()
        db.session.add(ProjectMember(project_id=env.pid, user_id=member.id, role=role))
        db.session.commit()
        member_id = member.id
    with env.client.session_transaction() as session:
        session["lm_user_id"] = member_id
    response = env.client.get(f"/lanmatrix/projects/{env.pid}/ut")
    assert response.status_code == 200
    html = response.get_data(as_text=True)
    capabilities = json.loads(re.search(r"projectCapabilities: (\{[^\n]+\})", html).group(1))
    assert capabilities["view"] is True
    assert tuple(capabilities[key] for key in ("edit", "review", "configure")) == expected


def test_passing_generation_waits_for_review_then_approves(env):
    targets = _configure(env)
    [job_id] = _enqueue(env, targets["f"])
    job = _job(env, job_id)
    assert job["state"] == "awaiting_review"
    assert [(a["origin"], a["verdict"]) for a in job["attempts"]] == [("GENERATOR", "PASS")]
    assert job["batch"]["status"] == "done"
    assert job["batch"]["provenance"] == {"baseline_ref": "psd-rebuild@1.1"}

    _, targets_after = env.call("get", "/targets")
    latest = {t["function"]: t["latest_job"] for t in targets_after["data"]}
    assert latest["f"]["state"] == "awaiting_review" and latest["g"] is None

    status, body = env.call("post", f"/jobs/{job_id}/review", json={"decision": "approve"})
    assert status == 200 and body["data"]["state"] == "approved"
    status, body = env.call("post", f"/jobs/{job_id}/review", json={"decision": "reject",
                                                                     "note": "late"})
    assert status == 400 and body["error"]["code"] == "INVALID_STATE"


def test_llm_repairs_a_failing_candidate(env, monkeypatch):
    targets = _configure(env)
    env.bridge.generated = BAD
    seen = []

    def fake_chat(messages, **kwargs):
        seen.append((messages, kwargs))
        return _llm_reply(GOOD)

    monkeypatch.setattr(provider, "chat", fake_chat)
    [job_id] = _enqueue(env, targets["f"])
    job = _job(env, job_id)
    assert job["state"] == "awaiting_review" and job["repair_rounds"] == 1
    assert [(a["origin"], a["verdict"]) for a in job["attempts"]] == [
        ("GENERATOR", "FAIL"), ("LLM", "PASS")]
    reviser = job["attempts"][1]["reviser"]
    assert reviser["model"] == "qwen3.8-27b-gsq" and len(reviser["prompt_sha256"]) == 64
    messages, kwargs = seen[0]
    assert kwargs["model"] == "qwen3.8-27b-gsq"
    assert "psd.4.compare" in messages[0]["content"] and "列不得改动" in messages[0]["content"]
    assert "ORACLE_MISMATCH" in messages[1]["content"]

    status, body = env.call(
        "get", f"/jobs/{job_id}/attempts/{job['attempts'][1]['id']}/csv",
        query_string={"format": "text"})
    assert status == 200 and body["data"]["text"] == GOOD.decode("cp932")


def test_exhausted_rounds_hand_over_to_a_human(env, monkeypatch):
    targets = _configure(env, rounds=2)
    env.bridge.generated = BAD
    replies = [_llm_reply(BAD), _llm_reply(None)]
    monkeypatch.setattr(provider, "chat", lambda *a, **k: replies.pop(0))
    [job_id] = _enqueue(env, targets["f"])
    job = _job(env, job_id)
    assert job["state"] == "needs_human" and job["repair_rounds"] == 2
    assert "上限 2" in job["reason"]
    assert [(a["origin"], a["verdict"]) for a in job["attempts"]] == [
        ("GENERATOR", "FAIL"), ("LLM", "FAIL"), ("LLM", "INVALID")]

    status, body = env.call("post", f"/jobs/{job_id}/review", json={"decision": "approve"})
    assert status == 400 and body["error"]["code"] == "VALIDATION_ERROR"

    # A failing human revision returns to the human instead of the model.
    status, _ = env.call("post", f"/jobs/{job_id}/revision",
                         json={"csv": BAD.decode("cp932"), "note": "try"})
    assert status == 202
    assert _job(env, job_id)["state"] == "needs_human"
    status, _ = env.call("post", f"/jobs/{job_id}/revision",
                         json={"csv": GOOD.decode("cp932").replace("\r\n", "\n")})
    job = _job(env, job_id)
    assert job["state"] == "awaiting_review"
    assert job["attempts"][-1]["origin"] == "HUMAN"
    status, body = env.call("post", f"/jobs/{job_id}/review", json={"decision": "reject"})
    assert status == 400
    status, body = env.call("post", f"/jobs/{job_id}/review",
                            json={"decision": "reject", "note": "期望值来源不明"})
    assert body["data"]["state"] == "rejected"


def test_blocked_environment_does_not_consume_rounds(env, monkeypatch):
    targets = _configure(env)
    env.bridge.execution_status = "BLOCKED"
    monkeypatch.setattr(provider, "chat", lambda *a, **k: pytest.fail("LLM must not run"))
    [job_id] = _enqueue(env, targets["f"])
    job = _job(env, job_id)
    assert job["state"] == "blocked" and job["repair_rounds"] == 0
    assert "ASSET_MISSING" in job["reason"]

    env.bridge.execution_status = "EXECUTED"
    status, _ = env.call("post", f"/jobs/{job_id}/retry")
    assert status == 202
    assert _job(env, job_id)["state"] == "awaiting_review"
    assert env.bridge.executions == 2


def test_stub_builds_are_normalised_and_passed_to_execution(env):
    status, body = env.call("put", "/config", json={
        "manifest_path": "m.json", "index_csv": "i.csv", "product_root": "P",
        "work_root": "W", "stub_builds": "  C:\\STUB\\stub1 \r\n\r\nC:\\STUB\\stub2\n"})
    assert status == 200, body
    assert body["data"]["stub_builds"] == "C:\\STUB\\stub1\nC:\\STUB\\stub2"
    env.call("post", "/targets/import")
    _, body = env.call("get", "/targets")
    targets = {t["function"]: t["id"] for t in body["data"]}

    env.bridge.execution_status = "BLOCKED"
    env.bridge.blocked_reason = {"code": "STUB_BUILD_UNRESOLVED", "path": "C:\\STUB\\stub1",
                                 "detail": "stub1: AMSTB_dep"}
    [job_id] = _enqueue(env, targets["f"])
    assert env.bridge.stub_builds == ["C:\\STUB\\stub1", "C:\\STUB\\stub2"]
    assert env.bridge.source_rel == "src/a.c"
    job = _job(env, job_id)
    assert job["state"] == "blocked" and job["repair_rounds"] == 0
    assert "STUB_BUILD_UNRESOLVED stub1: AMSTB_dep" in job["reason"]


def test_unreachable_model_is_an_error_and_retry_resumes_repair(env, monkeypatch):
    targets = _configure(env)
    env.bridge.generated = BAD

    def down(*args, **kwargs):
        raise provider.ProviderError("connection refused")

    monkeypatch.setattr(provider, "chat", down)
    [job_id] = _enqueue(env, targets["f"])
    job = _job(env, job_id)
    assert job["state"] == "error" and job["repair_rounds"] == 0

    monkeypatch.setattr(provider, "chat", lambda *a, **k: _llm_reply(GOOD))
    env.call("post", f"/jobs/{job_id}/retry")
    job = _job(env, job_id)
    assert job["state"] == "awaiting_review" and job["repair_rounds"] == 1


def test_generation_outcomes_reach_a_human(env):
    targets = _configure(env)
    env.bridge.generated = None
    [job_id] = _enqueue(env, targets["g"])
    job = _job(env, job_id)
    assert job["state"] == "needs_human" and job["generation_status"] == "UNSUPPORTED"
    assert "loop construct" in job["reason"]

    env.bridge.generate_error = "extractor crashed"
    [job_id] = _enqueue(env, targets["f"])
    job = _job(env, job_id)
    assert job["state"] == "error" and job["batch"]["status"] == "failed"
    status, body = env.call("post", f"/jobs/{job_id}/retry")
    assert status == 400 and body["error"]["code"] == "INVALID_STATE"


def test_next_state_is_pure_and_total():
    fail = {"verdict": "FAIL", "issues": [{"code": "X", "severity": "error"}]}
    assert pipeline.next_state(fail, "LLM", 0, 5)[0] == "repairing"
    assert pipeline.next_state(fail, "GENERATOR", 0, 0)[0] == "needs_human"
    assert pipeline.next_state(fail, "HUMAN", 0, 5)[0] == "needs_human"
    blocked = {"verdict": "BLOCKED", "issues": [
        {"code": "COVERAGE_UNAVAILABLE", "severity": "error"}]}
    assert pipeline.next_state(blocked, "LLM", 0, 5)[0] == "needs_human"


def _member(app_ctx, pid, role):
    from app.extensions import db
    from app.models import LMUser, ProjectMember

    with app_ctx.app_context():
        user = LMUser(username=f"ws-{role}", display_name=role, password_hash="x")
        db.session.add(user)
        db.session.flush()
        db.session.add(ProjectMember(project_id=pid, user_id=user.id, role=role))
        db.session.commit()
        return user.id


def _as(env, user_id):
    with env.client.session_transaction() as session:
        session["lm_user_id"] = user_id


def _ut_reviews(env, **query):
    resp = env.client.get("/api/v1/me/reviews", query_string=query, headers=env.headers)
    assert resp.status_code == 200, resp.get_json()
    data = resp.get_json()["data"]
    return [r for r in data["reviews"] if r["kind"] == "ut_job"], data["queue_counts"]


def test_workspace_review_queue_routes_gated_jobs_to_members_who_can_act(env, app_ctx):
    from app.models import LMUser, Notification

    targets = _configure(env)
    [job_id] = _enqueue(env, targets["f"])
    with app_ctx.app_context():
        admin_id = LMUser.query.filter_by(is_system_admin=True).first().id
    reviewer = _member(app_ctx, env.pid, "reviewer")
    editor = _member(app_ctx, env.pid, "editor")
    reader = _member(app_ctx, env.pid, "reader")

    _as(env, reviewer)
    rows, counts = _ut_reviews(env)
    assert [r["id"] for r in rows] == [job_id] and counts["pending"] == 1
    row = rows[0]
    assert row["test_id"] == "f" and row["review_status"] == "pending"
    assert row["bulk_approvable"] is False and row["review_verdict"] == "PASS"
    assert row["link"] == f"/lanmatrix/projects/{env.pid}/tasks?job={job_id}"
    for user in (editor, reader):  # awaiting_review needs the review capability
        _as(env, user)
        assert _ut_reviews(env)[0] == []

    _as(env, reviewer)
    status, body = env.call("post", f"/jobs/{job_id}/review",
                            json={"decision": "reject", "note": "期望值不对"})
    assert status == 200, body
    assert _ut_reviews(env)[0] == []
    decided, counts = _ut_reviews(env, status="decided")
    assert [r["review_status"] for r in decided] == ["rejected"] and counts["decided"] == 1

    _as(env, admin_id)
    rejected, counts = _ut_reviews(env, role="requester", status="rejected")
    assert [r["review_note"] for r in rejected] == ["期望值不对"] and counts["rejected"] == 1
    with app_ctx.app_context():
        notes = (Notification.query.filter_by(user_id=admin_id, ref_type="ut_job")
                 .order_by(Notification.id).all())
        assert [n.type for n in notes] == ["task.finished", "review.rejected"]
        assert notes[0].link_url.endswith(f"/tasks?job={job_id}")

    from app.models import AuditLog
    with app_ctx.app_context():
        trail = [(a.action, a.actor_id) for a in
                 AuditLog.query.filter_by(project_id=env.pid).order_by(AuditLog.id)
                 if a.action.startswith("ut.")]
    assert trail == [("ut.config.update", admin_id), ("ut.targets.import", admin_id),
                     ("ut.batch.create", admin_id), ("ut.job.reject", reviewer)]

    resp = env.client.get("/api/v1/me/overview", headers=env.headers)
    card = next(p for p in resp.get_json()["data"]["projects"] if p["id"] == env.pid)
    assert (card["ut_targets"], card["ut_gates"], card["ut_approved"]) == (2, 0, 0)

    # An unknown status is the pending inbox for every source, never "all decided".
    assert _ut_reviews(env, role="requester", status="bogus")[0] == []


def test_gate_notifications_stop_after_membership_is_revoked(env, app_ctx):
    from app.extensions import db
    from app.models import LMUser, Notification, ProjectMember

    targets = _configure(env)
    editor = _member(app_ctx, env.pid, "editor")
    _as(env, editor)
    [job_id] = _enqueue(env, targets["f"])
    with app_ctx.app_context():
        assert Notification.query.filter_by(user_id=editor, ref_type="ut_job").count() == 1
        ProjectMember.query.filter_by(project_id=env.pid, user_id=editor).delete()
        db.session.commit()
        admin_id = LMUser.query.filter_by(is_system_admin=True).first().id
    _as(env, admin_id)
    status, body = env.call("post", f"/jobs/{job_id}/review",
                            json={"decision": "reject", "note": "retired"})
    assert status == 200, body
    with app_ctx.app_context():
        assert Notification.query.filter_by(user_id=editor, ref_type="ut_job").count() == 1


def test_editor_sees_jobs_that_need_a_revision(env, app_ctx, monkeypatch):
    targets = _configure(env, rounds=0)
    env.bridge.generated = BAD
    [job_id] = _enqueue(env, targets["f"])
    assert _job(env, job_id)["state"] == "needs_human"
    _as(env, _member(app_ctx, env.pid, "editor"))
    rows, _ = _ut_reviews(env)
    assert [(r["id"], r["needs_note"]) for r in rows] == [(job_id, True)]


def test_workspace_task_list_and_kpis_map_jobs_onto_run_statuses(env, app_ctx):
    targets = _configure(env, rounds=0)
    [passing] = _enqueue(env, targets["g"])           # awaiting_review
    env.bridge.generated = BAD
    [failing] = _enqueue(env, targets["f"])           # needs_human

    def tasks(**query):
        resp = env.client.get("/api/v1/me/tasks", query_string=query, headers=env.headers)
        assert resp.status_code == 200, resp.get_json()
        return [job["id"] for job in resp.get_json()["data"]["ut_jobs"]]

    assert sorted(tasks()) == sorted([passing, failing])
    assert tasks(status="failed") == [failing]
    assert tasks(status="passed") == [passing] and tasks(status="running") == []
    assert tasks(q="g") == [passing] and tasks(project_id=env.pid, mine="1") != []

    kpi = env.client.get("/api/v1/me/overview",
                         headers=env.headers).get_json()["data"]["kpi"]
    # A machine PASS may still be waiting for human review.
    assert (kpi["failed"], kpi["passed"], kpi["running"], kpi["total"]) == (1, 1, 0, 2)

    env.call("post", f"/jobs/{passing}/review", json={"decision": "approve"})
    assert tasks(status="passed") == [passing]
    env.call("post", f"/jobs/{failing}/review", json={"decision": "approve", "note": "override"})
    assert tasks(status="failed") == [failing]
    assert tasks(status="passed") == [passing]
    response = env.client.get("/api/v1/me/tasks?limit=1", headers=env.headers).get_json()["data"]
    assert len(response["tasks"]) + len(response["ut_jobs"]) == 1
    assert response["truncated"] is True
    _enqueue(env, targets["f"])
    card = next(p for p in env.client.get("/api/v1/projects").get_json()["data"]["projects"]
                if p["id"] == env.pid)
    home = next(p for p in env.client.get("/api/v1/me/overview").get_json()["data"]["projects"]
                if p["id"] == env.pid)
    assert card["ut_jobs_approved"] == home["ut_approved"] == 1
    assert card["ut_jobs_needs_human"] == 1


def test_recent_entry_limit_applies_across_both_execution_types():
    from app.services.ut_pipeline.workspace import recent_entries

    silver = [{"task_id": "T000001", "created_at": "2026-10-18T10:00:00Z"}]
    unit = [{"id": 1, "created_at": "2026-10-19T10:00:00Z"}]
    tasks, jobs, truncated = recent_entries(silver, unit, 1)
    assert tasks == [] and jobs == unit and truncated is True


@pytest.mark.parametrize("failure,status", [("environment", "error"), ("model", "failed")])
@pytest.mark.parametrize("decision", ["approve", "reject"])
def test_review_does_not_reclassify_machine_evidence(env, monkeypatch, failure, status, decision):
    targets = _configure(env)
    if failure == "environment":
        env.bridge.execution_status = "BLOCKED"
    else:
        env.bridge.generated = BAD

        def down(*args, **kwargs):
            raise provider.ProviderError("offline")

        monkeypatch.setattr(provider, "chat", down)
    [job_id] = _enqueue(env, targets["f"])

    def snapshot():
        payload = env.client.get("/api/v1/me/tasks", query_string={"status": status}).get_json()["data"]
        counts = env.client.get("/api/v1/me/overview").get_json()["data"]["kpi"]
        return [j["id"] for j in payload["ut_jobs"]], counts[status]

    assert snapshot() == ([job_id], 1)
    response, body = env.call("post", f"/jobs/{job_id}/review",
                             json={"decision": decision, "note": "human assessment"})
    assert response == 200, body
    assert snapshot() == ([job_id], 1)
