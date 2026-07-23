"""Materialization (CRDT / Y.Doc -> DB reconcile) service tests.

Covers the ``items_service`` helpers added for real-time collaboration:

* the ``commit`` flag on the single-row/bulk write functions (so a whole
  Y.Array snapshot can land in one transaction), and
* ``materialize_create`` / ``materialize_update`` / ``materialize_sheet`` —
  uuid-keyed upsert that never raises ``VersionConflict`` and treats the Y.Doc
  as the source of truth for content and ordering.

Requires the standard PostgreSQL test database (see ``conftest.py``). Run::

    pytest tests/test_lanmatrix_materialize.py
"""

from __future__ import annotations

import pytest


@pytest.fixture()
def env(app_ctx):
    """A pushed app context with a fresh user + editable project."""
    from app.extensions import db
    from app.models import LMUser, Project

    with app_ctx.app_context():
        user = LMUser(username="mat_user", display_name="Mat")
        db.session.add(user)
        db.session.flush()
        project = Project(code="MATPRJ", name="Materialize", status="active",
                          owner_id=user.id, created_by=user.id)
        db.session.add(project)
        db.session.commit()
        yield app_ctx, user, project


def _svc():
    from app.services.lanmatrix import items_service
    return items_service


def _uuids(project, sheet="test"):
    """Live rows on a sheet, ordered by row_order -> {uuid: (order, title)}."""
    from app.models import TestItemRow
    rows = TestItemRow.query.filter_by(
        project_id=project.id, sheet=sheet, deleted_at=None
    ).order_by(TestItemRow.row_order.asc()).all()
    return [(r.uuid, r.row_order, r.title) for r in rows]


def test_materialize_create_preserves_uuid_and_autofills_case_id(env):
    app, user, project = env
    svc = _svc()
    with app.app_context():
        item = svc.materialize_create(
            user, project, {"uuid": "u" * 32, "title": "Hello"},
            sheet="test", row_order=1, commit=True)
        assert item.uuid == "u" * 32
        assert item.title == "Hello"
        assert item.case_id.strip()          # auto-generated, NOT NULL held
        assert item.version == 1


def test_materialize_update_no_version_conflict_and_bumps_version(env):
    app, user, project = env
    svc = _svc()
    from app.services.lanmatrix.errors import VersionConflict
    with app.app_context():
        item = svc.materialize_create(
            user, project, {"uuid": "a" * 32, "title": "v1"},
            sheet="test", row_order=1, commit=True)
        v0 = item.version
        # No ``version`` argument, no conflict raised even on repeated writes.
        try:
            svc.materialize_update(user, project, item, {"title": "v2"}, commit=True)
            svc.materialize_update(user, project, item, {"title": "v3"}, commit=True)
        except VersionConflict:  # pragma: no cover - must never happen
            pytest.fail("materialize_update must not raise VersionConflict")
        assert item.title == "v3"
        assert item.version == v0 + 2


def test_materialize_sheet_create_update_reorder_delete(env):
    app, user, project = env
    svc = _svc()
    u1, u2 = "1" * 32, "2" * 32
    with app.app_context():
        svc.materialize_create(user, project, {"uuid": u1, "title": "A"},
                               sheet="test", row_order=1, commit=True)
        svc.materialize_create(user, project, {"uuid": u2, "title": "B"},
                               sheet="test", row_order=2, commit=True)

        u3 = "3" * 32
        summary = svc.materialize_sheet(user, project, "test", [
            {"uuid": u2, "title": "B2"},
            {"uuid": u1, "title": "A2"},
            {"uuid": u3, "title": "C"},
        ])
        assert summary == {"created": 1, "updated": 2, "removed": 0, "total": 3}
        # Order follows the snapshot index; titles reflect the updates.
        assert _uuids(project) == [(u2, 1, "B2"), (u1, 2, "A2"), (u3, 3, "C")]

        # A shorter snapshot soft-deletes the rows that dropped out.
        summary2 = svc.materialize_sheet(user, project, "test", [
            {"uuid": u1, "title": "A3"},
        ])
        assert summary2 == {"created": 0, "updated": 1, "removed": 2, "total": 1}
        assert _uuids(project) == [(u1, 1, "A3")]


def test_materialize_sheet_resurrects_soft_deleted_row(env):
    app, user, project = env
    svc = _svc()
    u1 = "d" * 32
    with app.app_context():
        item = svc.materialize_create(user, project, {"uuid": u1, "title": "keep"},
                                      sheet="test", row_order=1, commit=True)
        svc.materialize_sheet(user, project, "test", [])   # drop it -> soft delete
        assert item.deleted_at is not None
        # It reappears in the Y.Array -> row is resurrected (same DB id/uuid).
        summary = svc.materialize_sheet(user, project, "test",
                                        [{"uuid": u1, "title": "back"}])
        assert summary["updated"] == 1 and summary["created"] == 0
        assert item.deleted_at is None
        assert _uuids(project) == [(u1, 1, "back")]


def test_materialize_sheet_skips_rows_without_uuid(env):
    app, user, project = env
    svc = _svc()
    with app.app_context():
        summary = svc.materialize_sheet(user, project, "test", [
            {"title": "no uuid"},
            {"uuid": "", "title": "blank uuid"},
            {"uuid": "e" * 32, "title": "ok"},
        ])
        assert summary == {"created": 1, "updated": 0, "removed": 0, "total": 3}
        assert [t for _, _, t in _uuids(project)] == ["ok"]


def test_commit_false_defers_persistence(env):
    app, user, project = env
    svc = _svc()
    from app.extensions import db
    from app.models import TestItemRow
    with app.app_context():
        svc.materialize_create(user, project, {"uuid": "f" * 32, "title": "pending"},
                               sheet="test", commit=False)
        db.session.rollback()                       # nothing was committed
        assert TestItemRow.query.filter_by(project_id=project.id).count() == 0
