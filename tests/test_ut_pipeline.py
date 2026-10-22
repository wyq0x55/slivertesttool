"""Unit-test pipeline (実施一覧 → generate → WinAMS → check → LLM repair → review).

ut-agent is replaced by a scripted bridge so the platform-side state machine
is exercised end to end over HTTP; the real bridge contract is tested in the
ut-agent repository.  Huey runs in immediate mode, so each request drives the
affected job to its next human gate before returning.
"""

from __future__ import annotations

import json
import hashlib
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.services.ai import provider  # noqa: E402
from app.services.ut_pipeline import pipeline  # noqa: E402
from app.services.ut_pipeline.bridge import BridgeError, BridgeSettings, UtAgentBridge  # noqa: E402

GOOD = b'mod,"a.c/f","f",1,1\r\n#COMMENT,x,ret\r\n,0,1\r\n'
BAD = b'mod,"a.c/f","f",1,1\r\n#COMMENT,x,ret\r\n,0,BAD\r\n'


def test_execution_passes_the_explicit_converter_and_source(monkeypatch, tmp_path):
    bridge = UtAgentBridge(BridgeSettings.from_config({
        "UT_OMF_CONVERTER": r"C:\WinAMS\BIN\v850ghsomf.exe",
    }))
    calls = []

    def invoke(args, *, timeout):
        assert timeout == 3 * bridge.settings.execute_timeout + 120
        calls.append(args)
        return {"execution": {"status": "BLOCKED"}}

    monkeypatch.setattr(bridge, "_run", invoke)
    bridge.execute(tmp_path, tmp_path / "c.csv", tmp_path / "out",
                   stub_builds=["S1", "S2"], source_rel="src/a.c", intents=tmp_path / "intents.json")
    [args] = calls
    assert args[args.index("--source-rel") + 1] == "src/a.c"
    assert args[args.index("--intents") + 1] == str(tmp_path / "intents.json")
    assert args[args.index("--omf-converter") + 1] == r"C:\WinAMS\BIN\v850ghsomf.exe"
    assert [args[i + 1] for i, value in enumerate(args) if value == "--stub-build"] == ["S1", "S2"]


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
        # Bytes WinAMS "captures" for any candidate; None means no capture.
        self.captured = None
        # Additional error codes reported next to ORACLE_MISMATCH for BAD candidates.
        self.extra_codes = ()
        # Data rows C1 coverage needs; fewer rows fail with a coverage gap.
        self.coverage_rows = 0
        # Completeness gaps reported with every executed check.
        self.gaps = []
        self.checked_intents = []
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
                "revision_rules": ["列不得改动"], "coverage_policy": {"c1": True}, "effective_test_policy": {"version": "1", "c1": True, "mcdc": False}, "test_policy_sha256": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb", "test_rules_ref": "universal-test-rules@1", "test_rules": {"version": "1"}}

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

    def execute(self, unit_dir, candidate, out, stub_builds=None, source_rel="", intents=None):
        self.executions += 1
        self.stub_builds = stub_builds
        self.source_rel = source_rel
        out.mkdir(parents=True)
        evidence = {"status": self.execution_status, "unit_dir": str(unit_dir)}
        if self.execution_status == "BLOCKED":
            evidence["blocked_reasons"] = [self.blocked_reason]
        elif self.captured is not None:
            captured = self.captured
            if callable(captured):
                captured = captured(Path(candidate).read_bytes())
            evidence["captured_candidate"] = str(out / "review-candidate.csv")
            (out / "review-candidate.csv").write_bytes(captured)
        (out / "execution.json").write_text(json.dumps(evidence), encoding="utf-8")
        return evidence

    def check(self, candidate, *, manifest, reference=None, previous=None, execution=None,
              intents=None, ir_json=None):
        self.checked_intents.append(intents)
        data = Path(candidate).read_bytes()
        rows = [line for line in data.split(b"\r\n") if line.startswith(b",")]
        completeness = {"unresolved_count": len(self.gaps), "unresolved": self.gaps}
        digest = hashlib.sha256(data).hexdigest()
        proved = ["b1:T"] if b",5," in data and b"LLM add b1:T" in data else []
        results = [{"row": len(rows) - 1, "obligation_id": "b1:T",
                    "status": "WITNESSED" if proved else "REJECTED", "new": True,
                    "observed": bool(proved), "reason": "x must equal 5"}] if (
                        self.gaps and b"LLM add b1:T" in data) else []
        base = {"completeness": completeness, "candidate_sha256": digest,
                "candidate_witnesses": {"contract": "ut-agent.candidate-witness/1",
                    "evidence": {"identified": True}, "candidate_sha256": digest,
                    "witnessed_obligation_ids": proved, "results": results}}
        if execution is None:
            if results and not proved:
                return {**base, "verdict": "FAIL", "issues": [{
                    "code": "INPUT_WITNESS_REJECTED", "severity": "error", "repairable": True,
                    "message": "x must equal 5", "detail": results}]}
            return {**base, "verdict": "FORMAT_OK", "issues": []}
        evidence = json.loads(Path(execution).read_text(encoding="utf-8"))
        if evidence["status"] == "BLOCKED":
            return {"verdict": "BLOCKED", "issues": [{
                "code": "EXECUTION_BLOCKED", "severity": "error", "repairable": False,
                "message": "env", "detail": evidence["blocked_reasons"]}]}
        if b"BAD" in data:
            return {**base, "verdict": "FAIL", "issues": [{
                "code": "ORACLE_MISMATCH", "severity": "error", "repairable": True,
                "message": "1 mismatch", "detail": [{"row": 0, "column": "ret"}]}] + [
                {"code": code, "severity": "error", "repairable": True, "message": code}
                for code in self.extra_codes]}
        issues = []
        if len(rows) < self.coverage_rows:
            issues.append({"code": "COVERAGE_C1_INCOMPLETE", "severity": "error",
                           "repairable": True, "message": "C1 < 100%"})
        if any(row.endswith(b",") for row in rows):
            issues.append({"code": "EXPECTED_VALUE_PENDING", "severity": "error",
                           "repairable": True, "message": "blank expected"})
        return {**base, "verdict": "FAIL" if issues else "PASS",
                "issues": issues}


def _llm_reply(*rows, declined=()):
    return json.dumps({"rows": list(rows), "rationale": "fix",
                       "declined": [{"obligation": oid, "reason": "no column"} for oid in declined]})


def _row(obligation, value="5"):
    return {"obligation": obligation, "base_row": 1, "inputs": {"I1": value}, "reason": "x"}


def _fill_blank(csv_bytes):
    """What a WinAMS capture does to blank expected values in the fakes."""
    return csv_bytes.replace(b",\r\n", b",1\r\n")


GAP = {"obligation_id": "b1:T", "kind": "branch", "gap_class": "MISSING_CASE",
       "solve_status": "SAT", "decision": "x == 5"}


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


def test_llm_rows_close_a_coverage_gap_and_the_capture_fills_them(env, monkeypatch):
    targets = _configure(env)
    env.bridge.coverage_rows = 2
    env.bridge.captured = _fill_blank
    seen = []

    def fake_chat(messages, **kwargs):
        seen.append((messages, kwargs))
        return _llm_reply(_row("COVERAGE_C1_INCOMPLETE"))

    monkeypatch.setattr(provider, "chat", fake_chat)
    [job_id] = _enqueue(env, targets["f"])
    job = _job(env, job_id)
    assert job["state"] == "awaiting_review" and job["repair_rounds"] == 1
    assert [(a["origin"], a["verdict"]) for a in job["attempts"]] == [
        ("GENERATOR", "FAIL"), ("LLM", "FAIL"), ("CAPTURE", "PASS")]
    reviser = job["attempts"][1]["reviser"]
    assert reviser["model"] == "qwen3.8-27b-gsq" and len(reviser["prompt_sha256"]) == 64
    assert reviser["claimed"] == ["COVERAGE_C1_INCOMPLETE"]
    messages, kwargs = seen[0]
    assert kwargs["model"] == "qwen3.8-27b-gsq"
    assert kwargs["timeout"] == 900
    assert "psd.4.compare" in messages[0]["content"] and "列不得改动" in messages[0]["content"]
    assert "COVERAGE_C1_INCOMPLETE" in messages[1]["content"] and "I1 = x" in messages[1]["content"]

    status, body = env.call(
        "get", f"/jobs/{job_id}/attempts/{job['attempts'][2]['id']}/csv",
        query_string={"format": "text"})
    assert status == 200
    assert body["data"]["text"] == GOOD.decode("cp932") + ";$L$,LLM add COVERAGE_C1_INCOMPLETE\r\n,5,1\r\n"


def test_missing_cases_on_a_pass_go_to_the_model_until_claimed_or_declined(env, monkeypatch):
    targets = _configure(env)
    env.bridge.captured = _fill_blank
    env.bridge.gaps = [GAP, dict(GAP, obligation_id="b2:T"),
                       dict(GAP, obligation_id="b3:T", gap_class="SEMANTIC_EVIDENCE")]
    calls = []

    def fake_chat(messages, **kwargs):
        calls.append(messages[1]["content"])
        return _llm_reply(_row("b1:T"), declined=["b2:T"])

    monkeypatch.setattr(provider, "chat", fake_chat)
    [job_id] = _enqueue(env, targets["f"])
    job = _job(env, job_id)
    # One round claims b1:T and declines b2:T; the evidence gap is never offered.
    assert len(calls) == 1 and "b3:T" not in calls[0].split("## 现有数据行")[0].split("## 待补缺口")[1]
    assert [(a["origin"], a["verdict"]) for a in job["attempts"]] == [
        ("GENERATOR", "PASS"), ("LLM", "FAIL"), ("CAPTURE", "PASS")]
    assert job["state"] == "awaiting_review" and job["repair_rounds"] == 1
    assert "候选输入已证实 1，模型补行认领 1，模型声明无法补齐 1，未处理 0" in job["reason"]


def test_rejected_witness_stays_out_of_current_candidate_and_winams(env, monkeypatch):
    targets = _configure(env)
    env.bridge.gaps = [GAP]
    env.bridge.captured = _fill_blank
    replies = [_llm_reply(_row("b1:T", "2")), _llm_reply(_row("b1:T", "5"))]
    seen = []

    def chat(messages, **kwargs):
        seen.append(messages[1]["content"])
        return replies.pop(0)

    monkeypatch.setattr(provider, "chat", chat)
    [job_id] = _enqueue(env, targets["f"])
    job = _job(env, job_id)
    assert job["state"] == "awaiting_review" and job["repair_rounds"] == 2
    assert [(a["origin"], a["verdict"]) for a in job["attempts"]] == [
        ("GENERATOR", "PASS"), ("LLM", "FAIL"), ("LLM", "FAIL"), ("CAPTURE", "PASS")]
    assert env.bridge.executions == 3  # generator, valid proposal, capture recheck
    generator, rejected, accepted, capture = job["attempts"]
    assert rejected["has_csv"] and "INPUT_WITNESS_REJECTED" in [
        issue["code"] for issue in rejected["check"]["issues"]]
    assert accepted["reviser"]["parent_attempt_id"] == generator["id"]
    assert capture["reviser"]["parent_attempt_id"] == accepted["id"]
    assert "x must equal 5" in seen[1] and '"observed": false' in seen[1]
    assert '"I1": "2"' in seen[1]
    status, body = env.call("get", f"/jobs/{job_id}/attempts/{capture['id']}/csv",
                            query_string={"format": "text"})
    assert status == 200 and ",2," not in body["data"]["text"] and ",5," in body["data"]["text"]


def test_a_claim_without_current_checker_evidence_goes_to_a_human(env, monkeypatch):
    targets = _configure(env)
    env.bridge.gaps = [GAP]
    original_check = env.bridge.check

    def no_proof(*args, **kwargs):
        result = original_check(*args, **kwargs)
        result.pop("candidate_witnesses", None)
        return result

    env.bridge.check = no_proof
    monkeypatch.setattr(provider, "chat", lambda *a, **k: _llm_reply(_row("b1:T")))
    [job_id] = _enqueue(env, targets["f"])
    job = _job(env, job_id)
    assert job["state"] == "needs_human" and env.bridge.executions == 1
    assert job["current_attempt_id"] == job["attempts"][0]["id"]
    assert job["attempts"][1]["verdict"] == "BLOCKED"


def test_declines_of_a_rejected_proposal_do_not_close_gaps(env, monkeypatch):
    targets = _configure(env)
    env.bridge.gaps = [GAP, dict(GAP, obligation_id="b2:T")]
    env.bridge.captured = _fill_blank
    replies = [_llm_reply(_row("b1:T", "2"), declined=["b2:T"]),
               _llm_reply(_row("b1:T", "5")),
               _llm_reply(declined=["b2:T"])]
    monkeypatch.setattr(provider, "chat", lambda *a, **k: replies.pop(0))
    [job_id] = _enqueue(env, targets["f"])
    job = _job(env, job_id)
    # The rejected round's decline is discarded with its rows, so b2:T is
    # offered again until a round on the accepted lineage declines it.
    assert not replies and job["repair_rounds"] == 3
    assert job["state"] == "awaiting_review"
    assert "模型声明无法补齐 1，未处理 0" in job["reason"]


def test_a_stored_witness_only_counts_for_its_own_csv():
    check = {"completeness": {"unresolved": [GAP]}, "candidate_sha256": "a",
             "candidate_witnesses": {"contract": "ut-agent.candidate-witness/1",
                                     "evidence": {"identified": True}, "candidate_sha256": "a",
                                     "witnessed_obligation_ids": ["b1:T"]}}
    assert pipeline.gap_status(check, [], "a")["open"] == []
    assert pipeline.gap_status(check, [], "b")["open"] == [GAP]


def test_prompt_budget_failure_stops_without_a_model_round(env, monkeypatch):
    targets = _configure(env)
    env.bridge.gaps = [GAP]
    monkeypatch.setattr(pipeline.repair, "_MAX_PROMPT_CHARS", 1)
    monkeypatch.setattr(provider, "chat", lambda *a, **k: pytest.fail("model must not run"))
    [job_id] = _enqueue(env, targets["f"])
    job = _job(env, job_id)
    assert job["state"] == "needs_human" and job["repair_rounds"] == 0
    last = job["attempts"][-1]
    assert last["reviser"]["code"] == "PROMPT_BUDGET_EXCEEDED"
    assert last["reviser"]["model_contacted"] is False and last["verdict"] == "BLOCKED"
    assert env.bridge.executions == 1


def test_wrong_expected_values_are_backfilled_from_winams_without_the_model(env, monkeypatch):
    targets = _configure(env)
    env.bridge.generated = BAD
    env.bridge.captured = GOOD
    monkeypatch.setattr(provider, "chat", lambda *a, **k: pytest.fail("LLM must not run"))
    [job_id] = _enqueue(env, targets["f"])
    job = _job(env, job_id)
    assert job["state"] == "awaiting_review" and job["repair_rounds"] == 0
    assert [(a["origin"], a["verdict"]) for a in job["attempts"]] == [
        ("GENERATOR", "FAIL"), ("CAPTURE", "PASS")]
    # The capture is re-executed before it counts, and its source is recorded.
    assert env.bridge.executions == 2
    capture = job["attempts"][1]
    assert capture["reviser"]["oracle_source"] == "EXECUTION_CAPTURED"
    assert capture["reviser"]["from_attempt"] == 1
    assert capture["reviser"]["resolved_codes"] == ["ORACLE_MISMATCH"]


def test_a_capture_that_still_fails_goes_to_a_human_not_the_model(env, monkeypatch):
    targets = _configure(env)
    env.bridge.generated = BAD
    env.bridge.captured = BAD.replace(b"BAD", b"BAD2")
    monkeypatch.setattr(provider, "chat", lambda *a, **k: pytest.fail("LLM must not run"))
    [job_id] = _enqueue(env, targets["f"])
    job = _job(env, job_id)
    assert [(a["origin"], a["verdict"]) for a in job["attempts"]] == [
        ("GENERATOR", "FAIL"), ("CAPTURE", "FAIL")]
    assert job["state"] == "needs_human" and job["repair_rounds"] == 0
    assert "无法通过新增输入行解决" in job["reason"]


@pytest.mark.parametrize("code", ["COVERAGE_C0_INCOMPLETE", "EXECUTION_PATH_DIVERGED"])
def test_mismatches_beside_input_evidence_are_not_overwritten(env, monkeypatch, code):
    targets = _configure(env, rounds=1)
    env.bridge.generated = BAD
    env.bridge.captured = GOOD
    env.bridge.extra_codes = (code,)
    monkeypatch.setattr(provider, "chat", lambda *a, **k: _llm_reply(_row(code)))
    [job_id] = _enqueue(env, targets["f"])
    job = _job(env, job_id)
    # Rows are added, but the capture never overwrites the mismatch.
    assert [(a["origin"], a["verdict"]) for a in job["attempts"]] == [
        ("GENERATOR", "FAIL"), ("LLM", "FAIL")]
    assert job["state"] == "needs_human"


def test_human_expected_values_are_never_overwritten_by_the_capture(env, monkeypatch):
    targets = _configure(env, rounds=1)
    env.bridge.generated = BAD
    monkeypatch.setattr(provider, "chat", lambda *a, **k: pytest.fail("LLM must not run"))
    [job_id] = _enqueue(env, targets["f"])
    assert _job(env, job_id)["state"] == "needs_human"
    env.bridge.captured = GOOD
    status, _ = env.call("post", f"/jobs/{job_id}/revision", json={"csv": BAD.decode("cp932")})
    assert status == 202
    job = _job(env, job_id)
    assert job["state"] == "needs_human"
    assert [a["origin"] for a in job["attempts"]] == ["GENERATOR", "HUMAN"]


def test_the_generator_intent_manifest_feeds_the_completeness_check(env, tmp_path):
    targets = _configure(env)
    manifest = tmp_path / "test-intents.json"
    manifest.write_text("{}", encoding="utf-8")
    generate = env.bridge.generate

    def generate_with_intents(*args, **kwargs):
        document = generate(*args, **kwargs)
        for unit in document["units"]:
            unit["intent_manifest"] = str(manifest)
        return document

    env.bridge.generate = generate_with_intents
    [job_id] = _enqueue(env, targets["f"])
    assert _job(env, job_id)["state"] == "awaiting_review"
    assert env.bridge.checked_intents == [manifest, manifest]


def test_exhausted_rounds_hand_over_to_a_human(env, monkeypatch):
    targets = _configure(env, rounds=2)
    env.bridge.coverage_rows = 3
    replies = [_llm_reply(_row("COVERAGE_C1_INCOMPLETE")), _llm_reply()]
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
    env.bridge.coverage_rows = 0
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
    env.bridge.coverage_rows = 2
    env.bridge.captured = _fill_blank

    def down(*args, **kwargs):
        raise provider.ProviderError("connection refused")

    monkeypatch.setattr(provider, "chat", down)
    [job_id] = _enqueue(env, targets["f"])
    job = _job(env, job_id)
    assert job["state"] == "error" and job["repair_rounds"] == 0

    monkeypatch.setattr(provider, "chat", lambda *a, **k: _llm_reply(_row("COVERAGE_C1_INCOMPLETE")))
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
    fail = {"verdict": "FAIL", "issues": [
        {"code": "COVERAGE_C1_INCOMPLETE", "severity": "error", "repairable": True}]}
    assert pipeline.next_state(fail, "LLM", 0, 5)[0] == "repairing"
    assert pipeline.next_state(fail, "GENERATOR", 0, 0)[0] == "needs_human"
    assert pipeline.next_state(fail, "HUMAN", 0, 5)[0] == "needs_human"
    # Rows cannot fix expected values or the file format.
    empty = {"verdict": "FAIL", "issues": [
        {"code": "FORMAT_NO_DATA_ROWS", "severity": "error", "repairable": True}]}
    assert pipeline.next_state(empty, "GENERATOR", 0, 5)[0] == "repairing"
    for code in ("ORACLE_MISMATCH", "FORMAT_ENCODING", "FORMAT_INVALID"):
        other = {"verdict": "FAIL", "issues": [{"code": code, "severity": "error", "repairable": True}]}
        assert pipeline.next_state(other, "LLM", 0, 5)[0] == "needs_human"
    passed = {"verdict": "PASS", "issues": []}
    gaps = {"missing": 1, "claimed": 0, "declined": 0, "open": [GAP]}
    assert pipeline.next_state(passed, "GENERATOR", 0, 5, gaps)[0] == "repairing"
    assert pipeline.next_state(passed, "CAPTURE", 5, 5, gaps)[0] == "needs_human"
    assert pipeline.next_state(passed, "HUMAN", 0, 5, gaps)[0] == "awaiting_review"
    assert pipeline.next_state(passed, "GENERATOR", 0, 5)[0] == "awaiting_review"


def test_gap_status_counts_only_explicit_missing_cases():
    check = {"completeness": {"unresolved": [
        GAP, dict(GAP, obligation_id="b2:T"), dict(GAP, obligation_id="b3:T"),
        dict(GAP, obligation_id="b4:T", gap_class="SEMANTIC_EVIDENCE"),
        {"obligation_id": "b5:T"}]}}
    status = pipeline.gap_status(check, [{"claimed": ["b1:T"], "declined": ["b2:T", "b1:T"]}, None])
    assert (status["missing"], status["claimed"], status["declined"]) == (3, 1, 2)
    assert [item["obligation_id"] for item in status["open"]] == ["b3:T"]
    blocked = {"verdict": "BLOCKED", "issues": [
        {"code": "COVERAGE_MCDC_UNAVAILABLE", "severity": "error", "repairable": False}]}
    assert pipeline.next_state(blocked, "LLM", 0, 5)[0] == "needs_human"
    assert pipeline.next_state(blocked, "GENERATOR", 0, 5)[0] == "needs_human"


def test_historical_claims_cannot_close_a_gap_or_survive_csv_replacement():
    check = {"completeness": {"unresolved": [GAP]}}
    history = [{"claimed": ["b1:T"]}]
    assert pipeline.gap_status(check, history)["open"] == [GAP]
    check.update(candidate_sha256="current", candidate_witnesses={
        "contract": "ut-agent.candidate-witness/1", "evidence": {"identified": True},
        "candidate_sha256": "current", "witnessed_obligation_ids": ["b1:T"]})
    assert pipeline.gap_status(check, history)["witnessed"] == 1
    assert pipeline.gap_status(check, history)["open"] == []
    check["candidate_sha256"] = "replacement"
    assert pipeline.gap_status(check, history)["open"] == [GAP]


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
