"""Field-key unification ("identity" protocol) data-layer tests.

The Test-Matrix editor speaks a single field vocabulary. Two of its keys alias
onto existing first-class columns (``test_name`` -> ``title``,
``remark`` -> ``comment``; see ``TestItemRow._FIELD_ALIASES``). These tests lock
in that the aliases route to real columns — restoring searchability / sortability
/ indexability — and that the one-time boot migration lifts legacy rows whose
values were stranded in the ``custom_values`` JSONB bag.

Requires the standard PostgreSQL test database (see ``conftest.py``). Run::

    pytest tests/test_lanmatrix_identity_columns.py

The JSONB pre-filter in ``_migrate_testitem_field_keys`` (``jsonb_exists_any``)
is exercised only on PostgreSQL; on other dialects the migration falls back to a
full scan with identical results.
"""

from __future__ import annotations

import pytest


@pytest.fixture()
def env(app_ctx):
    """A pushed app context with a fresh user + Test-Matrix-seeded project."""
    from app.extensions import db
    from app.models import LMUser, Project
    from app.services.lanmatrix import fields as fld, fields_service

    with app_ctx.app_context():
        user = LMUser(username="ident_user", display_name="Ident")
        db.session.add(user)
        db.session.flush()
        project = Project(code="IDENTPRJ", name="Identity", status="active",
                          owner_id=user.id, created_by=user.id)
        db.session.add(project)
        db.session.commit()
        # Provision the Test-Matrix field set so test_name/remark specs exist.
        fields_service.ensure_fields(user, project, fld.TEST_FIELDS)
        db.session.commit()
        yield app_ctx, user, project


def _svc():
    from app.services.lanmatrix import items_service
    return items_service


def test_new_values_land_in_first_class_columns(env):
    app, user, project = env
    with app.app_context():
        item = _svc().create_item(
            user, project,
            {"test_name": "Boot check", "remark": "Note A", "result": "Pass"})
        # Stored in real columns, not the JSONB bag.
        assert item.title == "Boot check"
        assert item.comment == "Note A"
        cv = item.custom_values or {}
        assert "test_name" not in cv
        assert "remark" not in cv
        # to_dict speaks the unified vocabulary.
        payload = item.to_dict()
        assert payload["test_name"] == "Boot check"
        assert payload["remark"] == "Note A"


def test_quick_search_matches_test_name_and_remark(env):
    app, user, project = env
    with app.app_context():
        svc = _svc()
        svc.create_item(user, project, {"test_name": "Ignition sequence"})
        svc.create_item(user, project, {"test_name": "Shutdown", "remark": "edge case"})
        # Search by test_name (backed by the title column).
        by_name = svc.list_items(project.id, quick="Ignition")
        assert by_name["total"] == 1
        assert by_name["items"][0]["test_name"] == "Ignition sequence"
        # Search by remark (backed by the comment column).
        by_remark = svc.list_items(project.id, quick="edge case")
        assert by_remark["total"] == 1
        assert by_remark["items"][0]["test_name"] == "Shutdown"


def test_sort_by_test_name_orders_rows(env):
    app, user, project = env
    with app.app_context():
        svc = _svc()
        for name in ("Charlie", "Alpha", "Bravo"):
            svc.create_item(user, project, {"test_name": name})
        res = svc.list_items(project.id, sort="test_name:asc")
        names = [row["test_name"] for row in res["items"]]
        assert names == ["Alpha", "Bravo", "Charlie"]


def test_boot_migration_lifts_legacy_jsonb_rows(env):
    app, user, project = env
    from app import _migrate_testitem_field_keys
    from app.extensions import db
    from app.models import TestItemRow

    with app.app_context():
        legacy = TestItemRow(
            project_id=project.id, sheet="test", title="", comment="",
            custom_values={"test_name": "Legacy title",
                           "remark": "Legacy remark",
                           "purpose": "keep me"})
        db.session.add(legacy)
        db.session.commit()
        legacy_id = legacy.id
        # Before migration to_dict is still correct via the JSONB overlay.
        assert legacy.to_dict()["test_name"] == "Legacy title"

        _migrate_testitem_field_keys({"lm_test_items"})
        db.session.expire_all()

        migrated = db.session.get(TestItemRow, legacy_id)
        assert migrated.title == "Legacy title"
        assert migrated.comment == "Legacy remark"
        cv = migrated.custom_values or {}
        assert "test_name" not in cv
        assert "remark" not in cv
        # Non-alias JSONB keys must survive untouched.
        assert cv.get("purpose") == "keep me"


def test_boot_migration_is_idempotent_and_non_destructive(env):
    app, user, project = env
    from app import _migrate_testitem_field_keys
    from app.extensions import db
    from app.models import TestItemRow

    with app.app_context():
        row = TestItemRow(
            project_id=project.id, sheet="test", title="", comment="",
            custom_values={"test_name": "Once"})
        db.session.add(row)
        db.session.commit()
        row_id = row.id

        _migrate_testitem_field_keys({"lm_test_items"})
        db.session.expire_all()
        edited = db.session.get(TestItemRow, row_id)
        assert edited.title == "Once"

        # A later explicit column edit must never be clobbered by a re-run.
        edited.title = "Manually edited"
        db.session.commit()
        _migrate_testitem_field_keys({"lm_test_items"})
        db.session.expire_all()
        assert db.session.get(TestItemRow, row_id).title == "Manually edited"
