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


def _counts(summary):
    """The count fields of a materialize summary, without the free-form details.

    Comparing a whole summary against a dict literal couples every test to the
    exact key set, which is how these assertions silently rotted: the service
    already returned ``failed``/``errors`` while the tests still compared
    against a 4-key literal, and nothing caught it because this module needs
    PostgreSQL to run.
    """
    return {k: summary[k] for k in
            ("created", "updated", "removed", "failed", "unchanged")}


def _materialize_audits(project):
    """How many ``item.materialize`` audit entries this project has."""
    from app.models import AuditLog
    return AuditLog.query.filter_by(project_id=project.id,
                                    action="item.materialize").count()


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
        assert _counts(summary) == {"created": 1, "updated": 2, "removed": 0,
                                    "failed": 0, "unchanged": 0}
        assert summary["total"] == 3
        # Order follows the snapshot index; titles reflect the updates.
        assert _uuids(project) == [(u2, 1, "B2"), (u1, 2, "A2"), (u3, 3, "C")]

        # A shorter snapshot soft-deletes the rows that dropped out.
        summary2 = svc.materialize_sheet(user, project, "test", [
            {"uuid": u1, "title": "A3"},
        ])
        assert _counts(summary2) == {"created": 0, "updated": 1, "removed": 2,
                                     "failed": 0, "unchanged": 0}
        assert summary2["total"] == 1
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
        assert _counts(summary) == {"created": 1, "updated": 0, "removed": 0,
                                    "failed": 0, "unchanged": 0}
        assert summary["total"] == 3
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


# --------------------------------------------------------------------------- #
# Write amplification: a debounced collab flush replays the WHOLE sheet, so an
# unchanged row must cost nothing. Before this was enforced, one edited cell in
# a 200-row sheet bumped 200 versions and wrote 200 audit entries -- 199 of them
# rendering in the audit UI as a change with an EMPTY field-level diff.
# --------------------------------------------------------------------------- #

def _sheet(n, over=None):
    """A deterministic n-row snapshot; ``over`` patches rows by index."""
    rows = [{"uuid": f"{i:032d}", "title": f"row {i}", "custom_col": f"c{i}"}
            for i in range(n)]
    for idx, changes in (over or {}).items():
        rows[idx].update(changes)
    return rows


def test_materialize_sheet_leaves_unchanged_rows_completely_alone(env):
    app, user, project = env
    svc = _svc()
    from app.extensions import db
    from app.models import TestItemRow
    with app.app_context():
        rows = _sheet(12)
        svc.materialize_sheet(user, project, "test", rows)
        db.session.expire_all()
        before = {r.uuid: (r.version, r.updated_at)
                  for r in TestItemRow.query.filter_by(project_id=project.id)}
        audits_before = _materialize_audits(project)

        # Replay the identical snapshot, exactly as an idle debounce would.
        summary = svc.materialize_sheet(user, project, "test", rows)

        assert _counts(summary) == {"created": 0, "updated": 0, "removed": 0,
                                    "failed": 0, "unchanged": 12}
        db.session.expire_all()
        after = {r.uuid: (r.version, r.updated_at)
                 for r in TestItemRow.query.filter_by(project_id=project.id)}
        assert after == before, "a no-op flush must not touch version/updated_at"
        assert _materialize_audits(project) == audits_before, \
            "a no-op flush must not write audit entries"


def test_materialize_sheet_one_edit_touches_only_that_row(env):
    app, user, project = env
    svc = _svc()
    from app.extensions import db
    from app.models import TestItemRow
    with app.app_context():
        rows = _sheet(12)
        svc.materialize_sheet(user, project, "test", rows)
        db.session.expire_all()
        before = {r.uuid: r.version
                  for r in TestItemRow.query.filter_by(project_id=project.id)}
        audits_before = _materialize_audits(project)

        edited = _sheet(12, {7: {"title": "EDITED"}})
        summary = svc.materialize_sheet(user, project, "test", edited)

        assert _counts(summary) == {"created": 0, "updated": 1, "removed": 0,
                                    "failed": 0, "unchanged": 11}
        # Exactly one audit entry, for exactly one row.
        assert _materialize_audits(project) == audits_before + 1
        db.session.expire_all()
        bumped = [r.uuid for r in TestItemRow.query.filter_by(project_id=project.id)
                  if r.version != before[r.uuid]]
        assert bumped == [f"{7:032d}"]
        assert TestItemRow.query.filter_by(uuid=f"{7:032d}").one().title == "EDITED"


def test_materialize_sheet_records_a_pure_reorder(env):
    """The dirty check must not suppress moves: row_order is real state."""
    app, user, project = env
    svc = _svc()
    from app.extensions import db
    with app.app_context():
        rows = _sheet(4)
        svc.materialize_sheet(user, project, "test", rows)
        db.session.expire_all()
        audits_before = _materialize_audits(project)

        moved = [rows[3]] + rows[0:3]          # same content, new order
        summary = svc.materialize_sheet(user, project, "test", moved)

        assert summary["updated"] == 4 and summary["unchanged"] == 0
        assert _materialize_audits(project) > audits_before
        assert [u for u, _, _ in _uuids(project)] == [r["uuid"] for r in moved]


def test_materialize_sheet_detects_a_custom_field_edit(env):
    """Custom values live in a JSON column that set_field rebuilds every call;
    the dirty check must still see a real change there (and only there)."""
    app, user, project = env
    svc = _svc()
    with app.app_context():
        rows = _sheet(5)
        svc.materialize_sheet(user, project, "test", rows)
        edited = _sheet(5, {2: {"custom_col": "CHANGED"}})
        summary = svc.materialize_sheet(user, project, "test", edited)
        assert _counts(summary) == {"created": 0, "updated": 1, "removed": 0,
                                    "failed": 0, "unchanged": 4}


def test_materialize_sheet_records_resurrect_of_identical_row(env):
    """Undelete is a state change even when no other field moved.

    This is the edge the dirty check is most likely to swallow: if deleted_at
    were cleared *after* the check instead of before it, the row would look
    untouched and stay deleted.
    """
    app, user, project = env
    svc = _svc()
    from app.extensions import db
    from app.models import TestItemRow
    u1 = "r" * 32
    with app.app_context():
        row = {"uuid": u1, "title": "same", "custom_col": "same"}
        svc.materialize_sheet(user, project, "test", [row])
        svc.materialize_sheet(user, project, "test", [])          # soft delete
        db.session.expire_all()
        assert TestItemRow.query.filter_by(uuid=u1).one().deleted_at is not None
        audits_before = _materialize_audits(project)

        summary = svc.materialize_sheet(user, project, "test", [dict(row)])

        assert summary["updated"] == 1 and summary["unchanged"] == 0
        assert _materialize_audits(project) == audits_before + 1
        db.session.expire_all()
        assert TestItemRow.query.filter_by(uuid=u1).one().deleted_at is None
        assert _uuids(project) == [(u1, 1, "same")]


def test_materialize_update_noop_does_not_bump_version(env):
    app, user, project = env
    svc = _svc()
    with app.app_context():
        item = svc.materialize_create(
            user, project, {"uuid": "n" * 32, "title": "same"},
            sheet="test", row_order=1, commit=True)
        v0, touched0 = item.version, item.updated_at
        svc.materialize_update(user, project, item, {"title": "same"}, commit=True)
        assert item.version == v0, "identical content must not bump version"
        assert item.updated_at == touched0
        svc.materialize_update(user, project, item, {"title": "different"},
                               commit=True)
        assert item.version == v0 + 1
