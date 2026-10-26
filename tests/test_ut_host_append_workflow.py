"""Opt-in PostgreSQL/API repair loop with real extraction, witnesses and LLVM."""
import json
import os
import subprocess
from pathlib import Path

import pytest
from test_ut_host_workflow import real_workflow as workflow_fixture
from test_ut_pipeline import _job
from test_ut_pipeline import env as workflow_env  # noqa: F401

from app.services.ai import provider

real_workflow = workflow_fixture


def _run_host_append_trial(real_workflow, monkeypatch, *, model_invoke=None, repair_model="fixture-input-repair"):
    env, bridge, calls, config = real_workflow
    manifest = Path(config["manifest_path"])
    settings = json.loads(manifest.read_text(encoding="utf-8"))
    settings["test_policy"]["mcdc"] = False
    settings["test_policy"]["combination_group"] = False
    manifest.write_text(json.dumps(settings), encoding="utf-8")
    assert env.call("put", "/config", json={"repair_model": repair_model})[0] == 200
    generate, execute = bridge.generate, bridge.execute
    observed = {}

    def remove_boundary(*args, **kwargs):
        # Inject the fault before the platform accepts the generated artifact.
        # Early publication itself is covered by the streaming workflow test.
        kwargs.pop("on_unit", None)
        result = generate(*args, **kwargs)
        unit = next(unit for unit in result["units"] if unit["function"] == "classify")
        # Inject a missing-case fault after real formal generation. Keep the
        # typed obligations/SAT results and regenerate the remaining projection.
        script = """
import hashlib,json,sys
from dataclasses import replace
from pathlib import Path
from ut_agent.generation import generate
from ut_agent.ir.codec import document_to_function_ir,read_document
from ut_agent.project import resolve_project_context
from ut_agent.targets.winams.csv import render_suite_csv
manifest,ir_path,suite_path,csv_path=map(Path,sys.argv[1:])
ir=document_to_function_ir(read_document(ir_path))
suite=generate(ir,resolve_project_context(manifest))
removed=[i for i in suite.intents if i.inputs.get('sample')==10]
assert removed
suite=replace(suite,intents=tuple(i for i in suite.intents if i not in removed))
receipt={}
csv_path.write_bytes(render_suite_csv(ir,suite,projection_receipt=receipt).encode('cp932'))
suite_path.write_text(json.dumps({**suite.to_dict(),'projection_receipt':receipt}),encoding='utf-8')
summary_path=suite_path.with_name('test-intents-summary.json')
summary=json.loads(summary_path.read_text(encoding='utf-8'))
summary['artifact_identity']['intent_manifest']=hashlib.sha256(suite_path.read_bytes()).hexdigest()
summary_path.write_text(json.dumps(summary),encoding='utf-8')
"""
        child = subprocess.run([bridge.settings.python, "-B", "-c", script, args[0], unit["ir_json"],
                                unit["intent_manifest"], unit["testcsv"]], cwd=bridge.settings.root,
                               env={**os.environ, "PYTHONPATH": str(Path(bridge.settings.root) / "src")},
                               capture_output=True, timeout=60, check=False)
        assert child.returncode == 0, child.stderr.decode("utf-8", "replace")
        return result

    def record_execution(**kwargs):
        result = execute(**kwargs)
        observed.update(check=result["check"], execution=result["execution"], candidate=kwargs["candidate"])
        return result

    prompts = []

    def propose(messages, **kwargs):
        from app.services.ut_pipeline import increment

        assert kwargs.get("model") == repair_model
        prompts.append(messages)
        if model_invoke is not None:
            return model_invoke(messages, **kwargs)
        table = increment.parse(Path(observed["candidate"]).read_bytes().decode("cp932"))
        used = {row[1] for row in table.rows}
        rows = []
        for gap in observed["check"]["completeness"]["unresolved"]:
            if gap.get("gap_class") != "MISSING_CASE":
                continue
            value = str(gap["boundary_value"]) if gap["kind"] == "boundary" else next(
                str(value) for value in range(11) if str(value) not in used)
            assert value not in used
            used.add(value)
            rows.append({"obligation": gap["obligation_id"], "base_row": 0,
                         "inputs": {"I1": value}, "reason": "Synthetic missing-boundary fixture"})
        return json.dumps({"rows": rows, "declined": [], "rationale": "Typed fixture input repair"})

    monkeypatch.setattr(bridge, "generate", remove_boundary)
    monkeypatch.setattr(bridge, "execute", record_execution)
    monkeypatch.setattr(provider, "invoke_chat", propose)
    status, imported = env.call("post", "/targets/import")
    assert status == 200, imported
    _, targets = env.call("get", "/targets")
    target = next(item for item in targets["data"] if item["function"] == "classify")
    status, batch = env.call("post", "/batches", json={"target_ids": [target["id"]]})
    assert status == 202, batch
    _, jobs = env.call("get", "/jobs", query_string={"batch_id": batch["data"]["id"]})
    job = _job(env, jobs["data"][0]["id"])
    assert job["state"] == "awaiting_review", job
    assert job["repair_rounds"] == len(prompts) == 1
    assert [attempt["origin"] for attempt in job["attempts"]] == ["GENERATOR", "LLM", "HOST_DERIVED"]
    parent, proposed, derived = job["attempts"]
    assert parent["csv_sha256"] != proposed["csv_sha256"] != derived["csv_sha256"]
    assert derived["reviser"]["oracle_source"] == "IMPLEMENTATION_DERIVED"
    assert derived["check"]["candidate_sha256"] == derived["csv_sha256"]
    assert observed["execution"]["input_binding"]["candidate"]["sha256"] == derived["csv_sha256"]
    assert derived["check"]["candidate_witnesses"]["witnessed_obligation_ids"]
    assert derived["execution"]["status"] == "HOST_PASS"
    assert derived["check"]["counts_as_target_evidence"] is False
    assert derived["check"]["delivery_validated"] is False
    assert not any(call[0] in {"winams-execute", "capture"} for call in calls)
    assert job["reviewed_at"] is None
    return {"job": job, "calls": calls, "prompts": prompts}


@pytest.mark.parametrize("real_workflow", ["explicit"], indirect=True)
def test_real_missing_boundary_is_repaired_derived_and_reexecuted(real_workflow, monkeypatch):
    result = _run_host_append_trial(real_workflow, monkeypatch)
    rows = result["job"]["timing"]["steps"]
    requests = [row for row in rows if row["step"] == "input_model"]
    assert len(requests) == 1 and requests[0]["round"] == 1
    executed_versions = {row["candidate_sha256"] for row in rows if row["step"] == "host_execute"}
    assert executed_versions == {attempt["csv_sha256"] for attempt in result["job"]["attempts"]}
