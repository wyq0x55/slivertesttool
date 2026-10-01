from __future__ import annotations

import pytest

from test_ai_api import project_env
from test_ai_context import _row
from test_run_evidence import prepared


@pytest.fixture
def archived(app_ctx, project_env, tmp_path):
    from app.extensions import db
    from app.models import Task, TestItemRow
    from app.services import run_evidence_service as evidence

    item_id = _row(app_ctx, project_env, case_id="TC-1")
    with app_ctx.app_context():
        prototype, source, model = prepared(tmp_path)
        task = Task(**{key: value for key, value in vars(prototype).items() if key != "id"})
        task.project_id = project_env
        db.session.add(task)
        db.session.commit()
        row = db.session.get(TestItemRow, item_id)
        approved = {"row": row.to_dict(), "test": {"input_signals": [["Input", "IN"]],
                    "steps": [{"no": 1, "inputs": ["1"]}]}}
        root = evidence.pin_attempt(task, source, approved_inputs=approved)
        results = root / "results"
        results.mkdir()
        (results / "Console.log").write_text("archived failure IN=1", encoding="utf-8")
        evidence.seal_attempt(task, status="failed", verdict="FAIL", message="Mismatch",
                              evidence_kind="synthetic", runner_backend="mock")
        db.session.commit()
        yield {"task": task, "row": row, "root": root, "approved": approved,
               "source": source, "model": model, "project_id": project_env}


def test_failure_context_uses_sealed_approved_snapshot_not_live_steps(archived):
    from app.extensions import db
    from app.services.ai import context

    row, task = archived["row"], archived["task"]
    row.title = "Changed after the run"
    row.custom_values = {"steps": {"steps": [{"no": 1, "inputs": ["999"]}]}}
    db.session.commit()
    payload = context.build_payload(archived["project_id"], "failure", {
        "item_id": row.id, "task_key": task.task_key, "run_count": 1,
        "log_text": "forged success", "steps_doc": {"steps": []}, "viewpoint": {"title": "forged"}})
    assert payload["viewpoint"]["title"] == "Stored viewpoint"
    assert payload["steps_doc"] == archived["approved"]["test"]
    assert "archived failure" in payload["log_text"]
    assert "forged" not in payload["log_text"]
    assert payload["_context"]["model"]["version"] == "v1"
    provenance = next(entry for entry in payload["_context"]["provenance"] if entry["kind"] == "run_attempt")
    assert provenance["run_count"] == 1 and provenance["task_key"] == task.task_key
    assert provenance["evidence_kind"] == "synthetic"
    assert provenance["artifacts"][0]["sha256"]


@pytest.mark.parametrize("change", ["missing_task", "foreign_project", "different_row", "unsealed", "tampered"])
def test_failure_context_rejects_unusable_or_foreign_archive(archived, change):
    from app.extensions import db
    from app.services.ai import context

    row, task = archived["row"], archived["task"]
    submitted = {"item_id": row.id, "task_key": task.task_key, "run_count": 1}
    if change == "missing_task":
        submitted["task_key"] = "T000999"
    elif change == "foreign_project":
        task.project_id += 100
        db.session.commit()
    elif change == "different_row":
        from app.models import TestItemRow
        other = TestItemRow(project_id=archived["project_id"], sheet="test", case_id="TC-2")
        db.session.add(other)
        db.session.commit()
        submitted["item_id"] = other.id
    elif change == "unsealed":
        (archived["root"] / "outcome.json").unlink()
    else:
        (archived["root"] / "results" / "Console.log").write_text("tampered", encoding="utf-8")
    with pytest.raises(ValueError):
        context.build_payload(archived["project_id"], "failure", submitted)


def test_failure_context_can_analyze_old_attempt_after_retest(archived):
    from app.extensions import db
    from app.services import run_evidence_service as evidence
    from app.services.ai import context

    task, row = archived["task"], archived["row"]
    original = context.build_payload(archived["project_id"], "failure", {
        "item_id": row.id, "task_key": task.task_key, "run_count": 1})
    task.run_count = 2
    task.sil_relpath = str(archived["model"])
    evidence.pin_attempt(task, archived["source"], approved_inputs=archived["approved"])
    db.session.commit()
    second = context.build_payload(archived["project_id"], "failure", {
        "item_id": row.id, "task_key": task.task_key, "run_count": 1})
    assert second["steps_doc"] == original["steps_doc"]
    assert second["log_text"] == original["log_text"]
    with pytest.raises(ValueError, match="sealed"):
        context.build_payload(archived["project_id"], "failure", {"item_id": row.id, "task_key": task.task_key})


@pytest.mark.parametrize("run_count", [True, 0, -1, "1"])
def test_failure_attempt_identity_requires_positive_integer(archived, run_count):
    from app.services.ai import context
    with pytest.raises(ValueError):
        context.build_payload(archived["project_id"], "failure", {
            "item_id": archived["row"].id, "task_key": archived["task"].task_key, "run_count": run_count})
