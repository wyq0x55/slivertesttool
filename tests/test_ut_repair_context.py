"""Source-free fixtures for lossless typed evidence sharing and budget gates."""

import copy
import json

from app.services.ut_pipeline import repair, repair_context
from test_ut_repair_prompt import CHECK, CONSTRAINTS, CSV, GAP


def _unpack(text):
    packed = json.loads(text)
    if isinstance(packed, list):
        return packed
    assert packed["contract"] == repair_context.CONTRACT
    records = []
    for item in packed["records"]:
        record = copy.deepcopy(item["record"])
        if "path" in item:
            record["path"] = []
            for edge in packed["paths"][item["path"]]:
                if "literal" in edge:
                    branch = edge["literal"]
                else:
                    branch = copy.deepcopy(packed["decisions"][edge["decision"]])
                    if "required_outcome" in edge:
                        branch["required_outcome"] = edge["required_outcome"]
                record["path"].append(branch)
        records.append(record)
    return records


def shared_records():
    # Same structural pattern as the audit: four boundaries, one long
    # nine-atom decision, partial direct bindings and unknown stub controls.
    branch = {
        "branch_id": "b0", "required_outcome": None,
        "condition_tree": {"kind": "logical", "op": "&&", "children": [
            {"kind": "atom", "index": 0},
            {"kind": "logical", "op": "||", "children": [
                {"kind": "atom", "index": index} for index in range(1, 9)]}]},
        "atoms": [{
            "variable": f"value_{index}", "operator": "==", "boundary": 1 if index else 0,
            "right_operand": None, "input_tokens": ["I1"] if index == 0 else [],
            "binding": "DIRECT_INPUT" if index == 0 else "NO_DIRECT_INPUT",
            "control_sources": ["global"] if index == 0 else ["stub"],
            "right_input_tokens": [], "right_binding": "NOT_APPLICABLE", "right_control_sources": [],
            "mask": None, "mask_variable": None, "mask_input_tokens": [],
            "mask_binding": "NOT_APPLICABLE", "mask_control_sources": [],
        } for index in range(9)],
        "decision": " && ".join(f"(constant_{i} == value_{i}) /* {'typed context ' * 8} */"
                                for i in range(9)),
        "location": {"file": "src/sample.c", "line": 30, "column": 5},
    }
    return [{"obligation_id": f"b0:boundary:{index}:{label}:{value}", "path": [copy.deepcopy(branch)]}
            for index in (2, 3) for label, value in (("below", 0), ("above", 2))]


def test_four_repeated_typed_paths_fit_budget_and_roundtrip_every_field():
    records = shared_records()
    assert len(json.dumps(records, ensure_ascii=False, sort_keys=True)) > 16000
    packed = repair_context.pack(records)
    assert packed.complete
    assert packed.required_chars <= 16000
    restored = _unpack(packed.text)
    assert restored == sorted(records, key=lambda record: record["obligation_id"])
    envelope = json.loads(packed.text)
    assert len(envelope["decisions"]) == len(envelope["paths"]) == 1
    assert len(envelope["records"]) == 4
    assert repair_context.pack(list(reversed(records))) == packed


def test_outcomes_paths_and_extension_fields_survive_sharing():
    records = shared_records()
    records[0]["path"][0]["required_outcome"] = False
    records[1]["path"][0]["required_outcome"] = True
    del records[2]["path"][0]["required_outcome"]
    records[0]["extension"] = {"record": "unchanged", "path": 999, "path_ref": None}
    records[1]["path"].append({"branch_id": "parent", "required_outcome": False,
                              "new_fact": {"value": 0, "source": "typed"}})
    records[2]["path"].append(None)
    packed = repair_context.pack(records)
    assert packed.complete
    assert _unpack(packed.text) == sorted(records, key=lambda record: record["obligation_id"])


def test_shared_context_reaches_every_requested_obligation(monkeypatch):
    records = shared_records()
    check = dict(CHECK, candidate_witnesses={"repair_context": records})
    gaps = [dict(GAP, obligation_id=record["obligation_id"]) for record in records]
    calls = []
    monkeypatch.setattr(repair.ai_config, "get_ai_config", lambda: {repair.ai_config.KEY_MODEL: "fake"})
    monkeypatch.setattr(repair.provider, "invoke_chat",
                        lambda messages, **kwargs: calls.append(messages) or '{"rows":[]}')
    result = repair.revise(constraints=CONSTRAINTS, target={"function": "f"}, csv_bytes=CSV.encode("cp932"),
                           check=check, execution=None, history=[], open_gaps=gaps,
                           allowed={gap["obligation_id"] for gap in gaps})
    assert len(calls) == 1
    assert result.meta["context_budget"]["complete"] is True
    prompt = calls[0][1]["content"]
    assert "未列出的语义记录" not in prompt
    evidence = prompt.split("No field is inferred or omitted.\n")[1].split("\n\n")[0]
    assert _unpack(evidence) == sorted(records, key=lambda record: record["obligation_id"])


def test_raw_context_prompt_describes_direct_records():
    records = [{"obligation_id": "A", "path": [{"branch_id": "b0", "required_outcome": True}]}]
    packed = repair_context.pack(records)
    assert isinstance(json.loads(packed.text), list)
    prefix = repair.prompt_prefix(CONSTRAINTS, {"function": "f"}, CSV.encode("cp932"),
                                 dict(CHECK, candidate_witnesses={"repair_context": records}))
    assert "If the context below is a JSON array" in prefix["function"]
    assert "use it directly" in prefix["function"]
    assert "If it is an object with contract ut-platform.shared-repair-context/1" in prefix["function"]
    assert _unpack(packed.text) == records


def test_oversize_context_defers_all_tasks_before_model_contact(monkeypatch):
    # One oversized item precedes a smaller item: neither is silently omitted
    # from a request, and neither becomes a model decline or proof.
    records = [{"obligation_id": "A", "path": [{"unique_fact": "x" * 17000}]},
               {"obligation_id": "B", "path": [{"input_tokens": ["I1"]}]}]
    check = dict(CHECK, candidate_witnesses={"repair_context": records})
    monkeypatch.setattr(repair.provider, "invoke_chat",
                        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("must not call")))
    result = repair.revise(constraints=CONSTRAINTS, target={"function": "f"}, csv_bytes=CSV.encode("cp932"),
                           check=check, execution=None, history=[],
                           open_gaps=[dict(GAP, obligation_id="A"), dict(GAP, obligation_id="B")],
                           allowed={"A", "B"})
    assert result.csv_bytes is None
    assert result.meta["code"] == "PROMPT_BUDGET_EXCEEDED"
    assert result.meta["model_contacted"] is False
    assert result.meta["deferred_obligations"] == ["A", "B"]
    assert result.meta["context_budget"]["required_chars"] > 16000
    assert "claimed" not in result.meta and "declined" not in result.meta
