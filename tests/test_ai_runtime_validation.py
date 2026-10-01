from __future__ import annotations

import copy
import json

import pytest

from app.services.ai import provider, scenarios, validators
from test_ai_api import project_env
from test_ai_context import _row


def document(value="BASE+2", timing="100ms以内"):
    return {"input_signals": [["Input", "IN"]], "expected_signals": [["Output", "OUT"]],
            "steps": [{"no": 1, "inputs": [value], "expecteds": ["[BASE,10)"], "timing": timing}]}


def payload():
    return {"viewpoints": [{"ref": "R1", "item_id": 1, "version": 1}],
            "sbs_variables": [["Input", "IN"], ["Output", "OUT"]],
            "runtime_inputs": {"constants": [{"const_name": "BASE", "const_value": "2"}], "libraries": []}}


@pytest.mark.parametrize("value,timing", [
    ("UNKNOWN+1", "100ms以内"), ("1/0", "即時"), ("nan", "即時"),
    ("BASE+2", "-10ms以内"), ("BASE+2", "invented 10ms"), ("BASE+2", "-.5ms以内"),
])
def test_shared_validator_blocks_non_executable_values_and_timing(value, timing):
    problems = validators.validate_output("procedure", payload(),
                                          {"procedures": [{"ref": "R1", "steps_doc": document(value, timing)}]})
    assert problems
    assert any("Runner" in problem or "timing" in problem for problem in problems)


def test_shared_validation_uses_snapshot_constants_not_names_only():
    source = payload()
    output = {"procedures": [{"ref": "R1", "steps_doc": document()}]}
    assert validators.validate_output("procedure", source, output) == []
    source["runtime_inputs"]["constants"] = []
    source["constant_names"] = ["BASE"]
    assert validators.validate_output("procedure", source, output)


def test_shared_validation_uses_actual_library_formal_arguments():
    source = payload()
    source["lib_functions"] = [{"name": "Set"}]
    source["runtime_inputs"]["libraries"] = [{"lib_func": "Set", "lib_para": "value",
                                              "lib_stb": document("value")}]
    doc = document("1")
    doc["steps"] = [{"no": 1, "subroutine": "Set", "args": ["BASE+1", "2"]}]
    output = {"procedures": [{"ref": "R1", "steps_doc": doc}]}
    assert validators.validate_output("procedure", source, output)
    doc["steps"][0]["args"] = ["BASE+1"]
    assert validators.validate_output("procedure", source, output) == []


def test_partial_approval_validates_only_selected_runtime_documents():
    source = payload()
    source["viewpoints"].append({"ref": "R2", "item_id": 2, "version": 1})
    output = {"procedures": [{"ref": "R1", "steps_doc": document()},
                             {"ref": "R2", "steps_doc": document("UNKNOWN")} ]}
    assert validators.validate_output("procedure", source, output, refs=["R1"]) == []
    assert validators.validate_output("procedure", source, output)


def test_procedure_retry_includes_actual_conversion_feedback(monkeypatch):
    source = payload()
    replies = [
        {"plans": [{"ref": "R1", "goal": {"IN": "1"}, "expected": {"OUT": "1"}}]},
        {"procedures": [{"ref": "R1", "steps": [{"no": 1, "inputs": {"IN": "UNKNOWN"},
                                                    "expecteds": {"OUT": "1"}, "timing": "即時"}]}]},
        {"procedures": [{"ref": "R1", "steps": [{"no": 1, "inputs": {"IN": "BASE+2"},
                                                    "expecteds": {"OUT": "1"}, "timing": "即時"}]}]},
    ]
    calls = []

    def chat(messages, **kwargs):
        calls.append(messages)
        return json.dumps(replies.pop(0))

    monkeypatch.setattr(provider, "chat", chat)
    result = scenarios.generate_procedure(source)
    assert len(calls) == 3
    assert "UNKNOWN" in json.dumps(calls[2])
    assert result.output["failed_refs"] == []
    assert result.output["procedures"][0]["steps_doc"]["steps"][0]["inputs"] == ["BASE+2"]


def test_library_retry_uses_shared_validator(monkeypatch):
    source = payload()
    source.update(proposal="Extract setter", procedures=[{"item_id": 1, "steps_doc": document("1")}])
    output = {"lib_name": "Setter", "lib_para": [{"name": "value", "default": 0}],
              "lib_stb": document("UNKNOWN"), "rewritten": []}
    replies = [output, {**output, "lib_stb": document("value")}]
    calls = []

    def chat(messages, **kwargs):
        calls.append(messages)
        return json.dumps(replies.pop(0))

    monkeypatch.setattr(provider, "chat", chat)
    result = scenarios.generate_lib(source)
    assert len(calls) == 2
    assert result.output["lib_stb"]["steps"][0]["inputs"] == ["value"]


def test_context_snapshots_exporter_values_and_overrides_forged_runtime_inputs(app_ctx, project_env):
    from app.extensions import db
    from app.models import TestItemRow
    from app.services.ai import context

    item_id = _row(app_ctx, project_env)
    with app_ctx.app_context():
        constant = TestItemRow(project_id=project_env, sheet="const", case_id="K1",
                               custom_values={"const_name": "BASE", "const_value": "2"})
        library = TestItemRow(project_id=project_env, sheet="lib", case_id="L1",
                              custom_values={"lib_func": "Set", "lib_para": "value=BASE", "lib_stb": document("value")})
        db.session.add_all([constant, library])
        db.session.commit()
        source = context.build_payload(project_env, "procedure", {
            "item_ids": [item_id], "runtime_inputs": {"constants": [{"const_name": "BASE", "const_value": "999"}]}})
        saved = copy.deepcopy(source["runtime_inputs"])
        constant.custom_values = {"const_name": "BASE", "const_value": "9"}
        db.session.commit()
    assert saved["constants"][0]["const_value"] == "2"
    assert saved["libraries"][0]["lib_para"] == "value=BASE"
    assert source["runtime_inputs"] == saved
