"""Retest and drawer submissions recover stale queue state without hiding errors."""

import pytest

from test_task_staging_race import app, _seed


@pytest.fixture()
def submission(app, monkeypatch):
    from app.extensions import db
    from app.models import TaskStatus

    user, project, task = _seed(app)
    saved = app._race_root / "saved.sil"
    saved.write_text("saved model", encoding="utf-8")
    task.sil_relpath = str(saved)
    task.status = TaskStatus.PASSED.value
    db.session.commit()
    enqueued, materialised = [], []
    monkeypatch.setattr("app.services.run_recovery_service.is_run_pending", lambda *_: False)
    monkeypatch.setattr("app.routes.lanmatrix.tasks._enqueue_task",
                        lambda t: enqueued.append((t.id, t.run_count)))
    monkeypatch.setattr("app.services.lanmatrix.silver_json_export.materialise_run_dir",
                        lambda *args: materialised.append(args[-1].sil_relpath))
    client = app.test_client()
    with client.session_transaction() as session:
        session["lm_user_id"] = user.id
        session["csrf_token"] = "test-csrf"

    def post(drawer=False):
        endpoint = "run-selected" if drawer else "rerun-selected"
        payload = ({"test_ids": [task.test_id], "model": "engine@v1"} if drawer
                   else {"task_keys": [task.task_key]})
        response = client.post(f"/api/v1/projects/{project.id}/tasks/{endpoint}",
                               json=payload, headers={"X-CSRF-Token": "test-csrf"})
        assert response.status_code == 201, response.get_data(as_text=True)
        return response.json["data"]

    return task, post, enqueued, materialised


@pytest.mark.parametrize("status", ["passed", "failed", "cancelled", "queued"])
def test_retest_enqueues_selected_row_with_saved_model(submission, status):
    from app.extensions import db

    task, post, enqueued, materialised = submission
    task.status = status
    db.session.commit()
    identity, previous_count, saved = task.id, task.run_count, task.sil_relpath
    data = post()
    assert data["created"] == [{"test_id": task.test_id, "task_id": task.task_key,
                                "run_count": previous_count + 1}]
    assert not data["skipped"] and not data["errors"]
    assert enqueued == [(identity, previous_count + 1)]
    assert materialised == [saved]


@pytest.mark.parametrize("status", ["running", "queued"])
def test_live_attempt_is_skipped_with_reason(submission, monkeypatch, status):
    from app.extensions import db

    task, post, enqueued, materialised = submission
    task.status = status
    db.session.commit()
    monkeypatch.setattr("app.services.run_recovery_service.is_run_pending", lambda *_: True)
    data = post()
    assert not data["created"] and not data["errors"]
    assert data["skipped"][0]["error"] == "任务仍在队列或执行中，无需重新测试。"
    assert not enqueued and not materialised


@pytest.mark.parametrize("other_status", ["queued", "running"])
def test_retest_protects_other_row_and_its_staging(app, submission, other_status):
    from app.extensions import db
    from app.runners import run_layout
    from app.services import task_service
    from app.services.run_recovery_service import is_run_pending

    task, post, enqueued, materialised = submission
    other = task_service.create_task(task_name=task.test_id, test_id=task.test_id,
                                     file_name="runner", submitter=task.submitter,
                                     sil_relpath=task.sil_relpath, workspace=task.workspace,
                                     project_id=task.project_id)
    other.status = other_status
    db.session.commit()
    other_count, selected_count = other.run_count, task.run_count
    assert not is_run_pending(other.id, other_count)
    project_root = app._race_root / "ws" / "RACE1"
    staging = run_layout.staging_dir(project_root, task.test_id) / task.test_id
    staging.mkdir(parents=True)
    marker = staging / "marker.txt"
    marker.write_text("B owns these scripts", encoding="utf-8")
    with pytest.raises(task_service.TaskStillActive):
        task_service.requeue_task(task, sil_relpath=task.sil_relpath,
                                  workspace=str(project_root))
    db.session.rollback()
    data = post()
    db.session.refresh(other)
    assert other.status == other_status and other.run_count == other_count
    assert data["skipped"][0]["error"] == "任务仍在队列或执行中，无需重新测试。"
    assert not data["created"] and not data["errors"]
    assert not enqueued and not materialised
    assert marker.read_text(encoding="utf-8") == "B owns these scripts"
    db.session.refresh(task)
    assert task.status == "passed" and task.run_count == selected_count
    if other_status == "queued":
        task_service.requeue_task(other, sil_relpath=other.sil_relpath,
                                  workspace=str(project_root))
        assert other.run_count == other_count + 1


@pytest.mark.parametrize("other_status", ["queued", "running"])
def test_recover_stale_upsert_protects_older_staging_owner(app, submission, other_status):
    from app.extensions import db
    from app.runners import run_layout
    from app.services import task_service

    owner, post, enqueued, materialised = submission
    owner.status = other_status
    db.session.commit()
    newest = task_service.create_task(task_name=owner.test_id, test_id=owner.test_id,
                                      file_name="runner", submitter=owner.submitter,
                                      sil_relpath=owner.sil_relpath, workspace=owner.workspace,
                                      project_id=owner.project_id)
    newest.status = "passed"
    db.session.commit()
    old_count, newest_count = owner.run_count, newest.run_count
    project_root = app._race_root / "ws" / "RACE1"
    staging = run_layout.staging_dir(project_root, owner.test_id) / owner.test_id
    staging.mkdir(parents=True)
    marker = staging / "marker.txt"
    marker.write_text("owned scripts", encoding="utf-8")
    found, started = task_service.upsert_task(
        task_name=owner.test_id, test_id=owner.test_id, file_name="runner",
        submitter=owner.submitter, sil_relpath=owner.sil_relpath,
        workspace=str(project_root), project_id=owner.project_id, recover_stale=True)
    assert found.id == owner.id and not started
    data = post(drawer=True)
    assert data["duplicates"] == [{"test_id": owner.test_id, "task_id": owner.task_key}]
    assert not data["created"] and not data["errors"]
    assert not enqueued and not materialised
    assert marker.read_text(encoding="utf-8") == "owned scripts"
    db.session.refresh(newest)
    db.session.refresh(owner)
    assert newest.status == "passed" and newest.run_count == newest_count
    assert owner.status == other_status and owner.run_count == old_count


@pytest.mark.parametrize("phase", ["materialise", "path", "enqueue"])
def test_value_error_is_failure_not_skip(submission, monkeypatch, phase):
    task, post, _enqueued, _materialised = submission

    def fail(*_args, **_kwargs):
        raise ValueError(f"invalid {phase}")

    target = {
        "materialise": "app.services.lanmatrix.silver_json_export.materialise_run_dir",
        "path": "app.routes.lanmatrix.tasks.run_evidence_service.checked_path",
        "enqueue": "app.routes.lanmatrix.tasks._enqueue_task",
    }[phase]
    monkeypatch.setattr(target, fail)
    data = post()
    assert not data["created"] and not data["skipped"]
    assert data["errors"] == [{"task_id": task.task_key, "test_id": task.test_id,
                               "error": f"invalid {phase}"}]


@pytest.mark.parametrize("pending", [False, True])
def test_drawer_recovers_only_stale_queued_attempt(submission, monkeypatch, pending):
    from app.extensions import db

    task, post, enqueued, materialised = submission
    task.status = "queued"
    db.session.commit()
    old_count = task.run_count
    monkeypatch.setattr("app.services.run_recovery_service.is_run_pending", lambda *_: pending)
    data = post(drawer=True)
    assert not data["errors"]
    if pending:
        assert data["duplicates"] and not data["created"]
        assert not enqueued and not materialised
    else:
        assert data["created"] and not data["duplicates"]
        assert enqueued == [(task.id, old_count + 1)]


@pytest.mark.parametrize("location", ["pending", "scheduled"])
def test_queue_liveness_matches_attempt_identity(app, monkeypatch, location):
    from app.jobqueue import huey
    from app.jobqueue.tasks import run_task
    from app.services.run_recovery_service import is_run_pending

    monkeypatch.setattr(huey, "pending", lambda: [])
    monkeypatch.setattr(huey, "scheduled", lambda: [])
    monkeypatch.setattr(huey, location, lambda: [run_task.s(42, 3)])
    assert is_run_pending(42, 3)
    assert not is_run_pending(42, 2)
    assert not is_run_pending(41, 3)
