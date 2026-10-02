from copy import deepcopy
from importlib import import_module
import json

import pytest

from app.services.pilot_contract import AuditSnapshot, DraftObservation, PilotInput, RunObservation


def measured_pilot():
    return PilotInput.model_validate({
        "schema_version": 1, "project_id": 7, "modules": ["module-a", "module-b"],
        "model": {"model_id": 3, "version": "v1", "sha256": "a" * 64},
        "viewpoints": [{"item_id": item_id, "version": 2,
                        "module": "module-a" if item_id <= 10 else "module-b",
                        "document_revision": "doc-v1", "approval_reference": "operator-review-1"}
                       for item_id in range(1, 21)],
        "draft_ids": [1, 2, 3],
        "run_attempts": [{"task_key": f"T{item_id:06}", "run_count": 1, "item_id": item_id}
                         for item_id in range(1, 21)],
        "timings": {"manual_baseline_minutes": 60, "assisted_total_minutes": 20,
                    "human_intervention_minutes": 5, "human_intervention_count": 2},
    })


def complete_snapshot(pilot):
    chunks = [list(range(1, 9)), list(range(9, 17)), list(range(17, 21))]
    return AuditSnapshot(
        selection_sha256=pilot.selection_digest(), source="postgresql_readonly",
        drafts=[DraftObservation(draft_id=draft_id, status="approved", provider_kind="live",
                                 generated_item_ids=chunk, accepted_item_ids=chunk,
                                 conversion_item_ids=chunk)
                for draft_id, chunk in zip(pilot.draft_ids, chunks)],
        runs=[RunObservation(**attempt.model_dump(), verified=True, finalised=True,
                             evidence_kind="silver_runtime", runner_backend="silver",
                             status="passed", verdict="PASS") for attempt in pilot.run_attempts],
    )


def report(pilot, snapshot=None):
    return import_module("app.services.pilot_metrics").build_report(pilot, snapshot)


def test_complete_measurement_is_not_rollout_approval():
    pilot = measured_pilot()
    result = report(pilot, complete_snapshot(pilot))
    assert result["measurement"]["complete"] is True
    assert result["measurement"]["rollout_approved"] is False
    assert result["measurement"]["requires_owner_review"] is True
    assert result["measurement"]["reported_time_saved_percent"] == pytest.approx(200 / 3)
    assert result["metrics"]["real_completed_viewpoints"] == 20
    assert result["metrics"]["draft_acceptance_rate"] == 1
    json.dumps(result, allow_nan=False)


def test_offline_report_does_not_certify_operator_supplied_times():
    result = report(measured_pilot())
    assert result["source"] == "unverified"
    assert result["metrics"]["generated_candidates"] is None
    assert result["measurement"]["complete"] is False
    assert result["measurement"]["reported_time_saved_percent"] is None


@pytest.mark.parametrize("field", ["manual_baseline_minutes", "assisted_total_minutes",
                                  "human_intervention_minutes", "human_intervention_count"])
def test_each_missing_owner_measurement_remains_missing(field):
    pilot = measured_pilot()
    setattr(pilot.timings, field, None)
    result = report(pilot, complete_snapshot(pilot))
    assert result["operator_timings"][field] is None
    assert field in result["measurement"]["missing"]
    assert result["measurement"]["reported_time_saved_minutes"] is None


def test_measured_zero_interventions_is_complete():
    pilot = measured_pilot()
    pilot.timings.human_intervention_minutes = 0
    pilot.timings.human_intervention_count = 0
    assert report(pilot, complete_snapshot(pilot))["measurement"]["complete"] is True


def test_negative_improvement_is_not_hidden():
    pilot = measured_pilot()
    pilot.timings.assisted_total_minutes = 90
    result = report(pilot, complete_snapshot(pilot))
    assert result["measurement"]["reported_time_saved_minutes"] == -30
    assert result["measurement"]["reported_time_saved_percent"] == -50


def test_extreme_finite_times_never_produce_nonfinite_json():
    pilot = measured_pilot()
    pilot.timings.manual_baseline_minutes = 1e-300
    pilot.timings.assisted_total_minutes = 1e300
    result = report(pilot, complete_snapshot(pilot))
    assert result["measurement"]["complete"] is False
    assert result["measurement"]["reported_time_saved_percent"] is None
    json.dumps(result, allow_nan=False)


@pytest.mark.parametrize("change", ["synthetic", "unclassified", "unverified", "unfinalised", "cancelled", "missing_artifact"])
def test_ineligible_run_never_fills_real_execution_coverage(change):
    pilot = measured_pilot()
    snapshot = complete_snapshot(pilot)
    current = snapshot.runs[0]
    if change == "synthetic":
        current.evidence_kind = "synthetic"
        current.runner_backend = "mock"
    elif change == "unclassified":
        current.evidence_kind = "unclassified"
    elif change == "unverified":
        current.verified = False
    elif change == "unfinalised":
        current.finalised = False
    elif change == "cancelled":
        current.status = "cancelled"
        current.verdict = "CANCELLED"
    else:
        current.issues = ["required_artifact_missing"]
    result = report(pilot, snapshot)
    assert result["metrics"]["real_completed_viewpoints"] == 19
    assert result["measurement"]["complete"] is False


@pytest.mark.parametrize("change", ["unknown_provider", "stub_provider", "conversion_gap", "partial_approval", "source_mismatch"])
def test_ineligible_draft_does_not_certify_accepted_executable_coverage(change):
    pilot = measured_pilot()
    snapshot = complete_snapshot(pilot)
    current = snapshot.drafts[0]
    if change == "unknown_provider":
        current.provider_kind = "unknown"
    elif change == "stub_provider":
        current.provider_kind = "stub"
    elif change == "conversion_gap":
        current.conversion_item_ids = current.conversion_item_ids[1:]
    elif change == "partial_approval":
        current.accepted_item_ids = current.accepted_item_ids[1:]
    else:
        current.issues = ["draft_source_mismatch"]
    assert report(pilot, snapshot)["measurement"]["complete"] is False


def test_rejected_generated_candidates_remain_in_acceptance_denominator():
    pilot = measured_pilot()
    snapshot = complete_snapshot(pilot)
    snapshot.drafts[0].status = "rejected"
    snapshot.drafts[0].accepted_item_ids = []
    result = report(pilot, snapshot)
    assert result["metrics"]["generated_candidates"] == 20
    assert result["metrics"]["accepted_candidates"] == 12
    assert result["metrics"]["draft_acceptance_rate"] == 0.6


def test_zero_generated_has_undefined_rates_not_success():
    pilot = measured_pilot()
    snapshot = complete_snapshot(pilot)
    for current in snapshot.drafts:
        current.generated_item_ids = []
        current.accepted_item_ids = []
        current.conversion_item_ids = []
    result = report(pilot, snapshot)
    assert result["metrics"]["draft_acceptance_rate"] is None
    assert result["metrics"]["executable_conversion_success_rate"] is None
    assert result["measurement"]["complete"] is False


def test_twenty_retests_of_one_row_are_not_twenty_completed_viewpoints():
    pilot = measured_pilot()
    for attempt in pilot.run_attempts:
        attempt.item_id = 1
    snapshot = complete_snapshot(pilot)
    result = report(pilot, snapshot)
    assert result["metrics"]["real_authenticated_attempts"] == 20
    assert result["metrics"]["real_completed_viewpoints"] == 1
    assert result["measurement"]["complete"] is False


def test_trusted_archived_metrics_survive_current_row_readiness_drift():
    pilot = measured_pilot()
    snapshot = complete_snapshot(pilot)
    snapshot.preflight_issues = ["row_version_changed"]
    result = report(pilot, snapshot)
    assert result["readiness"]["ready"] is False
    assert result["measurement"]["complete"] is True


@pytest.mark.parametrize("change", ["digest", "missing_draft", "duplicate_draft", "missing_run", "duplicate_run",
                                   "foreign_row", "accepted_not_generated", "duplicate_candidate", "unapproved_acceptance"])
def test_inconsistent_snapshot_fails_closed(change):
    pilot = measured_pilot()
    snapshot = complete_snapshot(pilot)
    if change == "digest":
        snapshot.selection_sha256 = "b" * 64
    elif change == "missing_draft":
        snapshot.drafts.pop()
    elif change == "duplicate_draft":
        snapshot.drafts.append(deepcopy(snapshot.drafts[0]))
    elif change == "missing_run":
        snapshot.runs.pop()
    elif change == "duplicate_run":
        snapshot.runs.append(deepcopy(snapshot.runs[0]))
    elif change == "foreign_row":
        snapshot.runs[0].item_id = 99
    elif change == "accepted_not_generated":
        snapshot.drafts[0].generated_item_ids = snapshot.drafts[0].generated_item_ids[1:]
    elif change == "duplicate_candidate":
        snapshot.drafts[0].generated_item_ids.append(1)
    else:
        snapshot.drafts[0].status = "pending"
    with pytest.raises(ValueError):
        report(pilot, snapshot)


def test_reducer_does_not_mutate_inputs():
    pilot = measured_pilot()
    snapshot = complete_snapshot(pilot)
    before = (pilot.model_dump(), snapshot.model_dump())
    report(pilot, snapshot)
    assert (pilot.model_dump(), snapshot.model_dump()) == before


def test_real_attempt_without_approved_generation_link_does_not_complete_ai_measurement():
    pilot = measured_pilot()
    snapshot = complete_snapshot(pilot)
    snapshot.runs[0] = RunObservation.model_validate(
        snapshot.runs[0].model_dump(exclude={"approved_draft_id"})
    )
    assert report(pilot, snapshot)["measurement"]["complete"] is False
