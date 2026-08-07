"""Field soft-delete and permission wiring for the recycle bin (#10).

Deleting a field used to be the single most destructive action in the product:
it dropped a column from every test case *and* purged the stored values out of
each row's ``custom_values`` in the same transaction, with no way back. These
pin the split between "delete" (reversible, values untouched) and "purge" (the
only thing allowed to touch ``custom_values``).

The models are swapped for evaluatable fakes, so this runs without PostgreSQL.
"""
import datetime as dt
import os
import sys
import unittest


def _find_repo():
    here = os.path.dirname(os.path.abspath(__file__))
    for cand in (os.path.dirname(here), here):
        if os.path.isdir(os.path.join(cand, "app", "services", "lanmatrix")):
            return cand
    raise AssertionError("repo root not found from " + here)


REPO = _find_repo()
if REPO not in sys.path:
    sys.path.insert(0, REPO)

from app.services.lanmatrix import fields_service as fs  # noqa: E402
from app.services.lanmatrix import permissions as perms  # noqa: E402
from app.services.lanmatrix.errors import ServiceError  # noqa: E402

UTC = dt.timezone.utc


# --------------------------------------------------------------------------- #
# Fakes
# --------------------------------------------------------------------------- #
class _Col:
    def __init__(self, name):
        self.name = name

    def _get(self, row):
        return getattr(row, self.name, None)

    def is_(self, other):
        return lambda row: self._get(row) is other

    def isnot(self, other):
        return lambda row: self._get(row) is not other

    def __eq__(self, other):
        return lambda row: self._get(row) == other

    def __hash__(self):
        return id(self)


class FakeQuery:
    def __init__(self, rows, preds=()):
        self._rows, self._preds = rows, list(preds)

    def filter(self, *preds):
        return FakeQuery(self._rows, self._preds + list(preds))

    def filter_by(self, **kw):
        def pred(row, kw=kw):
            return all(getattr(row, k, None) == v for k, v in kw.items())
        return FakeQuery(self._rows, self._preds + [pred])

    def order_by(self, *a):
        return self

    def _matching(self):
        return [r for r in self._rows if all(p(r) for p in self._preds)]

    def all(self):
        return self._matching()

    def first(self):
        m = self._matching()
        return m[0] if m else None

    def count(self):
        return len(self._matching())


class FDef:
    def __init__(self, i, project_id=1, key="prio", deleted_at=None,
                 deleted_by=None, is_active=True, display_order=0,
                 sheet="test"):
        self.id, self.project_id, self.field_key = i, project_id, key
        self.deleted_at, self.deleted_by = deleted_at, deleted_by
        self.is_active, self.display_order = is_active, display_order
        self.sheet, self.display_name = sheet, key

    def to_dict(self):
        return {"id": self.id, "field_key": self.field_key}


class ItemRow:
    def __init__(self, i, project_id=1, custom_values=None):
        self.id, self.project_id = i, project_id
        self.custom_values = custom_values or {}


class FakeSession:
    def __init__(self):
        self.deleted, self.commits = [], 0

    def delete(self, obj):
        self.deleted.append(obj)

    def commit(self):
        self.commits += 1


class User:
    def __init__(self, i=7):
        self.id = i


class Project:
    def __init__(self, i=1):
        self.id = i


def model(name, cols, rows):
    ns = {c: _Col(c) for c in cols}
    ns["query"] = FakeQuery(rows)
    return type(name, (object,), ns)


FDEF_COLS = ("id", "project_id", "field_key", "deleted_at", "deleted_by",
             "is_active", "display_order", "sheet")
ITEM_COLS = ("id", "project_id")


class FieldsBase(unittest.TestCase):
    def setUp(self):
        self._saved = {k: getattr(fs, k) for k in
                       ("FieldDefinition", "TestItemRow", "db", "audit")}
        self.audit_calls = []
        self.session = FakeSession()

        class FakeAudit:
            record = staticmethod(
                lambda action, **kw: self.audit_calls.append((action, kw)))

        class FakeDb:
            pass
        FakeDb.session = self.session

        fs.audit = FakeAudit
        fs.db = FakeDb
        self.fields, self.items = [], []
        self.install()

    def install(self):
        fs.FieldDefinition = model("FieldDefinition", FDEF_COLS, self.fields)
        fs.TestItemRow = model("TestItemRow", ITEM_COLS, self.items)

    def tearDown(self):
        for k, v in self._saved.items():
            setattr(fs, k, v)

    def actions(self):
        return [a for a, _ in self.audit_calls]


# --------------------------------------------------------------------------- #
# delete_field: reversible, values untouched
# --------------------------------------------------------------------------- #
class TestDeleteField(FieldsBase):
    def test_definition_row_survives(self):
        """It has to, or there is nothing left to restore."""
        f = FDef(1)
        self.fields[:] = [f]
        self.install()
        fs.delete_field(User(), Project(), f)
        self.assertEqual(self.session.deleted, [])

    def test_timestamp_is_stamped(self):
        f = FDef(1)
        fs.delete_field(User(), Project(), f)
        self.assertIsNotNone(f.deleted_at)

    def test_values_stay_in_every_row(self):
        """The values are what makes the restore worth having.

        The old implementation popped the key out of every row in the same
        transaction, which is precisely why the deletion could not be undone.
        """
        f = FDef(1, key="prio")
        self.fields[:] = [f]
        self.items[:] = [ItemRow(1, custom_values={"prio": "P1", "x": "1"}),
                         ItemRow(2, custom_values={"prio": "P2"})]
        self.install()
        fs.delete_field(User(), Project(), f)
        self.assertEqual(self.items[0].custom_values, {"prio": "P1", "x": "1"})
        self.assertEqual(self.items[1].custom_values, {"prio": "P2"})

    def test_records_who_deleted_it(self):
        f = FDef(1)
        fs.delete_field(User(42), Project(), f)
        self.assertEqual(f.deleted_by, 42)

    def test_deleting_twice_keeps_the_first_timestamp(self):
        f = FDef(1, deleted_at=dt.datetime(2025, 1, 1, tzinfo=UTC))
        fs.delete_field(User(), Project(), f)
        self.assertEqual(f.deleted_at, dt.datetime(2025, 1, 1, tzinfo=UTC))

    def test_deleting_twice_is_not_audited_twice(self):
        f = FDef(1, deleted_at=dt.datetime(2025, 1, 1, tzinfo=UTC))
        fs.delete_field(User(), Project(), f)
        self.assertEqual(self.actions(), [])

    def test_is_audited(self):
        fs.delete_field(User(), Project(), FDef(1))
        self.assertEqual(self.actions(), ["field.delete"])

    def test_commit_can_be_deferred(self):
        fs.delete_field(User(), Project(), FDef(1), commit=False)
        self.assertEqual(self.session.commits, 0)


class TestRestoreField(FieldsBase):
    def test_clears_the_timestamp(self):
        f = FDef(1, deleted_at=dt.datetime(2025, 1, 1, tzinfo=UTC))
        fs.restore_field(User(), Project(), f)
        self.assertIsNone(f.deleted_at)

    def test_clears_deleted_by(self):
        f = FDef(1, deleted_at=dt.datetime(2025, 1, 1, tzinfo=UTC),
                 deleted_by=9)
        fs.restore_field(User(), Project(), f)
        self.assertIsNone(f.deleted_by)

    def test_live_field_is_not_found(self):
        with self.assertRaises(ServiceError) as cm:
            fs.restore_field(User(), Project(), FDef(1))
        self.assertEqual(cm.exception.code, "NOT_FOUND")

    def test_is_audited(self):
        f = FDef(1, deleted_at=dt.datetime(2025, 1, 1, tzinfo=UTC))
        fs.restore_field(User(), Project(), f)
        self.assertEqual(self.actions(), ["field.restore"])


# --------------------------------------------------------------------------- #
# purge_field: the only thing allowed to touch custom_values
# --------------------------------------------------------------------------- #
class TestPurgeField(FieldsBase):
    def setUp(self):
        super().setUp()
        self.f = FDef(1, key="prio",
                      deleted_at=dt.datetime(2025, 1, 1, tzinfo=UTC))
        self.fields[:] = [self.f]
        self.items[:] = [ItemRow(1, custom_values={"prio": "P1", "x": "1"}),
                         ItemRow(2, custom_values={"other": "v"})]
        self.install()

    def test_values_are_dropped(self):
        fs.purge_field(User(), 1, self.f)
        self.assertEqual(self.items[0].custom_values, {"x": "1"})

    def test_untouched_keys_survive(self):
        fs.purge_field(User(), 1, self.f)
        self.assertEqual(self.items[1].custom_values, {"other": "v"})

    def test_custom_values_is_reassigned_not_mutated(self):
        """SQLAlchemy will not notice an in-place edit of a JSON column."""
        before = self.items[0].custom_values
        fs.purge_field(User(), 1, self.f)
        self.assertIsNot(self.items[0].custom_values, before)

    def test_definition_row_is_deleted(self):
        fs.purge_field(User(), 1, self.f)
        self.assertIn(self.f, self.session.deleted)

    def test_is_audited(self):
        fs.purge_field(User(), 1, self.f)
        self.assertEqual(self.actions(), ["field.purge"])

    def test_survives_a_null_actor(self):
        """The retention sweep has no user attached."""
        fs.purge_field(None, 1, self.f)
        self.assertIsNone(self.audit_calls[0][1]["actor_id"])

    def test_commit_can_be_deferred(self):
        fs.purge_field(User(), 1, self.f, commit=False)
        self.assertEqual(self.session.commits, 0)


# --------------------------------------------------------------------------- #
# Listing
# --------------------------------------------------------------------------- #
class TestListFields(FieldsBase):
    def test_deleted_fields_are_hidden_by_default(self):
        self.fields[:] = [FDef(1, key="a"),
                          FDef(2, key="b",
                               deleted_at=dt.datetime(2025, 1, 1, tzinfo=UTC))]
        self.install()
        self.assertEqual([f.field_key for f in fs.list_fields(1)], ["a"])

    def test_include_deleted_brings_them_back(self):
        self.fields[:] = [FDef(1, key="a"),
                          FDef(2, key="b",
                               deleted_at=dt.datetime(2025, 1, 1, tzinfo=UTC))]
        self.install()
        got = fs.list_fields(1, include_deleted=True)
        self.assertEqual([f.field_key for f in got], ["a", "b"])

    def test_project_scoped(self):
        self.fields[:] = [FDef(1, key="a", project_id=2)]
        self.install()
        self.assertEqual(fs.list_fields(1), [])


class TestAddFieldClash(FieldsBase):
    def test_key_sitting_in_the_bin_points_at_the_bin(self):
        """The unique constraint covers soft-deleted rows.

        A generic "字段标识已存在" would leave the user hunting for a field they
        cannot see anywhere in the editor.
        """
        self.fields[:] = [FDef(1, key="prio",
                               deleted_at=dt.datetime(2025, 1, 1, tzinfo=UTC))]
        self.install()
        with self.assertRaises(ServiceError) as cm:
            fs.add_field(User(), Project(), {"field_key": "prio"})
        self.assertIn("回收站", str(cm.exception))

    def test_live_clash_keeps_the_plain_message(self):
        self.fields[:] = [FDef(1, key="prio")]
        self.install()
        with self.assertRaises(ServiceError) as cm:
            fs.add_field(User(), Project(), {"field_key": "prio"})
        self.assertNotIn("回收站", str(cm.exception))


class TestEnsureFields(FieldsBase):
    """The Lib/Const importers provision their field set before writing rows."""

    def test_a_deleted_key_is_revived_not_rejected(self):
        """The importer needs the key live, and its values live under it."""
        f = FDef(1, key="prio", deleted_at=dt.datetime(2025, 1, 1, tzinfo=UTC))
        self.fields[:] = [f]
        self.install()
        created = fs.ensure_fields(User(), Project(), [{"field_key": "prio"}])
        self.assertIsNone(f.deleted_at)
        self.assertEqual(created, 0)

    def test_a_live_key_is_left_alone(self):
        f = FDef(1, key="prio")
        self.fields[:] = [f]
        self.install()
        self.assertEqual(
            fs.ensure_fields(User(), Project(), [{"field_key": "prio"}]), 0)
        self.assertEqual(self.actions(), [])


# --------------------------------------------------------------------------- #
# Permissions
# --------------------------------------------------------------------------- #
class TestTrashPermissions(unittest.TestCase):
    def test_editor_can_see_the_bin(self):
        self.assertTrue(perms.can("trash.view", "editor"))

    def test_editor_can_undo_their_own_deletion(self):
        """Gating undo behind an admin puts it out of reach of the one person
        who needs it: whoever just made the mistake."""
        self.assertTrue(perms.can("trash.restore", "editor"))

    def test_reader_cannot_see_the_bin(self):
        self.assertFalse(perms.can("trash.view", "reader"))

    def test_reader_cannot_restore(self):
        self.assertFalse(perms.can("trash.restore", "reader"))

    def test_editor_cannot_purge_for_real(self):
        """Purging is the step nobody can take back."""
        self.assertFalse(perms.can("trash.purge", "editor"))

    def test_admin_can_purge(self):
        self.assertTrue(perms.can("trash.purge", "project_admin"))

    def test_non_member_gets_nothing(self):
        for cap in ("trash.view", "trash.restore", "trash.purge"):
            self.assertFalse(perms.can(cap, None), cap)

    def test_system_admin_overrides(self):
        self.assertTrue(
            perms.can("trash.purge", None, is_system_admin=True))


if __name__ == "__main__":
    unittest.main()
