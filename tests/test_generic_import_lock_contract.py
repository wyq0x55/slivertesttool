from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import Mock

import pytest


def test_generic_replace_locks_before_snapshot_and_rolls_back_stale_preview(monkeypatch):
    from app.services.lanmatrix import excel_service, service

    events = []
    snapshot = {"rows": [], "fields": [], "project": {}}
    job = SimpleNamespace(
        id=1, project_id=2, job_type="import", status="previewed",
        parameters={"mode": "replace_all", "_snapshot": snapshot},
        preview={"invalid": 0, "rows": []},
        expires_at=datetime.now(timezone.utc) + timedelta(days=1),
    )
    project = SimpleNamespace(id=2, is_editable=True)
    user = SimpleNamespace(id=3, is_system_admin=True)
    session = Mock()
    session.rollback.side_effect = lambda: events.append("rollback")
    monkeypatch.setattr(excel_service, "db", SimpleNamespace(session=session))
    for name, row in (("DataJob", job), ("Project", project)):
        query = Mock()
        query.filter_by.return_value.with_for_update.return_value.populate_existing.return_value.first.return_value = row
        monkeypatch.setattr(excel_service, name, SimpleNamespace(query=query))
    monkeypatch.setattr(excel_service.service, "role_in_project", lambda *_args: "project_admin")
    monkeypatch.setattr(excel_service.permissions, "require", Mock())
    monkeypatch.setattr(
        excel_service.import_job_service, "_lock_special_import_tables",
        lambda: events.append("lock"),
    )

    def capture(*_args):
        events.append("snapshot")
        assert events[0] == "lock"
        return {**snapshot, "rows": [{"id": 99, "version": 1}]}

    monkeypatch.setattr(excel_service.import_job_service, "capture_snapshot", capture)

    with pytest.raises(service.ServiceError) as error:
        excel_service.commit_import(user, project, job)

    assert error.value.code == "IMPORT_PREVIEW_STALE"
    assert events == ["lock", "snapshot", "rollback"]
    session.commit.assert_not_called()
    assert job.status == "previewed"
