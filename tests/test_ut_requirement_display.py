"""Read-only serialization of the requirements frozen on a UT batch."""
from app.models import UtBatch


def test_batch_serializes_frozen_test_requirements_without_current_settings():
    policy = {"version": "1", "boundary_adjacent": False, "c1": True, "mcdc": False,
              "arithmetic": True, "combination_group": False}
    batch = UtBatch(constraints={"effective_test_policy": policy, "test_policy_sha256": "frozen-hash",
                                 "test_rules_ref": "universal-test-rules@1", "revision_rules": ["bulky"]},
                    config_snapshot={"manifest_path": "old-manifest.json"})
    data = batch.to_dict()
    assert data["constraints"] == {"effective_test_policy": policy, "test_policy_sha256": "frozen-hash",
                                   "test_rules_ref": "universal-test-rules@1"}
    assert "config_snapshot" not in data


def test_batch_without_frozen_requirements_does_not_invent_switches():
    assert UtBatch().to_dict()["constraints"] == {}


def test_batch_frozen_before_baseline_removal_does_not_republish_the_baseline():
    batch = UtBatch(constraints={"baseline": {"id": "locked", "version": "2"},
                                 "coverage_policy": {"mcdc": True}, "test_rules_ref": "locked@2"})
    data = batch.to_dict()["constraints"]
    assert data == {"coverage_policy": {"mcdc": True}, "test_rules_ref": "locked@2"}


def test_config_conflict_is_excluded_from_machine_row_work_even_if_model_claims_it():
    from app.services.ut_pipeline.pipeline import gap_status, next_state

    conflict = {"obligation_id": "M", "gap_class": "MISSING_CASE",
                "requirement": {"configuration_status": "NEEDS_REVIEW"}}
    check = {"verdict": "PASS", "completeness": {"unresolved": [conflict], "unresolved_count": 1}, "issues": []}
    status = gap_status(check, [{"claimed": ["M"]}])
    assert status["missing"] == 0 and status["open"] == []
    state, reason, code = next_state(check, "GENERATOR", 0, 5, status)
    assert state == "needs_human" and code == "CONFIGURATION_CONFLICT"
    assert "配置冲突" in reason
