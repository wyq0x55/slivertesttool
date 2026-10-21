"""Opt-in integration with the pinned external ut-agent and local LLVM tools.

Set UT_HOST_TEST_AGENT_ROOT and UT_HOST_TEST_PYTHON to a compatible checkout.
The model is always fake; only typed extraction and host compilation are real.
"""

import json
import os
import subprocess
from pathlib import Path

import pytest

from app.services.ai import provider
from app.services.ut_pipeline import host
from app.services.ut_pipeline.bridge import BridgeSettings, UtAgentBridge


@pytest.fixture()
def artifacts(tmp_path):
    root, python = os.environ.get("UT_HOST_TEST_AGENT_ROOT"), os.environ.get("UT_HOST_TEST_PYTHON")
    if not root or not python:
        pytest.skip("External ut-agent checkout/interpreter not configured")
    source = tmp_path / "unit.c"
    source.write_text("int read_value(int selector);\n"
                      "int subject(int selector) { int value = read_value(selector); "
                      "if (value > 3) return 1; return 0; }\n", encoding="utf-8")
    ir = tmp_path / "function-ir.json"
    environment = {**os.environ, "PYTHONPATH": str(Path(root) / "src")}
    script = (
        "import json,sys; from pathlib import Path; "
        "from ut_agent.toolchain import ClangExtractor,default_clang_extractor,make_compile_context; "
        "source=Path(sys.argv[1]); "
        "doc=ClangExtractor(default_clang_extractor()).extract_document("
        "make_compile_context([source]),'subject',cwd=source.parent); "
        "Path(sys.argv[2]).write_text(json.dumps(doc),encoding='utf-8')")
    result = subprocess.run([python, "-c", script, str(source), str(ir)], cwd=root, env=environment,
                            capture_output=True, timeout=60, check=False)
    assert result.returncode == 0, result.stderr.decode("utf-8", "replace")
    intents = tmp_path / "intents.json"
    intents.write_text(json.dumps({"intents": [
        {"case_id": "A", "inputs": {"selector": 2, "call:read_value:return:0": 7,
                                    "call:read_value:param:0:0": 2, "call:read_value:count": 0},
         "obligation": {"oid": "A", "kind": "execution"},
         "validation": {"status": "VALIDATED", "checks": [], "errors": []},
         "expected": {"return": 1, "call:read_value:count": 1, "call:read_value:param:0:0": 2}},
        {"case_id": "B", "inputs": {"selector": 9, "call:read_value:return:0": 2,
                                    "call:read_value:param:0:0": 9, "call:read_value:count": 0},
         "obligation": {"oid": "B", "kind": "execution"},
         "validation": {"status": "VALIDATED", "checks": [], "errors": []},
         "expected": {"return": 0, "call:read_value:count": 1, "call:read_value:param:0:0": 9}}],
        "projection_receipt": {"rows": [{"row_index": 0, "case_id": "A"}, {"row_index": 1, "case_id": "B"}]}
    }), encoding="utf-8")
    reference = tmp_path / "reference.csv"
    projection = (
        "import json,sys; from pathlib import Path; "
        "from ut_agent.ir.codec import document_to_function_ir,read_document; "
        "from ut_agent.targets.host.harness import intents_from_records; "
        "from ut_agent.targets.winams.csv import _render_intents_csv; "
        "ir=document_to_function_ir(read_document(Path(sys.argv[1]))); "
        "records=json.loads(Path(sys.argv[2]).read_text(encoding='utf-8'))['intents']; "
        "Path(sys.argv[3]).write_bytes(_render_intents_csv(ir,intents_from_records(records)).encode('cp932'))")
    result = subprocess.run([python, "-c", projection, str(ir), str(intents), str(reference)],
                            cwd=root, env=environment, capture_output=True, timeout=60, check=False)
    assert result.returncode == 0, result.stderr.decode("utf-8", "replace")
    bridge = UtAgentBridge(BridgeSettings.from_config({"UT_AGENT_ROOT": root, "UT_AGENT_PYTHON": python}))
    return {"bridge": bridge, "ir_json": str(ir), "intents": str(intents), "reference": str(reference),
            "candidate": str(reference), "model_timeout": 30, "continuing": lambda: True, "persist": lambda value: None}


def test_real_bridge_deterministic_execution_and_csv_binding(artifacts, tmp_path):
    execution, check = host.execute(**artifacts, out=tmp_path / "valid", config={})
    assert execution["status"] == "HOST_PASS" and check["verdict"] == "PASS", execution
    assert check["counts_as_target_evidence"] is False and check["delivery_validated"] is False
    candidate = tmp_path / "revision.csv"
    rows = Path(artifacts["reference"]).read_bytes().splitlines(keepends=True)
    index = next(i for i, row in enumerate(rows) if row.startswith(b","))
    cells = rows[index].rstrip(b"\r\n").split(b",")
    cells[-1] = b"987654"
    rows[index] = b",".join(cells) + b"\r\n"
    candidate.write_bytes(b"".join(rows))
    artifacts["candidate"] = str(candidate)
    execution, check = host.execute(**artifacts, out=tmp_path / "unbound", config={})
    assert check["verdict"] == "BLOCKED" and execution["reason_code"] == "HOST_CSV_NOT_BOUND"


def test_real_bridge_validates_fake_model_and_repairs_with_observations(artifacts, monkeypatch, tmp_path):
    prompts = []

    def fake_model(messages, **kwargs):
        prompts.append(messages)
        request = json.loads(messages[1]["content"])
        bodies = {name: value["reference_body"] for name, value in request["stubs"].items()}
        if len(prompts) == 1:
            bodies["read_value"] = "return 0;"
        return json.dumps({"status": "CANDIDATE", "stub_bodies": bodies,
                           "evidence_ids": ["typed-function-ir-v5", "suite-stub-projection-v1"], "limitations": []})

    monkeypatch.setattr(provider, "invoke_chat", fake_model)
    execution, check = host.execute(**artifacts, out=tmp_path / "llm",
                                   config={"stub_mode": "llm", "stub_model": "fake-model", "stub_max_rounds": 2})
    assert execution["status"] == "HOST_PASS" and check["verdict"] == "PASS", execution
    assert len(prompts) == 2
    assert '"observed"' in prompts[1][-1]["content"] and '"expected"' in prompts[1][-1]["content"]
    assert len(execution["stub_generation"]["rounds"]) == 2
