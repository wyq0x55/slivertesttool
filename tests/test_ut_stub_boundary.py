"""Pure platform orchestration checks; C stub semantics belong to ut-agent."""

import hashlib
import json
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.services.ut_pipeline import binding, host


@pytest.fixture
def boundary(tmp_path, monkeypatch):
    inputs = {}
    for key in ("ir_json", "intents", "reference", "candidate"):
        path = tmp_path / key
        path.write_text("{}", encoding="utf-8")
        inputs[key] = path
    state = SimpleNamespace(calls=[], prepared=0, saved=[], verdict="PASS", stub_count=1)

    def prepare(*, out, **kwargs):
        state.prepared += 1
        out.mkdir(parents=True)
        request = out / "request.json"
        request.write_text("{}", encoding="utf-8")
        return {"status": "READY", "stub_count": state.stub_count,
                "request_path": str(request),
                "request_sha256": hashlib.sha256(request.read_bytes()).hexdigest(),
                "messages": [{"role": "system", "content": "Trusted stub request"}]}

    def execute(**kwargs):
        state.calls.append(kwargs)
        if kwargs["stub_response"] is not None:
            envelope = json.loads(kwargs["stub_response"].read_text(encoding="utf-8"))
            assert envelope["request_sha256"] == hashlib.sha256(
                kwargs["stub_request"].read_bytes()).hexdigest()
            assert envelope["proposal"]["status"] == "CANDIDATE"
        return {"execution": {"status": "HOST_PASS" if state.verdict == "PASS" else "HOST_MISMATCH"},
                "check": {"verdict": state.verdict, "executed": True}}

    def unexpected(*args, **kwargs):
        pytest.fail("Default automated stubs must not contact the model")

    monkeypatch.setattr(host.provider, "invoke_chat", unexpected)
    monkeypatch.setattr(host.ai_config, "get_ai_config", unexpected)
    state.run = lambda config: host.execute(
        bridge=SimpleNamespace(execute=execute, host_prepare=prepare),
        **inputs, out=tmp_path / "execution", config=config, model_timeout=5,
        persist=lambda value: state.saved.append(deepcopy(value)), continuing=lambda: True)
    return state


@pytest.mark.parametrize("config", [{}, {"stub_mode": "deterministic"}])
def test_automatic_stubs_need_neither_interface_entries_nor_a_model(boundary, config):
    execution, check = boundary.run(config)
    assert check["verdict"] == "PASS"
    assert execution["stub_generation"]["mode"] == "deterministic"
    assert execution["stub_generation"]["rounds"] == []
    assert boundary.prepared == 0
    assert len(boundary.calls) == 1
    assert boundary.calls[0]["stub_request"] is None
    assert boundary.calls[0]["stub_response"] is None
    assert boundary.calls[0]["config"] == config
    for record in (execution, check, boundary.saved[-1]):
        assert record["evidence_source"] == "HOST_EXECUTION"
        assert record["counts_as_target_evidence"] is False
        assert record["delivery_validated"] is False


def test_optional_llm_without_dependencies_skips_model(boundary):
    boundary.stub_count = 0
    execution, check = boundary.run({"stub_mode": "llm"})
    assert check["verdict"] == "PASS"
    assert boundary.prepared == 1
    assert len(boundary.calls) == 1
    assert execution["stub_generation"]["rounds"] == []
    assert "model was not contacted" in execution["stub_generation"]["skipped"]


@pytest.mark.parametrize("verdict", ["PASS", "FAIL"])
def test_optional_model_candidate_uses_external_validation_verdict(boundary, monkeypatch, verdict):
    boundary.verdict = verdict
    proposal = {"status": "CANDIDATE", "stub_bodies": {"dep": "return control;"},
                "evidence_ids": ["typed-function-ir-v5", "suite-stub-projection-v1"],
                "limitations": []}
    monkeypatch.setattr(host.provider, "invoke_chat", lambda *args, **kwargs: json.dumps(proposal))
    execution, check = boundary.run({
        "stub_mode": "llm", "stub_model": "test-candidate-model", "stub_max_rounds": 1,
    })
    assert boundary.prepared == 1
    assert len(boundary.calls) == 1
    assert boundary.calls[0]["stub_response"].name == "proposal.json"
    assert check["verdict"] == verdict
    assert check["repairable"] is False
    assert execution["stub_generation"]["rounds"][0]["verdict"] == verdict
    assert execution["counts_as_target_evidence"] is False
    assert execution["delivery_validated"] is False


@pytest.mark.parametrize("interfaces", [
    None,
    {"contract": "advanced@1", "entries": [
        {"function": "dep", "parameter_index": 0, "direction": "inout",
         "pointee_access_order": "read_before_write", "custom": {"retained": True}},
    ], "custom": ["retained"]},
])
def test_manifest_boundary_retains_optional_raw_contracts_for_external_validation(tmp_path, interfaces):
    document = {"project": {"id": "demo"}, "build": {"abi": {"pointer_size": 4}}}
    if interfaces is not None:
        document["interfaces"] = interfaces
    received = []

    def constraints(path):
        received.append(json.loads(Path(path).read_text(encoding="utf-8")))
        return {}

    original = deepcopy(document)
    bridge = SimpleNamespace(constraints=constraints)
    result = binding.prepare_manifest(1, document, bridge=bridge, workspace=tmp_path, persist=True)
    saved = binding.read_manifest(result["path"], bridge=bridge)
    assert document == original
    assert received == [original, original]
    assert saved["manifest_document"] == original
