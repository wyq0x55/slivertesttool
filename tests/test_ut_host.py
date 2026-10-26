"""Host workflow integration on PostgreSQL, with a fake bridge and gateway."""

import hashlib
import json
from pathlib import Path

import pytest
from test_ut_pipeline import FakeBridge, _as, _configure, _enqueue, _job, _member
from test_ut_pipeline import env as _shared_env

from app.services.ai import provider
from app.services.ut_pipeline import host, pipeline
from app.services.ut_pipeline.bridge import BridgeError, BridgeSettings, UtAgentBridge

env = _shared_env

PROPOSAL = {"status": "CANDIDATE", "stub_bodies": {"dep": "return control;"},
            "evidence_ids": ["typed-function-ir-v5", "suite-stub-projection-v1"], "limitations": []}


class HostBridge(FakeBridge):
    def __init__(self, tmp):
        super().__init__(tmp)
        self.host_calls = []
        self.prepare_calls = 0
        self.stub_count = 1
        self.outcomes = ["HOST_PASS"]
        self.reason_codes = []
        self.coverage_report = None

    def generate(self, *args, **kwargs):
        document = super().generate(*args, **kwargs)
        for unit in document["units"]:
            for field, filename in (("ir_json", "ir.json"), ("intent_manifest", "intents.json")):
                path = Path(unit["testcsv"]).with_name(unit["function"] + "-" + filename)
                path.write_text("{}", encoding="utf-8")
                unit[field] = str(path)
        return document

    def host_prepare(self, *, out, **kwargs):
        self.prepare_calls += 1
        out.mkdir(parents=True)
        request = out / "request.json"
        request.write_text('{"trusted":"typed-facts"}', encoding="utf-8")
        return {"status": "READY", "stub_count": self.stub_count, "request_path": str(request),
                "request_sha256": hashlib.sha256(request.read_bytes()).hexdigest(),
                "messages": [{"role": "system", "content": "Only propose stub bodies"},
                             {"role": "user", "content": "Trusted projection"}]}

    def host_execute(self, *, out, **kwargs):
        self.host_calls.append(kwargs)
        out.mkdir(parents=True)
        status = self.outcomes[min(len(self.host_calls) - 1, len(self.outcomes) - 1)]
        bound = kwargs["candidate"].read_bytes() == kwargs["reference"].read_bytes()
        if not bound:
            status = "HOST_UNSUPPORTED"
        verdict = "PASS" if status == "HOST_PASS" else "BLOCKED" if status == "HOST_UNSUPPORTED" else "FAIL"
        execution = {"status": status, "evidence_source": "HOST_EXECUTION", "counts_as_target_evidence": False,
                     "reason": "Bound candidate" if bound else "HOST_CSV_NOT_BOUND: edited rows cannot be replayed",
                     "coverage": {"lines": {"covered": 1, "total": 2, "percent": 50},
                                  "branches": {"covered": 1, "total": 2, "percent": 50},
                                  "mcdc": {"status": "UNSUPPORTED"}}, "build": {"stderr": "compile diagnostic"}}
        if self.coverage_report is not None:
            execution["coverage"].update(status="MEASURED", c1={"covered": 3, "total": 4, "percent": 75.0},
                                         mcdc={"covered": 1, "total": 2, "percent": 50.0})
            execution["coverage_report"] = self.coverage_report
        if self.reason_codes:
            execution["reason_code"] = self.reason_codes[min(len(self.host_calls) - 1, len(self.reason_codes) - 1)]
        check = {"verdict": verdict, "repairable": False, "executed": verdict != "BLOCKED",
                 "completeness": {"unresolved": [], "unresolved_count": 0,
                                  "output_pending": [], "output_pending_count": 0, "details_complete": True},
                 "evidence_source": "HOST_EXECUTION", "counts_as_target_evidence": False,
                 "delivery_validated": False, "issues": [] if verdict == "PASS" else [
                     {"code": "HOST_CSV_NOT_BOUND" if not bound else execution.get("reason_code") or status,
                      "severity": "error",
                      "repairable": False, "message": execution["reason"]}]}
        for name, value in (("execution.json", execution), ("check.json", check)):
            (out / name).write_text(json.dumps(value), encoding="utf-8")
        (out / "harness.c").write_text("/* trusted harness */", encoding="utf-8")
        return {"execution": execution, "check": check}


@pytest.fixture()
def configured(env, monkeypatch, tmp_path):
    bridge = HostBridge(tmp_path)
    env.bridge = bridge
    monkeypatch.setattr(pipeline, "get_bridge", lambda: bridge)
    ids = _configure(env)
    status, body = env.call("put", "/config", json={"stub_mode": "deterministic"})
    assert status == 200, body
    monkeypatch.setattr(provider, "invoke_chat", lambda *a, **k: pytest.fail("Unexpected gateway call"))
    return env, ids


def test_missing_llvm_tool_can_retry_the_same_job_without_spending_model_rounds(configured):
    env, ids = configured
    env.bridge.outcomes = ["HOST_UNSUPPORTED", "HOST_PASS"]
    env.bridge.reason_codes = ["HOST_TOOL_UNAVAILABLE", ""]
    [job_id] = _enqueue(env, ids["f"])
    failed = _job(env, job_id)
    assert failed["state"] == "blocked" and failed["gate_code"] == "ENV_BLOCKED"
    assert "Clang/LLVM" in failed["reason"]
    assert failed["repair_rounds"] == 0 and len(env.bridge.host_calls) == 1

    status, body = env.call("post", f"/jobs/{job_id}/retry")
    assert status == 202, body
    retried = _job(env, job_id)
    assert retried["state"] == "awaiting_review" and retried["gate_code"] == "MACHINE_PASS"
    assert retried["id"] == job_id and retried["batch_id"] == failed["batch_id"]
    assert retried["repair_rounds"] == 0 and len(env.bridge.host_calls) == 2


def test_bridge_host_contract_arguments(monkeypatch, tmp_path):
    bridge = UtAgentBridge(BridgeSettings.from_config({}))
    calls = []

    def invoke(args, *, timeout):
        calls.append((args, timeout))
        return {"prepared": {"status": "READY"}, "execution": {}, "check": {}}

    monkeypatch.setattr(bridge, "_run", invoke)
    kwargs = {"ir_json": tmp_path / "ir", "intents": tmp_path / "intents", "reference": tmp_path / "ref",
              "candidate": tmp_path / "candidate", "out": tmp_path / "out"}
    bridge.host_prepare(**kwargs)
    bridge.host_execute(**kwargs, config={"host_clang": "custom clang", "host_profile_runtime": "runtime.lib"},
                        stub_request=tmp_path / "request", stub_response=tmp_path / "response")
    assert calls[0][0][0] == "host-prepare"
    args, timeout = calls[1]
    assert args[0] == "execute"
    assert args[args.index("--clang") + 1] == "custom clang"
    assert args[args.index("--profile-runtime") + 1] == "runtime.lib"
    assert "--stub-request" in args and "--stub-response" in args
    assert "--winams" not in args and "--manifest" not in args
    assert timeout == 3 * bridge.settings.execute_timeout + 120


@pytest.mark.parametrize("values", [
    {"stub_mode": "other"},
    {"stub_max_rounds": 0}, {"stub_max_rounds": 6}, {"stub_max_rounds": True},
    {"stub_max_rounds": "2"}, {"host_clang": ""}, {"stub_model": 7},
])
def test_invalid_settings_do_not_persist(env, values):
    _configure(env)
    _, before = env.call("get", "/config")
    status, _ = env.call("put", "/config", json=values)
    assert status == 400
    _, after = env.call("get", "/config")
    assert after["data"] == before["data"]


def test_clang_dispatch_no_winams_check_or_capture(configured, monkeypatch):
    env, ids = configured
    monkeypatch.setattr(env.bridge, "check", lambda *a, **k: pytest.fail("static checker called"))
    [job_id] = _enqueue(env, ids["f"])
    job = _job(env, job_id)
    assert job["state"] == "awaiting_review"
    assert job["repair_rounds"] == 0 and env.bridge.executions == 0
    assert env.bridge.prepare_calls == 0
    assert job["attempts"][0]["execution"]["counts_as_target_evidence"] is False
    assert job["attempts"][0]["check"]["delivery_validated"] is False
    status, _ = env.call("post", f"/jobs/{job_id}/review", json={
        "decision": "approve", "expected_attempt_id": job["current_attempt_id"]})
    assert status == 200
    assert _job(env, job_id)["last_verdict"] == "PASS"


def test_model_rounds_save_raw_envelope_diagnostics_and_artifacts(configured, monkeypatch):
    env, ids = configured
    env.call("put", "/config", json={"stub_mode": "llm", "stub_model": "stub-model", "stub_max_rounds": 2})
    env.bridge.outcomes = ["HOST_COMPILE_ERROR", "HOST_PASS"]
    messages = []

    def chat(prompt, **kwargs):
        assert kwargs["model"] == "stub-model" and kwargs["temperature"] == 0
        kwargs["usage"].update(input_tokens=10, output_tokens=5)
        messages.append(prompt)
        return json.dumps(PROPOSAL)

    monkeypatch.setattr(provider, "invoke_chat", chat)
    [job_id] = _enqueue(env, ids["f"])
    job = _job(env, job_id)
    attempt = job["attempts"][0]
    meta = attempt["execution"]["stub_generation"]
    assert job["state"] == "awaiting_review" and env.bridge.prepare_calls == 1
    assert [r["status"] for r in meta["rounds"]] == ["HOST_COMPILE_ERROR", "HOST_PASS"]
    assert "compile diagnostic" in messages[1][-1]["content"]
    assert all(r["usage"]["input_tokens"] == 10 for r in meta["rounds"])
    raw = next(a for a in meta["artifacts"] if a["name"] == "round-1/response.txt")
    url = f"/jobs/{job_id}/attempts/{attempt['id']}/artifacts/{raw['id']}"
    response = env.client.get(env.base + url)
    assert response.status_code == 200 and json.loads(response.data) == PROPOSAL
    envelope = json.loads(env.bridge.host_calls[0]["stub_response"].read_text(encoding="utf-8"))
    assert envelope["proposal"] == PROPOSAL and envelope["model"] == "stub-model"
    assert envelope["request_sha256"] == meta["prepared"]["request_sha256"]
    Path(meta["artifact_root"], raw["name"]).write_text("tampered", encoding="utf-8")
    assert env.client.get(env.base + url).status_code == 400
    assert env.client.get(env.base + url.replace(raw["id"], "unknown")).status_code == 404


def test_llm_without_stubs_skips_gateway(configured):
    env, ids = configured
    env.call("put", "/config", json={"stub_mode": "llm"})
    env.bridge.stub_count = 0
    [job_id] = _enqueue(env, ids["f"])
    assert _job(env, job_id)["state"] == "awaiting_review"
    assert len(env.bridge.host_calls) == 1


def test_model_unavailable_retains_request_and_retry_stays_host(configured, monkeypatch):
    env, ids = configured
    env.call("put", "/config", json={"stub_mode": "llm"})

    def down(*args, **kwargs):
        raise provider.ProviderError("fake gateway offline")

    monkeypatch.setattr(provider, "invoke_chat", down)
    [job_id] = _enqueue(env, ids["f"])
    job = _job(env, job_id)
    assert job["gate_code"] == "MODEL_UNAVAILABLE" and job["state"] == "error"
    meta = job["attempts"][0]["execution"]["stub_generation"]
    assert meta["rounds"][0]["status"] == "MODEL_UNAVAILABLE" and meta["artifacts"]
    monkeypatch.setattr(provider, "invoke_chat", lambda *a, **k: json.dumps(PROPOSAL))
    status, _ = env.call("post", f"/jobs/{job_id}/retry", json={})
    assert status == 202 and _job(env, job_id)["state"] == "awaiting_review"
    assert _job(env, job_id)["current_attempt_id"] != job["current_attempt_id"]
    assert len(_job(env, job_id)["attempts"]) == 2
    status, _ = env.call("post", f"/jobs/{job_id}/review", json={
        "decision": "approve", "expected_attempt_id": job["current_attempt_id"]})
    assert status == 409


def test_preparation_failure_retry_renews_attempt_before_any_execution_evidence(configured, monkeypatch):
    env, ids = configured
    env.call("put", "/config", json={"stub_mode": "llm"})
    prepare = env.bridge.host_prepare

    def unavailable(*args, **kwargs):
        raise BridgeError("fake preparation failure")

    monkeypatch.setattr(env.bridge, "host_prepare", unavailable)
    [job_id] = _enqueue(env, ids["f"])
    before = _job(env, job_id)
    assert before["state"] == "error" and before["gate_code"] == "TOOL_ERROR"
    assert before["attempts"][0]["execution"] is None
    old_id = before["current_attempt_id"]
    monkeypatch.setattr(env.bridge, "host_prepare", prepare)
    monkeypatch.setattr(provider, "invoke_chat", lambda *a, **k: json.dumps(PROPOSAL))
    status, _ = env.call("post", f"/jobs/{job_id}/retry", json={})
    after = _job(env, job_id)
    assert status == 202 and after["state"] == "awaiting_review"
    assert after["current_attempt_id"] != old_id and len(after["attempts"]) == 2
    assert after["attempts"][0]["id"] == old_id and after["attempts"][0]["execution"] is None
    status, _ = env.call("post", f"/jobs/{job_id}/review", json={
        "decision": "approve", "expected_attempt_id": old_id})
    assert status == 409


@pytest.mark.parametrize("status", ["HOST_COMPILE_ERROR", "HOST_RUNTIME_ERROR", "HOST_MISMATCH", "HOST_UNSUPPORTED"])
def test_host_failure_uses_human_gate_without_csv_repair(configured, status):
    env, ids = configured
    env.bridge.outcomes = [status]
    [job_id] = _enqueue(env, ids["f"])
    job = _job(env, job_id)
    assert job["state"] == "needs_human" and job["repair_rounds"] == 0
    assert len(env.bridge.host_calls) == 1


def test_edited_csv_blocks_instead_of_replaying_reference(configured):
    env, ids = configured
    [job_id] = _enqueue(env, ids["f"])
    status, _ = env.call("post", f"/jobs/{job_id}/revision", json={"csv": "unmapped edited candidate"})
    assert status == 202
    job = _job(env, job_id)
    assert job["state"] == "needs_human"
    assert job["attempts"][-1]["check"]["issues"][0]["code"] == "HOST_CSV_NOT_BOUND"


def test_stop_after_reply_retains_candidate_without_host_execution(configured, monkeypatch):
    env, ids = configured
    env.call("put", "/config", json={"stub_mode": "llm"})

    def stop(*args, **kwargs):
        from app.models import UtJob
        job = UtJob.query.filter_by(state="executing").one()
        env.call("post", f"/jobs/{job.id}/stop", json={})
        return json.dumps(PROPOSAL)

    monkeypatch.setattr(provider, "invoke_chat", stop)
    [job_id] = _enqueue(env, ids["f"])
    job = _job(env, job_id)
    assert job["state"] == "stopped" and not env.bridge.host_calls
    assert any(a["name"].endswith("proposal.json") for a in job["attempts"][0]["execution"]["stub_generation"]["artifacts"])


def test_batch_snapshot_freezes_executor_and_stub_fields(configured, app_ctx):
    env, ids = configured
    with app_ctx.app_context():
        from app.models import UtBatch
        batch = pipeline.create_batch(env.pid, [ids["f"]], 1)
        batch_id = batch.id
    env.call("put", "/config", json={"host_clang": "new-clang", "stub_max_rounds": 5, "stub_model": "new-model"})
    with app_ctx.app_context():
        from app.extensions import db
        snapshot = db.session.get(UtBatch, batch_id).config_snapshot
        assert snapshot["execution_backend"] == "clang"
        assert snapshot["stub_mode"] == "deterministic" and snapshot["stub_max_rounds"] == 2
        ready = pipeline.run_generation(batch_id)
        pipeline.advance_job(ready[0])
    assert env.bridge.host_calls[0]["config"]["host_clang"] == "clang"


def test_host_capture_cannot_change_expected_values():
    assert not hasattr(pipeline, "_capture_backfill")


def test_host_missing_case_creates_derived_version_then_reexecutes_before_review(configured, monkeypatch):
    from test_ut_pipeline import GAP, _fill_blank, _llm_reply, _row

    env, ids = configured
    env.bridge.gaps = [GAP]
    env.call("put", "/config", json={"repair_model": "case-model", "stub_model": "different-stub-model"})
    model_calls, host_calls = [], []

    def chat(messages, **kwargs):
        model_calls.append(kwargs["model"])
        return _llm_reply(_row("b1:T"))

    def host_execute(*, out, candidate, reference, **kwargs):
        host_calls.append(candidate)
        out.mkdir(parents=True)
        data = candidate.read_bytes()
        digest = hashlib.sha256(data).hexdigest()
        check = env.bridge.check(candidate, manifest="fixture")
        check.update(verdict="PASS", executed=True, evidence_source="HOST_EXECUTION",
                     counts_as_target_evidence=False, delivery_validated=False)
        check["candidate_witnesses"]["input_mapping_identified"] = True
        check["input_ranges"] = {"contract": {"contract": "ut-agent.input-domains/1", "columns": [
            {"index": 1, "column": "x", "status": "PROVEN", "min": 0, "max": 255}]}}
        execution = {"status": "HOST_PASS", "evidence_source": "HOST_EXECUTION",
                     "counts_as_target_evidence": False, "delivery_validated": False,
                     "sandbox": str(out.resolve()), "input_binding": {"candidate": {"sha256": digest}}}
        if b",5,\r\n" in data:
            typed = out / "typed.csv"
            typed.write_bytes(_fill_blank(data))
            execution["typed_candidate"] = {"contract": "ut-agent.host-typed-candidate/1",
                "path": str(typed.resolve()), "sha256": hashlib.sha256(typed.read_bytes()).hexdigest(),
                "source_candidate_sha256": digest, "reference_rows": 1,
                "expected_value_source": "IMPLEMENTATION_DERIVED"}
        return {"execution": execution, "check": check}

    monkeypatch.setattr(provider, "invoke_chat", chat)
    monkeypatch.setattr(env.bridge, "host_execute", host_execute)
    [job_id] = _enqueue(env, ids["f"])
    job = _job(env, job_id)
    assert job["state"] == "awaiting_review" and job["repair_rounds"] == 1
    assert model_calls == ["case-model"] and len(host_calls) == 3
    assert [a["origin"] for a in job["attempts"]] == ["GENERATOR", "LLM", "HOST_DERIVED"]
    parent, child = job["attempts"][-2:]
    assert child["reviser"]["parent_attempt_id"] == parent["id"]
    assert child["reviser"]["oracle_source"] == "IMPLEMENTATION_DERIVED"
    assert parent["csv_sha256"] != child["csv_sha256"]
    assert child["check"]["candidate_sha256"] == child["csv_sha256"]
    assert child["check"]["counts_as_target_evidence"] is False
    assert child["check"]["delivery_validated"] is False
    assert env.call("post", f"/jobs/{job_id}/review", json={
        "decision": "approve", "expected_attempt_id": parent["id"]})[0] == 409
    assert env.call("post", f"/jobs/{job_id}/review", json={
        "decision": "approve", "expected_attempt_id": child["id"]})[0] == 200


@pytest.mark.parametrize("proposal", [{}, {**PROPOSAL, "extra": True}, {**PROPOSAL, "evidence_ids": []},
                                      {**PROPOSAL, "stub_bodies": "source"}])
def test_proposal_schema_rejects_untrusted_shapes(proposal):
    with pytest.raises(ValueError):
        host.parse_proposal(json.dumps(proposal))


@pytest.mark.parametrize("status", ["UNKNOWN", "UNSUPPORTED", "NEEDS_REVIEW"])
def test_model_abstention_reaches_human_without_retry(configured, monkeypatch, status):
    env, ids = configured
    env.call("put", "/config", json={"stub_mode": "llm"})
    calls = []

    def abstain(*args, **kwargs):
        calls.append(args)
        return json.dumps({"status": status, "stub_bodies": {}, "evidence_ids": [],
                           "limitations": ["Insufficient typed evidence"]})

    monkeypatch.setattr(provider, "invoke_chat", abstain)
    [job_id] = _enqueue(env, ids["f"])
    job = _job(env, job_id)
    assert job["state"] == "needs_human" and len(calls) == 1
    assert not env.bridge.host_calls
    assert job["attempts"][0]["execution"]["stub_generation"]["rounds"][0]["status"] == status


def test_bootstrap_upgrades_existing_config_columns_idempotently(env, app_ctx):
    _configure(env)
    columns = ("execution_backend", "stub_mode", "stub_model", "stub_max_rounds", "host_clang",
               "host_llvm_cov", "host_llvm_profdata", "host_profile_runtime")
    with app_ctx.app_context():
        from sqlalchemy import inspect, text

        from app.bootstrap import _migrate_schema
        from app.extensions import db
        assert db.engine.dialect.name == "postgresql"
        with db.engine.begin() as connection:
            for name in columns:
                connection.execute(text(f"ALTER TABLE ut_pipeline_configs DROP COLUMN {name}"))
        _migrate_schema()
        _migrate_schema()
        assert set(columns) <= {c["name"] for c in inspect(db.engine).get_columns("ut_pipeline_configs")}
        config = pipeline.get_config(env.pid)
        assert config.execution_backend == "clang" and config.stub_mode == "deterministic"
        assert config.stub_max_rounds == 2 and config.host_clang == "clang"


def test_bootstrap_drops_retired_winams_config_columns(env, app_ctx):
    _configure(env)
    with app_ctx.app_context():
        from sqlalchemy import inspect, text

        from app.bootstrap import _migrate_schema
        from app.extensions import db
        with db.engine.begin() as connection:
            connection.execute(text("ALTER TABLE ut_pipeline_configs "
                                    "ADD COLUMN work_root VARCHAR(1024) NOT NULL DEFAULT ''"))
            connection.execute(text("ALTER TABLE ut_pipeline_configs "
                                    "ADD COLUMN stub_builds TEXT NOT NULL DEFAULT ''"))
        _migrate_schema()
        _migrate_schema()
        names = {c["name"] for c in inspect(db.engine).get_columns("ut_pipeline_configs")}
        assert not names & {"work_root", "stub_builds"}
        assert pipeline.get_config(env.pid).execution_backend == "clang"


def test_artifacts_follow_project_view_auth_and_settings_require_edit(configured, app_ctx):
    env, ids = configured
    [job_id] = _enqueue(env, ids["f"])
    attempt = _job(env, job_id)["attempts"][0]
    artifact = attempt["execution"]["stub_generation"]["artifacts"][0]
    url = env.base + f"/jobs/{job_id}/attempts/{attempt['id']}/artifacts/{artifact['id']}"
    reader = _member(app_ctx, env.pid, "reader")
    _as(env, reader)
    assert env.client.get(url).status_code == 200
    assert env.call("put", "/config", json={"stub_max_rounds": 3})[0] == 403
    assert env.call("post", f"/jobs/{job_id}/review", json={"decision": "approve"})[0] == 403
    with env.client.session_transaction() as session:
        session.pop("lm_user_id", None)
    assert env.client.get(url).status_code == 401


def test_artifact_metadata_cannot_escape_job_root(configured, app_ctx):
    env, ids = configured
    [job_id] = _enqueue(env, ids["f"])
    with app_ctx.app_context():
        from app.extensions import db
        from app.models import UtAttempt, UtJob
        attempt = db.session.get(UtAttempt, db.session.get(UtJob, job_id).current_attempt_id)
        execution = json.loads(json.dumps(attempt.execution))
        artifact = execution["stub_generation"]["artifacts"][0]
        artifact["name"] = "../outside.json"
        attempt.execution = execution
        db.session.commit()
        aid, artifact_id = attempt.id, artifact["id"]
    assert env.client.get(env.base + f"/jobs/{job_id}/attempts/{aid}/artifacts/{artifact_id}").status_code == 400


def test_stub_compile_retry_budget_is_independent_of_csv_rounds(configured, monkeypatch):
    env, ids = configured
    env.call("put", "/config", json={"stub_mode": "llm", "stub_max_rounds": 2, "max_repair_rounds": 0})
    env.bridge.outcomes = ["HOST_COMPILE_ERROR"]
    monkeypatch.setattr(provider, "invoke_chat", lambda *a, **k: json.dumps(PROPOSAL))
    [job_id] = _enqueue(env, ids["f"])
    job = _job(env, job_id)
    assert job["state"] == "needs_human" and job["repair_rounds"] == 0
    assert len(env.bridge.host_calls) == 2
    assert len(job["attempts"][0]["execution"]["stub_generation"]["rounds"]) == 2


def test_mismatch_feedback_keeps_cells_and_reference_observation_differences_bounded():
    execution = {"cases": [{"case_id": str(n), "status": "HOST_MISMATCH", "cells": [
        {"key": "return", "expected": 1, "observed": 0, "status": "MISMATCH"}],
        "observations": {"call:dep:count": {"value": 0}}} for n in range(20)],
        "reference_execution": {"cases": [{"case_id": str(n), "observations": {
            "call:dep:count": {"value": 1}}} for n in range(20)]}}
    cases = host._mismatch_cases(execution)
    assert len(cases) == 10
    assert cases[0]["cells"][0]["observed"] == 0
    assert cases[0]["observations"][0]["reference"] == {"value": 1}


@pytest.mark.parametrize("reason_code,expected_calls,expected_state", [
    ("HOST_STUB_BODY_INVALID", 2, "awaiting_review"),
    ("HOST_CSV_NOT_BOUND", 1, "needs_human"),
    ("HOST_STUB_BINDING_CHANGED", 1, "needs_human"),
])
def test_only_invalid_body_unsupported_result_retries(configured, monkeypatch, reason_code,
                                                     expected_calls, expected_state):
    env, ids = configured
    env.call("put", "/config", json={"stub_mode": "llm", "stub_max_rounds": 2})
    env.bridge.outcomes = ["HOST_UNSUPPORTED", "HOST_PASS"]
    env.bridge.reason_codes = [reason_code, ""]
    prompts = []

    def propose(messages, **kwargs):
        prompts.append(messages)
        return json.dumps(PROPOSAL)

    monkeypatch.setattr(provider, "invoke_chat", propose)
    [job_id] = _enqueue(env, ids["f"])
    assert _job(env, job_id)["state"] == expected_state
    assert len(prompts) == len(env.bridge.host_calls) == expected_calls
    if expected_calls == 2:
        assert reason_code in prompts[1][-1]["content"]


def test_invalid_body_retry_stops_at_frozen_budget(configured, monkeypatch):
    env, ids = configured
    env.call("put", "/config", json={"stub_mode": "llm", "stub_max_rounds": 2})
    env.bridge.outcomes = ["HOST_UNSUPPORTED"]
    env.bridge.reason_codes = ["HOST_STUB_BODY_INVALID"]
    monkeypatch.setattr(provider, "invoke_chat", lambda *a, **k: json.dumps(PROPOSAL))
    [job_id] = _enqueue(env, ids["f"])
    job = _job(env, job_id)
    assert job["state"] == "needs_human" and job["last_verdict"] == "BLOCKED"
    assert len(env.bridge.host_calls) == 2


COVERAGE_REPORT = {
    "contract": "ut-agent.coverage-lines/1", "evidence_source": "HOST_EXECUTION",
    "counts_as_target_coverage": False, "function": "f", "cases": 2,
    "summary": {"c0": {"covered": 1, "total": 2, "percent": 50.0}},
    "c1_basis": "DECISION", "mcdc_reported": True,
    "lines": [{"line": 3, "text": "if (x) {", "count": 2, "c0": "COVERED",
               "c1": [{"column": 5, "kind": "DECISION", "true": 1, "false": 0, "state": "PARTIAL"}],
               "mcdc": []}],
    "unplaced": [],
}


def test_host_coverage_report_is_listed_per_job_and_served_per_attempt(configured):
    env, ids = configured
    env.bridge.coverage_report = COVERAGE_REPORT
    [job_id] = _enqueue(env, ids["f"])
    job = _job(env, job_id)
    attempt = job["attempts"][0]
    assert attempt["execution"]["has_coverage_report"] is True
    assert "coverage_report" not in attempt["execution"]

    status, body = env.call("get", "/jobs")
    assert status == 200
    [listed] = [entry for entry in body["data"] if entry["id"] == job_id]
    assert listed["coverage"] == {"evidence_source": "HOST_EXECUTION", "c0": 50,
                                  "c1": 75.0, "mcdc": 50.0, "has_report": True}

    status, body = env.call("get", f"/jobs/{job_id}/attempts/{attempt['id']}/coverage")
    assert status == 200
    assert body["data"]["attempt"] == {"id": attempt["id"], "seq": attempt["seq"], "verdict": "PASS"}
    assert body["data"]["report"] == COVERAGE_REPORT
    status, _ = env.call("get", f"/jobs/{job_id + 999}/attempts/{attempt['id']}/coverage")
    assert status == 404


def test_attempts_without_a_host_report_have_no_coverage_digest(configured):
    env, ids = configured
    [job_id] = _enqueue(env, ids["f"])
    job = _job(env, job_id)
    attempt = job["attempts"][0]
    assert attempt["execution"]["has_coverage_report"] is False
    _, body = env.call("get", "/jobs")
    assert [entry["coverage"] for entry in body["data"] if entry["id"] == job_id] == [None]
    status, _ = env.call("get", f"/jobs/{job_id}/attempts/{attempt['id']}/coverage")
    assert status == 404
