"""Audit field-level diff and CSV export (#12).

`old_value` / `new_value` are whole-object snapshots, so the log used to render
two blobs of truncated JSON side by side -- the reviewer could see that
*something* changed but not *what*. These cover the diff rules and the CSV
export that shares them.

Pure functions throughout, so they run without PostgreSQL.
"""
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

from app.services.lanmatrix import comments_service as cs  # noqa: E402


def by_field(changes):
    return {c["field"]: c for c in changes}


class TestDiffBasics(unittest.TestCase):
    def test_a_changed_field_is_reported_both_ways(self):
        c = by_field(cs.diff_values({"title": "a"}, {"title": "b"}))["title"]
        self.assertEqual((c["old"], c["new"], c["kind"]), ("a", "b", "changed"))

    def test_an_unchanged_field_is_not_reported(self):
        self.assertEqual(cs.diff_values({"title": "a"}, {"title": "a"}), [])

    def test_a_new_field_is_an_addition(self):
        c = by_field(cs.diff_values({}, {"owner": "z"}))["owner"]
        self.assertEqual((c["old"], c["new"], c["kind"]), ("", "z", "added"))

    def test_a_dropped_field_is_a_removal(self):
        c = by_field(cs.diff_values({"owner": "z"}, {}))["owner"]
        self.assertEqual((c["old"], c["new"], c["kind"]), ("z", "", "removed"))

    def test_a_field_cleared_to_empty_is_a_change_not_a_removal(self):
        # The key is still present; reporting it as "removed" would tell the
        # reviewer the column was deleted from the project.
        c = by_field(cs.diff_values({"owner": "z"}, {"owner": ""}))["owner"]
        self.assertEqual(c["kind"], "changed")

    def test_a_field_set_to_null_is_a_change_not_a_removal(self):
        c = by_field(cs.diff_values({"owner": "z"}, {"owner": None}))["owner"]
        self.assertEqual((c["kind"], c["new"]), ("changed", ""))

    def test_creates_list_every_field_as_added(self):
        out = cs.diff_values(None, {"title": "a", "status": "draft"})
        self.assertEqual({c["kind"] for c in out}, {"added"})
        self.assertEqual(len(out), 2)

    def test_deletes_list_every_field_as_removed(self):
        out = cs.diff_values({"title": "a", "status": "draft"}, None)
        self.assertEqual({c["kind"] for c in out}, {"removed"})

    def test_nothing_at_all_is_an_empty_diff(self):
        self.assertEqual(cs.diff_values(None, None), [])

    def test_fields_are_ordered_stably(self):
        a = cs.diff_values({}, {"b": 1, "a": 1, "c": 1})
        b = cs.diff_values({}, {"c": 1, "a": 1, "b": 1})
        self.assertEqual([c["field"] for c in a], [c["field"] for c in b],
                         "field order must not depend on dict insertion order")

    def test_every_row_has_the_keys_the_renderer_reads(self):
        for c in cs.diff_values({"a": 1}, {"a": 2, "b": 3}):
            self.assertEqual(set(c), {"field", "label", "old", "new", "kind"})


class TestNoise(unittest.TestCase):
    """Bookkeeping columns change on every write and explain nothing."""

    def test_version_bump_alone_produces_no_diff(self):
        self.assertEqual(
            cs.diff_values({"title": "a", "version": 1},
                           {"title": "a", "version": 2}), [])

    def test_timestamp_churn_alone_produces_no_diff(self):
        self.assertEqual(
            cs.diff_values({"updated_at": "1", "id": 5},
                           {"updated_at": "2", "id": 5}), [])

    def test_row_order_churn_is_hidden(self):
        self.assertEqual(cs.diff_values({"row_order": 1}, {"row_order": 9}), [])

    def test_real_changes_survive_alongside_noise(self):
        out = by_field(cs.diff_values(
            {"title": "a", "version": 1, "updated_at": "x"},
            {"title": "b", "version": 2, "updated_at": "y"}))
        self.assertEqual(list(out), ["title"])


class TestFormatting(unittest.TestCase):
    def test_none_renders_as_empty_not_the_word_none(self):
        self.assertEqual(cs.format_value(None), "")

    def test_booleans_render_in_chinese(self):
        self.assertEqual(cs.format_value(True), "是")
        self.assertEqual(cs.format_value(False), "否")

    def test_false_is_not_confused_with_empty(self):
        # `if not v` would render False as "" and lose the change entirely.
        self.assertNotEqual(cs.format_value(False), cs.format_value(None))

    def test_zero_is_not_confused_with_empty(self):
        self.assertEqual(cs.format_value(0), "0")

    def test_numbers_render_bare(self):
        self.assertEqual(cs.format_value(3), "3")

    def test_lists_render_readably(self):
        self.assertEqual(cs.format_value(["a", "b"]), "a、b")

    def test_dicts_render_with_stable_key_order(self):
        self.assertEqual(cs.format_value({"b": 2, "a": 1}), "a=1; b=2")

    def test_nested_values_are_not_left_as_python_repr(self):
        self.assertNotIn("'", cs.format_value({"a": ["x", "y"]}))

    def test_known_fields_get_a_chinese_label(self):
        self.assertEqual(cs.field_label("title"), "标题")

    def test_project_defined_fields_keep_their_own_key(self):
        # Inventing a translation for a user-named column would be worse than
        # showing the name the user chose.
        self.assertEqual(cs.field_label("自定义列"), "自定义列")
        self.assertEqual(cs.field_label("custom_x"), "custom_x")


class TestNonDictSnapshots(unittest.TestCase):
    def test_scalar_values_still_produce_a_row(self):
        out = cs.diff_values("old", "new")
        self.assertEqual(len(out), 1)
        self.assertEqual((out[0]["old"], out[0]["new"]), ("old", "new"))

    def test_a_list_snapshot_does_not_crash(self):
        self.assertEqual(len(cs.diff_values([1, 2], [1, 3])), 1)

    def test_a_dict_against_a_scalar_still_diffs_the_dict(self):
        out = by_field(cs.diff_values(None, {"a": 1}))
        self.assertIn("a", out)


class TestTruncation(unittest.TestCase):
    def test_a_huge_snapshot_is_capped(self):
        new = {"f%03d" % i: i for i in range(200)}
        out = cs.diff_values(None, new, limit=5)
        self.assertEqual(len(out), 6, "5 rows plus one truncation notice")

    def test_truncation_is_announced_not_silent(self):
        out = cs.diff_values(None, {"f%03d" % i: i for i in range(200)}, limit=5)
        self.assertEqual(out[-1]["kind"], "truncated")
        self.assertIn("195", out[-1]["new"])

    def test_a_normal_snapshot_is_not_truncated(self):
        out = cs.diff_values(None, {"a": 1, "b": 2})
        self.assertNotIn("truncated", {c["kind"] for c in out})


class TestCsvCell(unittest.TestCase):
    """Excel executes a leading = + - @ as a formula. The audit log records
    hostile input verbatim by design, so this is a live path, not theory."""

    def test_a_formula_is_disarmed(self):
        self.assertTrue(cs.csv_cell("=cmd|'/c calc'!A1").startswith("'="))

    def test_all_four_formula_leaders_are_disarmed(self):
        for lead in ("=", "+", "-", "@"):
            self.assertTrue(cs.csv_cell(lead + "x").startswith("'" + lead),
                            "%r must be neutralised" % lead)

    def test_leading_whitespace_does_not_smuggle_a_formula(self):
        # Excel strips tab/CR before parsing, so "\t=1+1" is still a formula.
        for pre in ("\t", "\r", "\n", " "):
            self.assertTrue(cs.csv_cell(pre + "=1+1").startswith("'"),
                            "%r-prefixed formula must be neutralised" % pre)

    def test_ordinary_text_is_untouched(self):
        self.assertEqual(cs.csv_cell("正常标题"), "正常标题")

    def test_a_negative_number_is_still_readable(self):
        # Quoted, but the value stays visible -- correctness beats prettiness
        # when the alternative is code execution.
        self.assertIn("-3", cs.csv_cell(-3))

    def test_none_becomes_empty(self):
        self.assertEqual(cs.csv_cell(None), "")

    def test_empty_string_is_not_mangled(self):
        self.assertEqual(cs.csv_cell(""), "")


class FakeLog:
    def __init__(self, i, **over):
        self._d = {"id": i, "created_at": "2024-05-06T07:08:09.123",
                   "action": "item.update", "result": "success",
                   "object_type": "item", "object_id": i, "actor_id": 1,
                   "client_ip": "10.0.0.1", "error_summary": "",
                   "old_value": {"title": "a"}, "new_value": {"title": "b"}}
        self._d.update(over)

    def to_dict(self):
        return dict(self._d)


class _Col:
    """Stands in for a mapped column so ``.desc()`` in order_by resolves."""

    def desc(self):
        return self


class FakeQuery:
    """Records the paging the generator asks for and serves a fixed corpus."""

    def __init__(self, rows, log):
        self._rows, self._log = rows, log
        self._off, self._lim = 0, None

    def filter(self, *a):
        return self

    def order_by(self, *a):
        return self

    def offset(self, n):
        self._off = n
        return self

    def limit(self, n):
        self._lim = n
        return self

    def all(self):
        self._log.append((self._off, self._lim))
        return self._rows[self._off:self._off + self._lim]


class TestCsvExport(unittest.TestCase):
    """Runs the generator for real against a stub query.

    Asserting on the source text instead would pass just as happily against a
    version that calls ``audit_criteria(project_id)`` and drops every filter.
    """

    def setUp(self):
        self.paging = []
        self.crit_calls = []
        # Swap the whole model reference: merely *reading* AuditLog.query goes
        # through Flask-SQLAlchemy and demands an application context.
        self._real_model = cs.AuditLog
        self._real_crit = cs.audit_criteria
        self._real_names = cs._attach_actor_names

        def fake_criteria(pid, **kw):
            self.crit_calls.append((pid, kw))
            return []

        cs.audit_criteria = fake_criteria
        cs._attach_actor_names = lambda items: None

    def tearDown(self):
        cs.AuditLog = self._real_model
        cs.audit_criteria = self._real_crit
        cs._attach_actor_names = self._real_names

    def install(self, rows):
        q = FakeQuery(rows, self.paging)

        class FakeModel:
            query = q
            created_at = _Col()
            id = _Col()

        cs.AuditLog = FakeModel
        return q

    def rows(self, n=3, **kw):
        self.install([FakeLog(i) for i in range(n)])
        return list(cs.audit_csv_rows(7, **kw))

    def test_the_header_comes_first_and_matches_the_row_width(self):
        out = self.rows(2)
        self.assertEqual(out[0], cs.AUDIT_CSV_HEADER)
        self.assertEqual(len(cs.AUDIT_CSV_HEADER), 11)
        for r in out[1:]:
            self.assertEqual(len(r), len(cs.AUDIT_CSV_HEADER))

    def test_the_active_filters_reach_the_query(self):
        self.rows(1, action="item.update", q="boom")
        self.assertEqual(len(self.crit_calls), 1)
        pid, kw = self.crit_calls[0]
        self.assertEqual(pid, 7)
        self.assertEqual(kw.get("action"), "item.update")
        self.assertEqual(kw.get("q"), "boom",
                         "the export must honour the filters on screen")

    def test_rows_are_fetched_in_batches(self):
        self.install([FakeLog(i) for i in range(1200)])
        list(cs.audit_csv_rows(7))
        limits = {lim for _off, lim in self.paging}
        self.assertTrue(limits and max(limits) <= 500,
                        "the whole log was materialised in one query: %r"
                        % (self.paging,))
        self.assertGreater(len(self.paging), 1, "no second batch was fetched")

    def test_one_row_per_changed_field(self):
        self.install([FakeLog(1, old_value={"title": "a", "owner": "x"},
                              new_value={"title": "b", "owner": "y"})])
        out = list(cs.audit_csv_rows(7))
        self.assertEqual(len(out), 3, "header plus one row per changed field")

    def test_an_entry_with_no_field_delta_still_appears(self):
        # Logins and run triggers have no diff; dropping them would make the
        # export disagree with the count shown on screen.
        self.install([FakeLog(1, action="user.login", old_value=None,
                              new_value=None)])
        out = list(cs.audit_csv_rows(7))
        self.assertEqual(len(out), 2)
        self.assertIn("user.login", out[1])

    def test_exported_values_are_sanitised(self):
        self.install([FakeLog(1, old_value={"title": "ok"},
                              new_value={"title": "=cmd|'/c calc'!A1"})])
        out = list(cs.audit_csv_rows(7))
        self.assertTrue(any(str(c).startswith("'=") for c in out[1]),
                        "a formula reached the file unescaped: %r" % (out[1],))

    def test_reaching_the_cap_is_announced_inside_the_file(self):
        self.install([FakeLog(i) for i in range(50)])
        out = list(cs.audit_csv_rows(7, max_rows=10))
        self.assertIn("导出已达到上限", "".join(str(c) for c in out[-1]),
                      "a truncated export must never look complete")

    def test_the_cap_actually_caps(self):
        self.install([FakeLog(i) for i in range(50)])
        out = list(cs.audit_csv_rows(7, max_rows=10))
        self.assertLessEqual(len(out), 12, "header + 10 rows + notice")

    def test_a_complete_export_carries_no_truncation_notice(self):
        out = self.rows(3)
        self.assertNotIn("导出已达到上限", "".join(str(c) for r in out for c in r))

    def test_the_cap_default_is_finite(self):
        self.assertGreater(cs.AUDIT_CSV_MAX_ROWS, 0)


class TestRouteContract(unittest.TestCase):
    def setUp(self):
        self.src = open(os.path.join(REPO,
                        "app/routes/lanmatrix/projects_items.py"),
                        encoding="utf-8").read()
        self.body = self.src[self.src.index("def audit_logs_csv"):
                             self.src.index("def audit_log_actions")]

    def test_the_export_requires_the_same_permission_as_the_view(self):
        self.assertIn('"audit.view"', self.body)

    def test_the_export_is_login_gated(self):
        head = self.src[self.src.index('audit-logs.csv'):
                        self.src.index("def audit_logs_csv")]
        self.assertIn("@login_required", head)

    def test_the_date_range_is_validated_like_the_table(self):
        self.assertIn("date_from 不能晚于 date_to", self.body)
        self.assertRegex(self.body, r"return err\(\s*\"VALIDATION_ERROR\"",
                         "the range must be rejected, not merely noticed")

    def test_the_json_route_validates_the_range_too(self):
        # Covered here because a reversed range that 400s on the CSV route but
        # silently returns everything on the JSON route is the worse of the two
        # failures: it is the one the reviewer actually reads on screen.
        body = self.src[self.src.index("def audit_logs("):
                        self.src.index("def audit_logs_csv")]
        self.assertIn("date_from 不能晚于 date_to", body)
        self.assertRegex(body, r"return err\(\s*\"VALIDATION_ERROR\"")

    def test_a_utf8_bom_is_written_for_excel(self):
        self.assertIn('"\\ufeff"', self.body,
                      "without a BOM Excel reads the file as GBK")

    def test_exporting_is_itself_audited(self):
        self.assertIn('audit.record("audit.export"', self.body,
                      "bulk extraction of the audit log must leave a trace")

    def test_the_response_is_streamed(self):
        self.assertIn("stream_with_context", self.body)

    def test_the_download_is_not_cached(self):
        self.assertIn("no-store", self.body)

    def test_crlf_line_endings_for_excel(self):
        self.assertIn('lineterminator="\\r\\n"', self.body)


if __name__ == "__main__":
    unittest.main()
