from copy import deepcopy
from importlib import import_module

import pytest
from pydantic import ValidationError


def sample_input():
    return {
        "schema_version": 1,
        "project_id": 7,
        "modules": ["module-a", "module-b"],
        "model": {"model_id": 3, "version": "v1", "sha256": "a" * 64},
        "viewpoints": [
            {
                "item_id": item_id,
                "version": 2,
                "module": "module-a" if item_id <= 10 else "module-b",
                "document_revision": "doc-v1",
                "approval_reference": "operator-review-1",
            }
            for item_id in range(1, 21)
        ],
        "draft_ids": [],
        "run_attempts": [],
        "timings": {},
    }


def contract():
    return import_module("app.services.pilot_contract")


def test_valid_selection_retains_missing_measurements():
    pilot = contract().PilotInput.model_validate(sample_input())
    assert pilot.timings.manual_baseline_minutes is None
    assert len(pilot.viewpoints) == 20


@pytest.mark.parametrize("field,value", [
    ("project_id", True), ("project_id", "7"), ("project_id", 0),
    ("schema_version", True), ("schema_version", 2),
    ("modules", ["module-a", "module-a"]),
    ("modules", ["module-a"]), ("modules", ["module-a", " "]),
])
def test_rejects_invalid_selection_identity(field, value):
    payload = sample_input()
    payload[field] = value
    with pytest.raises(ValidationError):
        contract().PilotInput.model_validate(payload)


@pytest.mark.parametrize("change", ["short", "duplicate", "unused_module", "foreign_module", "missing_approval"])
def test_selection_is_twenty_unique_items_from_both_modules(change):
    payload = sample_input()
    if change == "short":
        payload["viewpoints"].pop()
    elif change == "duplicate":
        payload["viewpoints"][19] = deepcopy(payload["viewpoints"][0])
    elif change == "unused_module":
        for item in payload["viewpoints"]:
            item["module"] = "module-a"
    elif change == "foreign_module":
        payload["viewpoints"][0]["module"] = "foreign"
    else:
        payload["viewpoints"][0]["approval_reference"] = ""
    with pytest.raises(ValidationError):
        contract().PilotInput.model_validate(payload)


@pytest.mark.parametrize("value", [True, "10", float("nan"), float("inf"), -1, 0])
def test_baseline_must_be_positive_finite_measured_minutes(value):
    payload = sample_input()
    payload["timings"]["manual_baseline_minutes"] = value
    with pytest.raises(ValidationError):
        contract().PilotInput.model_validate(payload)


def test_zero_intervention_is_a_measurement_not_missing_data():
    payload = sample_input()
    payload["timings"] = {"human_intervention_count": 0, "human_intervention_minutes": 0}
    pilot = contract().PilotInput.model_validate(payload)
    assert pilot.timings.human_intervention_count == 0
    assert pilot.timings.human_intervention_minutes == 0


@pytest.mark.parametrize("change", ["duplicate_draft", "duplicate_attempt", "foreign_item", "boolean_attempt", "extra"])
def test_rejects_ambiguous_or_injected_evidence_references(change):
    payload = sample_input()
    if change == "duplicate_draft":
        payload["draft_ids"] = [1, 1]
    elif change == "duplicate_attempt":
        payload["run_attempts"] = [{"task_key": "T000001", "run_count": 1, "item_id": 1}] * 2
    elif change == "foreign_item":
        payload["run_attempts"] = [{"task_key": "T000001", "run_count": 1, "item_id": 99}]
    elif change == "boolean_attempt":
        payload["run_attempts"] = [{"task_key": "T000001", "run_count": True, "item_id": 1}]
    else:
        payload["trusted_snapshot"] = {"source": "postgresql_readonly"}
    with pytest.raises(ValidationError):
        contract().PilotInput.model_validate(payload)


def test_selection_digest_binds_evidence_but_not_operator_timing():
    payload = sample_input()
    first = contract().PilotInput.model_validate(payload)
    payload["timings"] = {"manual_baseline_minutes": 30}
    second = contract().PilotInput.model_validate(payload)
    assert first.selection_digest() == second.selection_digest()
    payload["draft_ids"] = [4]
    third = contract().PilotInput.model_validate(payload)
    assert first.selection_digest() != third.selection_digest()


def test_json_schema_is_derived_from_the_authoritative_typed_contract():
    schema = contract().PilotInput.model_json_schema()
    assert schema["additionalProperties"] is False
    assert schema["properties"]["viewpoints"]["minItems"] == 20
    assert schema["properties"]["viewpoints"]["maxItems"] == 20
