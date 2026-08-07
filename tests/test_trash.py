"""Recycle bin: listing, retention arithmetic, restore/purge, sweep (#10).

Deleting a field, a row or a task used to be final, and deleting a field also
wiped its values out of every row on the way past. These cover the soft-delete
replacement.

The fakes here implement real filter *semantics* (``__lt__``, ``isnot``, ``==``
build predicates that are actually evaluated against the rows) rather than
swallowing every filter and returning a fixed corpus. A fake that ignores
filters would pass just as happily against a sweep that purges everything, or a
listing that hands back live rows alongside deleted ones.

Pure Python throughout, so they run without PostgreSQL.
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

from app.services.lanmatrix import trash_service as ts  # noqa: E402
from app.services.lanmatrix.errors import ServiceError  # noqa: E402

UTC = dt.timezone.utc


def aware(*a):
    return dt.datetime(*a, tzinfo=UTC)


# --------------------------------------------------------------------------- #
# Fakes
# --------------------------------------------------------------------------- #
def _bare(value):
    """Both operands as Postgres sees them: TIMESTAMP WITHOUT TIME ZONE.

    The columns are declared without ``timezone=True``, so the database strips
    the offset on both sides before comparing. The fake does the same, otherwise
    it would reject a mix that the real query accepts.
    """
    if isinstance(value, dt.datetime) and value.tzinfo is not None:
        return value.astimezone(UTC).replace(tzinfo=None)
    return value


class _Col:
    """A mapped column stand-in that builds evaluatable predicates."""

    def __init__(self, name):
        self.name = name

    def _get(self, row):
        return getattr(row, self.name, None)

    def desc(self):
        return self

    def asc(self):
        return self

    def isnot(self, other):
        if other is None:
            return lambda row: self._get(row) is not None
        return lambda row: self._get(row) is not other

    def is_(self, other):
        return lambda row: self._get(row) is other

    def in_(self, values):
        vals = set(values)
        return lambda row: self._get(row) in vals

    def __eq__(self, other):
        return lambda row: self._get(row) == other

    def __lt__(self, other):
        return lambda row: (self._get(row) is not None
                            and _bare(self._get(row)) < _bare(other))

    def __gt__(self, other):
        return lambda row: (self._get(row) is not None
                            and _bare(self._get(row)) > _bare(other))

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

    def count(self):
        return len(self._matching())

    def first(self):
        m = self._matching()
        return m[0] if m else None


class Row:
    """A stand-in row; the class object carries the _Col descriptors."""

    _COLS = ()

    def __init__(self, **kw):
        for c in self._COLS:
            setattr(self, c, None)
        for k, v in kw.items():
            setattr(self, k, v)


def make_model(name, cols, rows):
    """Build a model class whose ``.query`` serves ``rows``."""
    ns = {c: _Col(c) for c in cols}
    ns["_COLS"] = cols
    ns["query"] = FakeQuery(rows)
    return type(name, (Row,), ns)


ITEM_COLS = ("id", "project_id", "case_id", "title", "deleted_at")
FIELD_COLS = ("id", "project_id", "field_key", "display_name", "sheet",
              "deleted_at", "deleted_by")
TASK_COLS = ("task_key", "project_id", "task_name", "test_id", "status",
             "deleted_at", "deleted_by")
USER_COLS = ("id", "username", "display_name")


class FakeSession:
    def __init__(self):
        self.deleted, self.commits = [], 0

    def delete(self, obj):
        self.deleted.append(obj)

    def commit(self):
        self.commits += 1


class TrashBase(unittest.TestCase):
    """Swaps the module's model references for evaluatable fakes."""

    def setUp(self):
        self._saved = {k: getattr(ts, k) for k in
                       ("TestItemRow", "FieldDefinition", "Task", "LMUser",
                        "db", "audit", "fields_service", "items_service",
                        "task_service")}
        self.audit_calls = []
        self.session = FakeSession()

        class FakeAudit:
            record = staticmethod(
                lambda action, **kw: self.audit_calls.append((action, kw)))

        class FakeDb:
            pass
        FakeDb.session = self.session

        self.purged_fields, self.purged_tasks = [], []
        self.restored = []

        class FakeFields:
            @staticmethod
            def purge_field(user, project_id, fdef, commit=True):
                self.purged_fields.append((fdef, commit))

            @staticmethod
            def restore_field(user, project, fdef, commit=True):
                fdef.deleted_at = None
                self.restored.append(("field", fdef))
                return fdef

        class FakeItems:
            @staticmethod
            def restore_item(user, project, item_id, commit=True):
                row = _find(self.items, "id", item_id)
                if row is None or row.deleted_at is None:
                    raise ServiceError("回收站中无此记录", code="NOT_FOUND")
                row.deleted_at = None
                self.restored.append(("item", row))
                return row

        class FakeTasks:
            @staticmethod
            def purge_task(task, commit=True):
                self.purged_tasks.append(
                    (_snapshot(task, TASK_COLS), commit))
                # SQLAlchemy expires a deleted instance, so reading its columns
                # afterwards no longer yields the values it had. Model that, or
                # code that audits *after* purging looks perfectly fine here and
                # writes a blank object_id in production.
                for c in TASK_COLS:
                    setattr(task, c, None)

            @staticmethod
            def restore_task(task, commit=True):
                task.deleted_at = None
                self.restored.append(("task", task))
                return task

        ts.audit = FakeAudit
        ts.db = FakeDb
        ts.fields_service = FakeFields
        ts.items_service = FakeItems
        ts.task_service = FakeTasks
        self.items, self.fields, self.tasks, self.users = [], [], [], []
        self.install()

    def install(self):
        ts.TestItemRow = make_model("TestItemRow", ITEM_COLS, self.items)
        ts.FieldDefinition = make_model("FieldDefinition", FIELD_COLS,
                                        self.fields)
        ts.Task = make_model("Task", TASK_COLS, self.tasks)
        ts.LMUser = make_model("LMUser", USER_COLS, self.users)

    def tearDown(self):
        for k, v in self._saved.items():
            setattr(ts, k, v)


def _snapshot(obj, cols):
    """Freeze a row's column values before the instance is expired."""
    return Row(**{c: getattr(obj, c, None) for c in cols})


def _find(rows, attr, value):
    for r in rows:
        if getattr(r, attr) == value:
            return r
    return None


def item(i, project_id=1, deleted_at=None, case_id=None, title="",
         deleted_by=None):
    return Row(id=i, project_id=project_id, deleted_at=deleted_at,
               case_id=(case_id if case_id is not None else "TC-%03d" % i),
               title=title, deleted_by=deleted_by)


def field(i, project_id=1, deleted_at=None, key=None, name=None,
          sheet="test", deleted_by=None):
    return Row(id=i, project_id=project_id, deleted_at=deleted_at,
               field_key=key or ("f%d" % i), display_name=name,
               sheet=sheet, deleted_by=deleted_by)


def task(key, project_id=1, deleted_at=None, name=None, test_id="T1",
         status="passed", deleted_by=None):
    return Row(task_key=key, project_id=project_id, deleted_at=deleted_at,
               task_name=name, test_id=test_id, status=status,
               deleted_by=deleted_by)


class User:
    def __init__(self, i=7):
        self.id = i


class Project:
    def __init__(self, i=1):
        self.id = i


# --------------------------------------------------------------------------- #
# Retention arithmetic
# --------------------------------------------------------------------------- #
class TestRetentionMath(unittest.TestCase):
    def test_expires_thirty_days_after_deletion(self):
        self.assertEqual(ts.expires_at(aware(2025, 1, 1)),
                         aware(2025, 1, 31))

    def test_expires_none_for_live_row(self):
        self.assertIsNone(ts.expires_at(None))

    def test_days_left_counts_down(self):
        self.assertEqual(
            ts.days_left(aware(2025, 1, 1), now=aware(2025, 1, 11)), 20)

    def test_partial_day_rounds_up(self):
        """Six hours left must read 1, not 0.

        A bin that says 0 while the entry is still restorable reads as "too
        late" and stops the user from trying.
        """
        self.assertEqual(
            ts.days_left(aware(2025, 1, 1), now=aware(2025, 1, 30, 18)), 1)

    def test_never_negative(self):
        self.assertEqual(
            ts.days_left(aware(2025, 1, 1), now=aware(2026, 1, 1)), 0)

    def test_exactly_expired_is_zero(self):
        self.assertEqual(
            ts.days_left(aware(2025, 1, 1), now=aware(2025, 1, 31)), 0)

    def test_naive_timestamp_does_not_explode(self):
        """Legacy rows come back without tzinfo; comparing would raise."""
        self.assertEqual(
            ts.days_left(dt.datetime(2025, 1, 1), now=aware(2025, 1, 11)), 20)

    def test_custom_retention_is_honoured(self):
        self.assertEqual(
            ts.days_left(aware(2025, 1, 1), retention_days=7,
                         now=aware(2025, 1, 3)), 5)

    def test_retention_is_thirty_days(self):
        self.assertEqual(ts.RETENTION_DAYS, 30)


class TestClampLimit(unittest.TestCase):
    def test_default_on_garbage(self):
        for raw in (None, "", "abc", "1e3", [], {}):
            self.assertEqual(ts.clamp_limit(raw), ts.DEFAULT_LIMIT)

    def test_zero_and_negative_fall_back(self):
        self.assertEqual(ts.clamp_limit(0), ts.DEFAULT_LIMIT)
        self.assertEqual(ts.clamp_limit(-5), ts.DEFAULT_LIMIT)

    def test_caps_at_max(self):
        self.assertEqual(ts.clamp_limit(10 ** 9), ts.MAX_LIMIT)

    def test_passes_through_sane_value(self):
        self.assertEqual(ts.clamp_limit("25"), 25)


# --------------------------------------------------------------------------- #
# Listing
# --------------------------------------------------------------------------- #
class TestListing(TrashBase):
    def test_live_rows_are_not_listed(self):
        self.items[:] = [item(1), item(2, deleted_at=aware(2025, 1, 1))]
        self.install()
        got = ts.list_trash(1, now=aware(2025, 1, 2))
        self.assertEqual([e["id"] for e in got["entries"]], [2])

    def test_other_projects_are_not_listed(self):
        self.items[:] = [item(1, project_id=2, deleted_at=aware(2025, 1, 1)),
                         item(2, deleted_at=aware(2025, 1, 1))]
        self.install()
        got = ts.list_trash(1, now=aware(2025, 1, 2))
        self.assertEqual([e["id"] for e in got["entries"]], [2])

    def test_other_projects_fields_are_not_listed(self):
        self.fields[:] = [field(1, project_id=2, deleted_at=aware(2025, 1, 1)),
                          field(2, deleted_at=aware(2025, 1, 1))]
        self.install()
        got = ts.list_trash(1, kind="field", now=aware(2025, 1, 2))
        self.assertEqual([e["id"] for e in got["entries"]], [2])

    def test_other_projects_tasks_are_not_listed(self):
        self.tasks[:] = [task("T001", project_id=2,
                              deleted_at=aware(2025, 1, 1)),
                         task("T002", deleted_at=aware(2025, 1, 1))]
        self.install()
        got = ts.list_trash(1, kind="task", now=aware(2025, 1, 2))
        self.assertEqual([e["id"] for e in got["entries"]], ["T002"])

    def test_all_three_kinds_appear_together(self):
        self.items[:] = [item(1, deleted_at=aware(2025, 1, 1))]
        self.fields[:] = [field(9, deleted_at=aware(2025, 1, 2))]
        self.tasks[:] = [task("T001", deleted_at=aware(2025, 1, 3))]
        self.install()
        got = ts.list_trash(1, now=aware(2025, 1, 4))
        self.assertEqual([e["kind"] for e in got["entries"]],
                         ["task", "field", "item"])

    def test_newest_first_across_kinds(self):
        self.items[:] = [item(1, deleted_at=aware(2025, 1, 5))]
        self.fields[:] = [field(9, deleted_at=aware(2025, 1, 9))]
        self.tasks[:] = [task("T001", deleted_at=aware(2025, 1, 7))]
        self.install()
        got = ts.list_trash(1, now=aware(2025, 1, 10))
        self.assertEqual([e["kind"] for e in got["entries"]],
                         ["field", "task", "item"])

    def test_kind_filter_narrows(self):
        self.items[:] = [item(1, deleted_at=aware(2025, 1, 1))]
        self.fields[:] = [field(9, deleted_at=aware(2025, 1, 2))]
        self.install()
        got = ts.list_trash(1, kind="field", now=aware(2025, 1, 3))
        self.assertEqual([e["kind"] for e in got["entries"]], ["field"])

    def test_unknown_kind_is_rejected(self):
        with self.assertRaises(ServiceError) as cm:
            ts.list_trash(1, kind="wombat")
        self.assertEqual(cm.exception.code, "VALIDATION_ERROR")

    def test_total_counts_beyond_the_page(self):
        """Truncation must be announced.

        Showing 5 of 40 without saying so reads as "the other 35 are already
        gone for good".
        """
        self.items[:] = [item(i, deleted_at=aware(2025, 1, 1))
                         for i in range(1, 41)]
        self.install()
        got = ts.list_trash(1, limit=5, now=aware(2025, 1, 2))
        self.assertEqual(len(got["entries"]), 5)
        self.assertEqual(got["total"], 40)
        self.assertTrue(got["truncated"])

    def test_limit_goes_through_the_clamp(self):
        """A limit of 0 must fall back to the default, not serve an empty bin.

        Reaching the query with the raw value would show a user with a full
        recycle bin an empty one -- the same thing they would see if their data
        had actually been purged.
        """
        self.items[:] = [item(i, deleted_at=aware(2025, 1, 1))
                         for i in range(1, 4)]
        self.install()
        got = ts.list_trash(1, limit=0, now=aware(2025, 1, 2))
        self.assertEqual(len(got["entries"]), 3)

    def test_not_truncated_when_it_all_fits(self):
        self.items[:] = [item(1, deleted_at=aware(2025, 1, 1))]
        self.install()
        got = ts.list_trash(1, limit=5, now=aware(2025, 1, 2))
        self.assertFalse(got["truncated"])
        self.assertEqual(got["total"], 1)

    def test_limit_is_clamped(self):
        self.items[:] = [item(i, deleted_at=aware(2025, 1, 1))
                         for i in range(1, 5)]
        self.install()
        got = ts.list_trash(1, limit=10 ** 9, now=aware(2025, 1, 2))
        self.assertEqual(len(got["entries"]), 4)

    def test_entry_carries_days_left_and_expiry(self):
        self.items[:] = [item(1, deleted_at=aware(2025, 1, 1))]
        self.install()
        e = ts.list_trash(1, now=aware(2025, 1, 11))["entries"][0]
        self.assertEqual(e["days_left"], 20)
        self.assertEqual(e["expires_at"], "2025-01-31T00:00:00")

    def test_retention_days_is_reported(self):
        got = ts.list_trash(1, now=aware(2025, 1, 2))
        self.assertEqual(got["retention_days"], ts.RETENTION_DAYS)

    def test_item_title_falls_back_to_id(self):
        self.items[:] = [item(4, case_id="", deleted_at=aware(2025, 1, 1))]
        self.install()
        e = ts.list_trash(1, now=aware(2025, 1, 2))["entries"][0]
        self.assertEqual(e["title"], "#4")

    def test_field_title_falls_back_to_key(self):
        self.fields[:] = [field(4, key="prio", name=None,
                                deleted_at=aware(2025, 1, 1))]
        self.install()
        e = ts.list_trash(1, now=aware(2025, 1, 2))["entries"][0]
        self.assertEqual(e["title"], "prio")

    def test_field_subtitle_names_the_key(self):
        self.fields[:] = [field(4, key="prio", name="优先级",
                                deleted_at=aware(2025, 1, 1))]
        self.install()
        e = ts.list_trash(1, now=aware(2025, 1, 2))["entries"][0]
        self.assertEqual(e["title"], "优先级")
        self.assertIn("prio", e["subtitle"])

    def test_task_title_falls_back_to_key(self):
        self.tasks[:] = [task("T007", name=None,
                              deleted_at=aware(2025, 1, 1))]
        self.install()
        e = ts.list_trash(1, now=aware(2025, 1, 2))["entries"][0]
        self.assertEqual(e["title"], "T007")

    def test_kind_label_is_localised(self):
        self.fields[:] = [field(1, deleted_at=aware(2025, 1, 1))]
        self.install()
        e = ts.list_trash(1, now=aware(2025, 1, 2))["entries"][0]
        self.assertEqual(e["kind_label"], "字段")

    def test_deleted_at_is_iso(self):
        self.items[:] = [item(1, deleted_at=aware(2025, 1, 1))]
        self.install()
        e = ts.list_trash(1, now=aware(2025, 1, 2))["entries"][0]
        self.assertEqual(e["deleted_at"], "2025-01-01T00:00:00")

    def test_timestamps_carry_no_offset_suffix(self):
        """The shared JS ``stamp`` helper only strips a trailing ``Z``.

        Emitting ``+00:00`` here would leave the offset dangling in the table
        while every other screen shows a bare timestamp -- so the column would
        look broken rather than more precise.
        """
        self.items[:] = [item(1, deleted_at=aware(2025, 1, 1))]
        self.install()
        e = ts.list_trash(1, now=aware(2025, 1, 2))["entries"][0]
        for key in ("deleted_at", "expires_at"):
            self.assertNotIn("+", e[key], key)
            self.assertFalse(e[key].endswith("Z"), key)

    def test_deleted_by_is_resolved_to_a_name(self):
        """"删除人 3" does not tell you whose mistake this was."""
        self.fields[:] = [field(9, deleted_at=aware(2025, 1, 1),
                                deleted_by=3)]
        self.users[:] = [Row(id=3, username="zwang", display_name="王志")]
        self.install()
        e = ts.list_trash(1, now=aware(2025, 1, 2))["entries"][0]
        self.assertEqual(e["deleted_by_name"], "王志")

    def test_deleted_by_falls_back_to_the_username(self):
        self.fields[:] = [field(9, deleted_at=aware(2025, 1, 1),
                                deleted_by=3)]
        self.users[:] = [Row(id=3, username="zwang", display_name=None)]
        self.install()
        e = ts.list_trash(1, now=aware(2025, 1, 2))["entries"][0]
        self.assertEqual(e["deleted_by_name"], "zwang")

    def test_a_deleted_row_names_its_deleter_too(self):
        """Rows are the most-deleted kind, so this is the case that matters."""
        self.items[:] = [item(4, deleted_at=aware(2025, 1, 1), deleted_by=3)]
        self.users[:] = [Row(id=3, username="zwang", display_name="王志")]
        self.install()
        e = ts.list_trash(1, now=aware(2025, 1, 2))["entries"][0]
        self.assertEqual(e["kind"], "item")
        self.assertEqual(e["deleted_by"], 3)
        self.assertEqual(e["deleted_by_name"], "王志")

    def test_every_entry_carries_the_name_key(self):
        """Absent and null render alike but are not alike to a caller."""
        self.items[:] = [item(4, deleted_at=aware(2025, 1, 1))]
        self.install()
        e = ts.list_trash(1, now=aware(2025, 1, 2))["entries"][0]
        self.assertIn("deleted_by_name", e)
        self.assertIsNone(e["deleted_by_name"])

    def test_a_departed_colleague_does_not_break_the_listing(self):
        self.fields[:] = [field(9, deleted_at=aware(2025, 1, 1),
                                deleted_by=404)]
        self.install()
        e = ts.list_trash(1, now=aware(2025, 1, 2))["entries"][0]
        self.assertIsNone(e["deleted_by_name"])
        self.assertEqual(e["deleted_by"], 404)

    def test_names_are_looked_up_once_for_the_page(self):
        """N+1 on a 100-entry bin is 100 round trips for a cosmetic column."""
        calls = []
        self.fields[:] = [field(i, deleted_at=aware(2025, 1, 1), deleted_by=3)
                          for i in range(1, 21)]
        self.users[:] = [Row(id=3, username="zwang", display_name="王志")]
        self.install()

        real = ts.LMUser.query.filter

        def counting(*preds):
            calls.append(1)
            return real(*preds)
        ts.LMUser.query.filter = counting
        ts.list_trash(1, now=aware(2025, 1, 2))
        self.assertEqual(len(calls), 1)

    def test_count_trash_spans_all_kinds(self):
        """Deliberately asymmetric per kind.

        With one live and one deleted row of each kind the total is the same
        whichever way round the filter reads, so the badge could be counting
        precisely the wrong rows and still look right.
        """
        self.items[:] = [item(1, deleted_at=aware(2025, 1, 1)),
                         item(2, deleted_at=aware(2025, 1, 1)),
                         item(3), item(4), item(5)]
        self.fields[:] = [field(9, deleted_at=aware(2025, 1, 1))]
        self.tasks[:] = [task("T1", deleted_at=aware(2025, 1, 1)),
                         task("T2")]
        self.install()
        self.assertEqual(ts.count_trash(1), 4)

    def test_count_trash_ignores_live_rows(self):
        self.items[:] = [item(1), item(2), item(3)]
        self.install()
        self.assertEqual(ts.count_trash(1), 0)

    def test_count_trash_is_project_scoped(self):
        self.items[:] = [item(1, project_id=2, deleted_at=aware(2025, 1, 1))]
        self.install()
        self.assertEqual(ts.count_trash(1), 0)


# --------------------------------------------------------------------------- #
# Restore
# --------------------------------------------------------------------------- #
class TestRestore(TrashBase):
    def test_restore_item(self):
        row = item(1, deleted_at=aware(2025, 1, 1))
        self.items[:] = [row]
        self.install()
        out = ts.restore(User(), Project(), "item", 1)
        self.assertIsNone(row.deleted_at)
        self.assertEqual(out["kind"], "item")

    def test_restore_field_goes_through_field_service(self):
        f = field(3, name="优先级", deleted_at=aware(2025, 1, 1))
        self.fields[:] = [f]
        self.install()
        out = ts.restore(User(), Project(), "field", 3)
        self.assertIsNone(f.deleted_at)
        self.assertEqual(out["title"], "优先级")

    def test_restore_task(self):
        t = task("T001", deleted_at=aware(2025, 1, 1))
        self.tasks[:] = [t]
        self.install()
        out = ts.restore(User(), Project(), "task", "T001")
        self.assertIsNone(t.deleted_at)
        self.assertEqual(out["id"], "T001")

    def test_restoring_a_task_is_audited(self):
        self.tasks[:] = [task("T001", deleted_at=aware(2025, 1, 1))]
        self.install()
        ts.restore(User(), Project(), "task", "T001")
        self.assertIn("task.restore", [a for a, _ in self.audit_calls])

    def test_restore_rejects_unknown_kind(self):
        with self.assertRaises(ServiceError) as cm:
            ts.restore(User(), Project(), "wombat", 1)
        self.assertEqual(cm.exception.code, "VALIDATION_ERROR")

    def test_restore_missing_field_is_not_found(self):
        with self.assertRaises(ServiceError) as cm:
            ts.restore(User(), Project(), "field", 99)
        self.assertEqual(cm.exception.code, "NOT_FOUND")

    def test_restore_live_field_is_not_found(self):
        """A field that was never deleted is not in the bin."""
        self.fields[:] = [field(3)]
        self.install()
        with self.assertRaises(ServiceError) as cm:
            ts.restore(User(), Project(), "field", 3)
        self.assertEqual(cm.exception.code, "NOT_FOUND")

    def test_restore_task_from_another_project_is_not_found(self):
        self.tasks[:] = [task("T001", project_id=2,
                              deleted_at=aware(2025, 1, 1))]
        self.install()
        with self.assertRaises(ServiceError) as cm:
            ts.restore(User(), Project(1), "task", "T001")
        self.assertEqual(cm.exception.code, "NOT_FOUND")

    def test_restore_field_from_another_project_is_not_found(self):
        self.fields[:] = [field(3, project_id=2,
                                deleted_at=aware(2025, 1, 1))]
        self.install()
        with self.assertRaises(ServiceError) as cm:
            ts.restore(User(), Project(1), "field", 3)
        self.assertEqual(cm.exception.code, "NOT_FOUND")


# --------------------------------------------------------------------------- #
# Purge
# --------------------------------------------------------------------------- #
class TestPurge(TrashBase):
    def test_purge_item_deletes_the_row(self):
        row = item(1, deleted_at=aware(2025, 1, 1))
        self.items[:] = [row]
        self.install()
        ts.purge(User(), Project(), "item", 1)
        self.assertIn(row, self.session.deleted)

    def test_purge_item_is_audited(self):
        self.items[:] = [item(1, deleted_at=aware(2025, 1, 1))]
        self.install()
        ts.purge(User(), Project(), "item", 1)
        self.assertIn("item.purge", [a for a, _ in self.audit_calls])

    def test_purge_field_goes_through_field_service(self):
        """Only that path drops the values out of custom_values."""
        f = field(3, deleted_at=aware(2025, 1, 1))
        self.fields[:] = [f]
        self.install()
        ts.purge(User(), Project(), "field", 3)
        self.assertEqual([x[0] for x in self.purged_fields], [f])

    def test_purge_task_goes_through_task_service(self):
        t = task("T001", deleted_at=aware(2025, 1, 1))
        self.tasks[:] = [t]
        self.install()
        ts.purge(User(), Project(), "task", "T001")
        self.assertEqual([x[0].task_key for x in self.purged_tasks], ["T001"])
        self.assertIs(t, self.tasks[0])

    def test_purging_a_task_is_audited_before_it_vanishes(self):
        """The audit entry has to be written while the row still has a key.

        Audit *after* purge and the log records object_id=None -- the one record
        of a permanent deletion, with the identity stripped out of it.
        """
        self.tasks[:] = [task("T001", deleted_at=aware(2025, 1, 1))]
        self.install()
        ts.purge(User(), Project(), "task", "T001")
        recorded = [kw for a, kw in self.audit_calls if a == "task.purge"]
        self.assertEqual(len(recorded), 1)
        self.assertEqual(recorded[0]["object_id"], "T001")

    def test_purge_refuses_a_live_item(self):
        self.items[:] = [item(1)]
        self.install()
        with self.assertRaises(ServiceError) as cm:
            ts.purge(User(), Project(), "item", 1)
        self.assertEqual(cm.exception.code, "NOT_FOUND")
        self.assertEqual(self.session.deleted, [])

    def test_purge_refuses_a_live_task(self):
        self.tasks[:] = [task("T001")]
        self.install()
        with self.assertRaises(ServiceError):
            ts.purge(User(), Project(), "task", "T001")
        self.assertEqual(self.purged_tasks, [])

    def test_purge_is_project_scoped(self):
        self.items[:] = [item(1, project_id=2, deleted_at=aware(2025, 1, 1))]
        self.install()
        with self.assertRaises(ServiceError):
            ts.purge(User(), Project(1), "item", 1)

    def test_purge_rejects_unknown_kind(self):
        with self.assertRaises(ServiceError) as cm:
            ts.purge(User(), Project(), "wombat", 1)
        self.assertEqual(cm.exception.code, "VALIDATION_ERROR")


# --------------------------------------------------------------------------- #
# Retention sweep
# --------------------------------------------------------------------------- #
class TestPurgeExpired(TrashBase):
    def test_expired_item_is_removed(self):
        row = item(1, deleted_at=aware(2025, 1, 1))
        self.items[:] = [row]
        self.install()
        counts = ts.purge_expired(now=aware(2025, 3, 1))
        self.assertEqual(counts["items"], 1)
        self.assertIn(row, self.session.deleted)

    def test_row_inside_retention_survives(self):
        """The sweep must not run ahead of the promise on screen."""
        row = item(1, deleted_at=aware(2025, 1, 1))
        self.items[:] = [row]
        self.install()
        counts = ts.purge_expired(now=aware(2025, 1, 20))
        self.assertEqual(counts["items"], 0)
        self.assertEqual(self.session.deleted, [])

    def test_boundary_day_is_not_purged_early(self):
        """Exactly 30 days old is the last moment it is still restorable."""
        self.items[:] = [item(1, deleted_at=aware(2025, 1, 1))]
        self.install()
        self.assertEqual(ts.purge_expired(now=aware(2025, 1, 31))["items"], 0)

    def test_one_second_past_the_boundary_is_purged(self):
        self.items[:] = [item(1, deleted_at=aware(2025, 1, 1))]
        self.install()
        self.assertEqual(
            ts.purge_expired(now=aware(2025, 1, 31, 0, 0, 1))["items"], 1)

    def test_live_rows_are_never_swept(self):
        """A row with no deleted_at must survive any cutoff."""
        self.items[:] = [item(1)]
        self.tasks[:] = [task("T1")]
        self.fields[:] = [field(1)]
        self.install()
        counts = ts.purge_expired(now=aware(2099, 1, 1))
        self.assertEqual(counts, {"items": 0, "fields": 0, "tasks": 0})
        self.assertEqual(self.session.deleted, [])

    def test_expired_field_goes_through_purge_field(self):
        f = field(3, deleted_at=aware(2025, 1, 1))
        self.fields[:] = [f]
        self.install()
        counts = ts.purge_expired(now=aware(2025, 3, 1))
        self.assertEqual(counts["fields"], 1)
        self.assertEqual([x[0] for x in self.purged_fields], [f])

    def test_sweep_defers_the_commit(self):
        """One commit for the whole sweep, not one per row."""
        self.fields[:] = [field(3, deleted_at=aware(2025, 1, 1))]
        self.tasks[:] = [task("T1", deleted_at=aware(2025, 1, 1))]
        self.install()
        ts.purge_expired(now=aware(2025, 3, 1))
        self.assertEqual([x[1] for x in self.purged_fields], [False])
        self.assertEqual([x[1] for x in self.purged_tasks], [False])
        self.assertEqual(self.session.commits, 1)

    def test_expired_task_goes_through_purge_task(self):
        t = task("T1", deleted_at=aware(2025, 1, 1))
        self.tasks[:] = [t]
        self.install()
        counts = ts.purge_expired(now=aware(2025, 3, 1))
        self.assertEqual(counts["tasks"], 1)
        self.assertEqual([x[0].task_key for x in self.purged_tasks], ["T1"])

    def test_sweep_can_be_scoped_to_one_project(self):
        self.items[:] = [item(1, project_id=1, deleted_at=aware(2025, 1, 1)),
                         item(2, project_id=2, deleted_at=aware(2025, 1, 1))]
        self.install()
        counts = ts.purge_expired(now=aware(2025, 3, 1), project_id=2)
        self.assertEqual(counts["items"], 1)
        self.assertEqual([r.id for r in self.session.deleted], [2])

    def test_custom_retention_shifts_the_cutoff(self):
        self.items[:] = [item(1, deleted_at=aware(2025, 1, 1))]
        self.install()
        self.assertEqual(
            ts.purge_expired(now=aware(2025, 1, 10),
                             retention_days=7)["items"], 1)

    def test_a_sweep_that_removed_nothing_is_not_audited(self):
        counts = ts.purge_expired(now=aware(2025, 3, 1))
        self.assertEqual(counts, {"items": 0, "fields": 0, "tasks": 0})
        self.assertEqual(self.audit_calls, [])

    def test_a_sweep_that_removed_something_is_audited(self):
        self.items[:] = [item(1, deleted_at=aware(2025, 1, 1))]
        self.install()
        ts.purge_expired(now=aware(2025, 3, 1))
        self.assertEqual([a for a, _ in self.audit_calls],
                         ["trash.purge_expired"])

    def test_sweep_is_idempotent(self):
        """Driven by deleted_at, not a cursor, so a second run finds nothing."""
        self.items[:] = [item(1, deleted_at=aware(2025, 1, 1))]
        self.install()
        ts.purge_expired(now=aware(2025, 3, 1))
        self.items[:] = []
        self.install()
        self.assertEqual(ts.purge_expired(now=aware(2025, 3, 1))["items"], 0)

    def test_sweep_handles_naive_stored_rows(self):
        """Postgres hands ``deleted_at`` back without tzinfo."""
        self.items[:] = [item(1, deleted_at=dt.datetime(2025, 1, 1))]
        self.install()
        counts = ts.purge_expired(now=aware(2025, 3, 1))
        self.assertEqual(counts["items"], 1)

    def test_cutoff_is_naive_utc(self):
        """The cutoff must match the column's type.

        ``deleted_at`` is TIMESTAMP WITHOUT TIME ZONE. Handing Postgres an
        *aware* cutoff makes it cast one side using the session TimeZone, so the
        retention boundary slides by the server's UTC offset -- on a UTC+8 host
        that is eight hours of "restorable" quietly cut short.
        """
        seen = []

        class Spy(_Col):
            def __lt__(self, other):
                seen.append(other)
                return _Col.__lt__(self, other)

        ts.TestItemRow.deleted_at = Spy("deleted_at")
        ts.purge_expired(now=aware(2025, 3, 1))
        self.assertTrue(seen, "cutoff comparison never ran")
        self.assertIsNone(seen[0].tzinfo)
        self.assertEqual(seen[0], dt.datetime(2025, 1, 30))

    def test_naive_utc_keeps_the_wall_clock(self):
        self.assertEqual(ts.naive_utc(aware(2025, 1, 1, 6)),
                         dt.datetime(2025, 1, 1, 6))
        self.assertIsNone(ts.naive_utc(None))


if __name__ == "__main__":
    unittest.main()
