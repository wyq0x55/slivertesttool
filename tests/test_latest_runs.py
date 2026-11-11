"""Newest run per test id, beyond the recent-task window."""

from test_task_staging_race import _seed, app  # noqa: F401 - shared fixture


def _run(project, test_id, *, deleted=False):
    from app.extensions import db
    from app.models import Task
    import datetime as dt

    task = Task(task_key=f"K{Task.query.count() + 1:06d}", task_name=test_id, file_name="(json runner)",
                submitter="race", test_id=test_id, project_id=project.id, status="passed",
                deleted_at=dt.datetime.now(dt.timezone.utc) if deleted else None)
    db.session.add(task)
    db.session.commit()
    return task


def test_latest_per_test_keeps_old_cases_and_skips_deleted(app):  # noqa: F811
    from app.services import task_service

    _user, project, first = _seed(app)
    old = _run(project, "TC-2")
    for _ in range(5):
        _run(project, "TC-1")
    newest_tc1 = _run(project, "TC-1")
    _run(project, "TC-2", deleted=True)
    _run(project, "")

    latest = {task.test_id: task.id for task in task_service.latest_per_test(project.id)}
    assert latest == {"TC-1": newest_tc1.id, "TC-2": old.id}
    assert first.id not in latest.values()


def test_latest_endpoint_requires_view_permission(app):  # noqa: F811
    _user, project, _ = _seed(app)
    client = app.test_client()
    assert client.get(f"/api/v1/projects/{project.id}/tasks/latest").status_code == 401
    assert client.post("/api/v1/auth/login", json={"username": "race", "password": "secret"}).status_code == 200
    response = client.get(f"/api/v1/projects/{project.id}/tasks/latest")
    assert response.status_code == 200
    assert [task["test_id"] for task in response.get_json()["data"]["tasks"]] == ["TC-1"]
