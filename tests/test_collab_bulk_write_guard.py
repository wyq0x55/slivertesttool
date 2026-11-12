"""Bulk row writes respect the CRDT single-writer boundary.

Excel import commit, IO extraction and recycle-bin item restore add or revive
rows that a live room's Y.Doc does not contain; the room's next materializer
flush would soft-delete them again. While a room is active these routes must
refuse with COLLAB_ACTIVE, unless the caller is the collaborative editor that
reconciles the written rows into the Y.Doc itself (X-Collab-Reconcile: 1).
"""
from __future__ import annotations

import pytest

from test_import_replace_auth import (  # noqa: F401 - shared fixtures
    ADMIN_PASSWORD, _live_case_ids, _login, app, client, replace_case,
)

RECONCILE = {"X-Collab-Reconcile": "1"}


@pytest.fixture
def collab(monkeypatch):
    from app.collab import presence

    state = {"active": True}

    def is_collab_active(project_id):
        if state["active"] == "unavailable":
            raise RuntimeError("presence unavailable")
        return state["active"]

    monkeypatch.setattr(presence, "is_collab_active", is_collab_active)
    return state


@pytest.fixture
def admin_headers(client, replace_case):
    return _login(client, replace_case["admin_username"], ADMIN_PASSWORD)


def _job_status(job_id):
    from app.extensions import db
    from app.models import DataJob

    db.session.expire_all()
    return db.session.get(DataJob, job_id).status


def test_import_commit_is_refused_while_a_room_is_active(client, replace_case, collab, admin_headers):
    response = client.post(f"/api/v1/imports/{replace_case['job_id']}/commit", headers=admin_headers)
    assert response.status_code == 409
    assert response.get_json()["error"]["code"] == "COLLAB_ACTIVE"
    assert _live_case_ids(replace_case["project_id"]) == [replace_case["keep_case_id"]]
    assert _job_status(replace_case["job_id"]) == "previewed"


def test_reconciling_editor_may_commit_while_a_room_is_active(client, replace_case, collab, admin_headers):
    response = client.post(f"/api/v1/imports/{replace_case['job_id']}/commit",
                           headers={**admin_headers, **RECONCILE})
    assert response.status_code == 200, response.get_data(as_text=True)
    assert _live_case_ids(replace_case["project_id"]) == [replace_case["new_case_id"]]


def test_import_commit_proceeds_without_a_room(client, replace_case, collab, admin_headers):
    collab["active"] = False
    response = client.post(f"/api/v1/imports/{replace_case['job_id']}/commit", headers=admin_headers)
    assert response.status_code == 200, response.get_data(as_text=True)


def test_unknown_presence_fails_closed(client, replace_case, collab, admin_headers):
    collab["active"] = "unavailable"
    response = client.post(f"/api/v1/imports/{replace_case['job_id']}/commit", headers=admin_headers)
    assert response.status_code == 503
    assert response.get_json()["error"]["code"] == "COLLAB_STATE_UNAVAILABLE"
    assert _job_status(replace_case["job_id"]) == "previewed"


@pytest.mark.parametrize("headers,expected", [({}, 409), (RECONCILE, 201)])
def test_io_extraction_follows_the_guard(client, replace_case, collab, admin_headers, monkeypatch,
                                         headers, expected):
    calls = []
    monkeypatch.setattr("app.services.lanmatrix.libconst_bridge.extract_io_from_steps",
                        lambda *args, **kwargs: calls.append(kwargs) or {"created": 0})
    response = client.post(f"/api/v1/projects/{replace_case['project_id']}/io/extract",
                           json={"sheets": ["lib"]}, headers={**admin_headers, **headers})
    assert response.status_code == expected, response.get_data(as_text=True)
    assert len(calls) == (1 if expected == 201 else 0)


def test_item_restore_is_refused_while_a_room_is_active(client, replace_case, collab, admin_headers):
    from app.extensions import db
    from app.models import TestItemRow
    from app.services.lanmatrix.items_service import _utcnow

    row = db.session.get(TestItemRow, replace_case["keep_row_id"])
    row.deleted_at = _utcnow()
    db.session.commit()
    url = f"/api/v1/projects/{replace_case['project_id']}/trash/restore"
    body = {"kind": "item", "id": replace_case["keep_row_id"]}

    response = client.post(url, json=body, headers=admin_headers)
    assert response.status_code == 409
    assert response.get_json()["error"]["code"] == "COLLAB_ACTIVE"
    assert _live_case_ids(replace_case["project_id"]) == []

    collab["active"] = False
    response = client.post(url, json=body, headers=admin_headers)
    assert response.status_code == 200, response.get_data(as_text=True)
    assert _live_case_ids(replace_case["project_id"]) == [replace_case["keep_case_id"]]


def test_non_row_restore_is_not_guarded(client, replace_case, collab, admin_headers, monkeypatch):
    calls = []
    monkeypatch.setattr("app.routes.lanmatrix.audit_trash.trash_service.restore",
                        lambda user, project, kind, obj_id: calls.append(kind) or {"kind": kind})
    response = client.post(f"/api/v1/projects/{replace_case['project_id']}/trash/restore",
                           json={"kind": "field", "id": 1}, headers=admin_headers)
    assert response.status_code == 200, response.get_data(as_text=True)
    assert calls == ["field"]
