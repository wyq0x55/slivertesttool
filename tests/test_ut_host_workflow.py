"""Opt-in PostgreSQL/API/Huey workflow with real ut-agent and LLVM tools.

Repository-owned C only. The model transport is faked, while index extraction,
formal generation, typed stub preparation, compilation and llvm-cov are real.
"""

import csv
import hashlib
import io
import json
import os
import shutil
import zipfile
from pathlib import Path

import pytest
from test_ut_host_subprocess import _scalar_stub_bodies
from test_ut_pipeline import _job
from test_ut_pipeline import env as workflow_env  # noqa: F401

from app.services.ai import provider
from app.services.ut_pipeline import pipeline
from app.services.ut_pipeline.bridge import BridgeSettings, UtAgentBridge


@pytest.fixture()
def real_workflow(request, app_ctx, monkeypatch, tmp_path):
    env = request.getfixturevalue("workflow_env")
    root = os.environ.get("UT_HOST_TEST_AGENT_ROOT")
    python = os.environ.get("UT_HOST_TEST_PYTHON")
    extractor = os.environ.get("UT_CLANG_EXTRACT")
    if not root or not python or not extractor:
        pytest.skip("Real ut-agent checkout, interpreter and extractor are required")
    root = Path(root)
    product = tmp_path / "Product"
    sources = product / "src"
    sources.mkdir(parents=True)
    (sources / "classify.c").write_text(
        "int classify(int sample) { if (sample > 10) { return 1; } return 0; }\n", encoding="utf-8")
    (sources / "relay.c").write_text(
        "int external_sample(int channel);\n"
        "int relay(int channel) { return external_sample(channel); }\n", encoding="utf-8")
    (sources / "notify.c").write_text(
        "void external_notice(int code);\n"
        "void notify(int code) { external_notice(code); }\n", encoding="utf-8")
    index = tmp_path / "functions.csv"
    index.write_bytes((
        "0,classify.c,classify,Product/src/classify.c,WinAMS/src/classify\r\n"
        "1,relay.c,relay,Product/src/relay.c,WinAMS/src/relay\r\n"
        "1,notify.c,notify,Product/src/notify.c,WinAMS/src/notify\r\n").encode("cp932"))
    manifest = tmp_path / "manifest.json"
    manifest_data = json.loads((root / "config/projects/N-O2606-PSD-049.json").read_text(encoding="utf-8"))
    manifest_data["project"]["id"] = "llvm-workflow-fixture"
    manifest_data["build"]["exclude_directories"] = []
    manifest_data["interfaces"]["contract"] = None
    if "test_policy" not in manifest_data:
        manifest_data["test_policy"] = {
            "version": "1", "boundary_adjacent": True, "c1": True,
            "mcdc": True, "arithmetic": False, "combination_group": True,
        }
    manifest.write_text(json.dumps(manifest_data), encoding="utf-8")
    settings = BridgeSettings(str(root), python, extractor, 120, 180, 120)
    bridge = UtAgentBridge(settings)
    calls = []
    invoke = bridge._run

    def real_call(arguments, *, timeout, **kwargs):
        calls.append(list(arguments))
        return invoke(arguments, timeout=timeout, **kwargs)

    monkeypatch.setattr(bridge, "_run", real_call)
    monkeypatch.setattr(pipeline, "get_bridge", lambda: bridge)
    monkeypatch.setattr(provider, "invoke_chat", lambda *a, **k: pytest.fail("Actual model must not be contacted"))
    config = {"execution_backend": "clang", "stub_mode": "deterministic",
              "manifest_path": str(manifest),
              "index_csv": str(index), "product_root": str(product),
              "stub_max_rounds": 2, "stub_model": "workflow-fake-model"}
    status, body = env.call("put", "/config", json=config)
    assert status == 200, body
    return env, bridge, calls, config


@pytest.mark.parametrize("stub_mode", ["deterministic", "llm"])
def test_real_index_generation_execution_review_and_export(real_workflow, monkeypatch, stub_mode, app_ctx):
    env, _bridge, calls, _config = real_workflow
    prompts = []
    if stub_mode == "llm":
        def local_model(messages, **kwargs):
            prompts.append(messages)
            request = json.loads(messages[1]["content"])
            return json.dumps({"status": "CANDIDATE", "stub_bodies": _scalar_stub_bodies(request),
                "evidence_ids": ["typed-function-ir-v5", "suite-stub-projection-v1"], "limitations": []})

        monkeypatch.setattr(provider, "invoke_chat", local_model)
        assert env.call("put", "/config", json={"stub_mode": "llm"})[0] == 200
    status, body = env.call("post", "/targets/import")
    assert status == 200 and body["data"]["total"] == 3, body
    _, targets = env.call("get", "/targets")
    ids = [target["id"] for target in targets["data"]]
    status, batch = env.call("post", "/batches", json={"target_ids": ids, "label": "real LLVM workflow"})
    assert status == 202, batch
    batch_id = batch["data"]["id"]
    with app_ctx.app_context():
        from app.extensions import db
        from app.models import UtBatch

        frozen = db.session.get(UtBatch, batch_id)
        document = json.loads(Path(frozen.config_snapshot["manifest_path"]).read_text(encoding="utf-8"))
        if "test_policy" in document:
            constraints = frozen.constraints
            assert constraints["test_policy"] == document["test_policy"]
            assert len(constraints["test_policy_sha256"]) == 64
            assert constraints["test_rules_ref"] == "universal-test-rules@1"
    _, jobs = env.call("get", "/jobs", query_string={"batch_id": batch_id})
    assert len(jobs["data"]) == 3, jobs
    for entry in jobs["data"]:
        job = _job(env, entry["id"])
        assert job["state"] == "awaiting_review", {"reason": job.get("reason"),
                                                    "gate_code": job.get("gate_code"),
                                                    "generation": job.get("generation")}
        assert job["generation"]["ir_json"] and job["generation"]["intent_manifest"]
        attempt = job["attempts"][-1]
        execution = attempt["execution"]
        assert execution["status"] == "HOST_PASS", job
        assert execution["coverage"]["status"] == "MEASURED"
        assert execution["evidence_source"] == "HOST_EXECUTION"
        assert execution["counts_as_target_evidence"] is False
        assert attempt["check"]["delivery_validated"] is False
        artifact = next(a for a in execution["stub_generation"]["artifacts"] if a["name"].endswith("execution.json"))
        download = env.client.get(env.base + f"/jobs/{job['id']}/attempts/{attempt['id']}/artifacts/{artifact['id']}")
        assert download.status_code == 200
        assert hashlib.sha256(download.data).hexdigest() == artifact["sha256"]
        status, reviewed = env.call("post", f"/jobs/{job['id']}/review", json={
            "decision": "approve", "expected_attempt_id": job["current_attempt_id"],
            "note": "Synthetic host workflow review; not target acceptance"})
        assert status == 200, reviewed
    exported = env.client.get(env.base + f"/approved/export?batch_id={batch_id}")
    assert exported.status_code == 200
    with zipfile.ZipFile(io.BytesIO(exported.data)) as archive:
        manifest = json.loads(archive.read("manifest.json"))
        assert {item["function"] for item in manifest["files"]} == {"classify", "relay", "notify"}
        for item in manifest["files"]:
            assert item["evidence_source"] == "HOST_EXECUTION"
            assert item["counts_as_target_evidence"] is False and item["delivery_validated"] is False
            assert hashlib.sha256(archive.read(item["file"])).hexdigest() == item["file_sha256"]
            # No repair rows were appended, so nothing is presented differently.
            assert item["file_sha256"] == item["csv_sha256"]
        table = list(csv.DictReader(io.StringIO(archive.read("manifest.csv").decode("utf-8-sig"))))
        assert len(table) == 3
        assert all(row["execution_backend"] == "clang" and row["evidence_source"] == "HOST_EXECUTION"
                   and row["counts_as_target_evidence"] == "False" and row["delivery_validated"] == "False"
                   for row in table)
    commands = [arguments[0] for arguments in calls]
    assert {"index", "constraints", "generate", "execute"} <= set(commands)
    assert "winams-execute" not in commands and "check" not in commands
    if stub_mode == "llm":
        assert len(prompts) == 2  # classify has no dependency stubs.
        assert commands.count("host-prepare") == 3
    else:
        assert not prompts and "host-prepare" not in commands


def test_missing_llvm_tool_is_reported_and_fixed_settings_apply_to_a_new_batch(real_workflow, tmp_path):
    env, _bridge, _calls, _config = real_workflow
    missing = str(tmp_path / "missing-llvm-cov.exe")
    assert env.call("put", "/config", json={"host_llvm_cov": missing})[0] == 200
    assert env.call("post", "/targets/import")[0] == 200
    _, targets = env.call("get", "/targets")
    target = next(t for t in targets["data"] if t["function"] == "classify")
    status, first = env.call("post", "/batches", json={"target_ids": [target["id"]]})
    assert status == 202, first
    _, jobs = env.call("get", "/jobs", query_string={"batch_id": first["data"]["id"]})
    failed_id = jobs["data"][0]["id"]
    failed = _job(env, failed_id)
    assert failed["state"] == "blocked" and failed["gate_code"] == "ENV_BLOCKED", failed.get("reason")
    assert failed["attempts"][-1]["check"]["issues"][0]["code"] == "HOST_TOOL_UNAVAILABLE"
    assert missing in failed["reason"]
    assert env.call("put", "/config", json={"host_llvm_cov": "llvm-cov"})[0] == 200
    status, second = env.call("post", "/batches", json={"target_ids": [target["id"]]})
    assert status == 202, second
    _, jobs = env.call("get", "/jobs", query_string={"batch_id": second["data"]["id"]})
    successful = _job(env, jobs["data"][0]["id"])
    assert successful["state"] == "awaiting_review", successful.get("reason")
    assert _job(env, failed_id)["attempts"][-1]["check"]["issues"][0]["code"] == "HOST_TOOL_UNAVAILABLE"


def test_missing_llvm_tool_retries_after_restoration_at_the_frozen_path(real_workflow, tmp_path):
    env, _bridge, _calls, _config = real_workflow
    installed = shutil.which("llvm-cov")
    assert installed
    restored = tmp_path / Path(installed).name
    assert env.call("put", "/config", json={"host_llvm_cov": str(restored)})[0] == 200
    assert env.call("post", "/targets/import")[0] == 200
    _, targets = env.call("get", "/targets")
    target = next(t for t in targets["data"] if t["function"] == "classify")
    status, batch = env.call("post", "/batches", json={"target_ids": [target["id"]]})
    assert status == 202, batch
    _, jobs = env.call("get", "/jobs", query_string={"batch_id": batch["data"]["id"]})
    job_id = jobs["data"][0]["id"]
    failed = _job(env, job_id)
    assert failed["state"] == "blocked" and failed["gate_code"] == "ENV_BLOCKED"
    assert str(restored) in failed["reason"] and failed["repair_rounds"] == 0

    shutil.copy2(installed, restored)
    status, body = env.call("post", f"/jobs/{job_id}/retry")
    assert status == 202, body
    retried = _job(env, job_id)
    assert retried["state"] == "awaiting_review", retried.get("reason")
    assert retried["batch_id"] == batch["data"]["id"] and retried["repair_rounds"] == 0
    assert retried["attempts"][-1]["execution"]["status"] == "HOST_PASS"
    assert retried["attempts"][-1]["execution"]["evidence_source"] == "HOST_EXECUTION"
    assert retried["attempts"][-1]["check"]["counts_as_target_evidence"] is False


def test_real_generation_failure_keeps_extractor_diagnostic_in_the_job(real_workflow):
    env, _bridge, calls, config = real_workflow
    manifest = Path(config["manifest_path"])
    value = json.loads(manifest.read_text(encoding="utf-8"))
    value["build"]["exclude_directories"] = ["does-not-exist"]
    manifest.write_text(json.dumps(value), encoding="utf-8")
    assert env.call("post", "/targets/import")[0] == 200
    _, targets = env.call("get", "/targets")
    status, batch = env.call("post", "/batches", json={"target_ids": [targets["data"][0]["id"]]})
    assert status == 202, batch
    _, jobs = env.call("get", "/jobs", query_string={"batch_id": batch["data"]["id"]})
    failed = _job(env, jobs["data"][0]["id"])
    assert failed["state"] == "error", failed
    assert "does-not-exist" in failed["reason"] and "generation report" in failed["reason"]
    assert "生成结果中缺少该函数" not in failed["reason"]
    assert not failed["attempts"]
    assert not any(arguments[0] == "execute" for arguments in calls)
