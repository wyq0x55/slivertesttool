"""Unit tests for notification delivery, collapsing and retention.

Runs against an in-memory SQLite database rather than the project's PostgreSQL,
because the behaviour under test (grouping window, self-suppression, unread
accounting, retention) is pure application logic and should stay verifiable on a
machine with no database server.
"""

from __future__ import annotations

import pytest
from datetime import timedelta

from app import create_app
from app.extensions import db
from app.models import LMUser, Notification
from app.services.lanmatrix import notification_service as ns


@pytest.fixture()
def app(tmp_path, monkeypatch):
    # Config is read at import time, so the module must be reloaded after the
    # environment is patched -- the same approach tests/conftest.py uses.
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path/'n.db'}")
    monkeypatch.setenv("SECRET_KEY", "test")
    monkeypatch.setenv("INSTANCE_DIR", str(tmp_path / "instance"))
    monkeypatch.setenv("START_WORKER", "0")
    monkeypatch.setenv("START_COLLAB", "0")

    import importlib
    import app as app_pkg
    import app.config as config_mod

    importlib.reload(config_mod)
    importlib.reload(app_pkg)

    application = app_pkg.create_app()
    with application.app_context():
        yield application
        db.session.remove()


@pytest.fixture()
def users(app):
    a = LMUser(username="alice", display_name="Alice", password_hash="x")
    b = LMUser(username="bob", display_name="Bob", password_hash="x")
    db.session.add_all([a, b])
    db.session.commit()
    return a, b


class TestDelivery:
    def test_basic_delivery(self, users):
        a, _ = users
        row = ns.notify(a.id, ns.TASK_FINISHED, "run done", commit=True)
        assert row is not None
        assert ns.unread_count(a.id) == 1

    def test_self_notification_is_suppressed(self, users):
        a, _ = users
        # Being told about the consequence of the click you just made is noise.
        assert ns.notify(a.id, ns.TASK_FINISHED, "mine", actor_id=a.id) is None
        assert ns.unread_count(a.id) == 0

    def test_notification_to_someone_else_is_delivered(self, users):
        a, b = users
        assert ns.notify(a.id, ns.REVIEW_ASSIGNED, "review", actor_id=b.id) is not None

    def test_missing_user_is_a_noop(self, app):
        assert ns.notify(None, ns.TASK_FINISHED, "x") is None
        assert ns.notify(0, ns.TASK_FINISHED, "x") is None

    def test_missing_type_is_a_noop(self, users):
        a, _ = users
        assert ns.notify(a.id, "", "x") is None

    def test_delivery_failure_never_raises(self, users):
        a, _ = users
        # A notification problem must not roll back the business transaction
        # that triggered it.
        assert ns.notify(a.id, ns.TASK_FINISHED, "t" * 5000) is not None

    def test_long_title_is_truncated_to_the_column(self, users):
        a, _ = users
        row = ns.notify(a.id, ns.TASK_FINISHED, "x" * 500, commit=True)
        assert len(row.title) <= 200

    def test_notify_many_deduplicates_recipients(self, users):
        a, b = users
        sent = ns.notify_many([a.id, b.id, a.id, None, 0], ns.REVIEW_ASSIGNED, "hi")
        db.session.commit()
        assert sent == 2


class TestNoCollapsingByDefault:
    """Shipping default: one event, one row, one link.

    Merging produced entries such as "待审核 ×2" that could only ever open one
    of the two cases -- the second was announced and then unreachable.
    """

    def test_window_defaults_to_zero(self, app):
        assert app.config["LM_NOTIFY_GROUP_SECONDS"] == 0

    def test_same_event_kind_twice_stays_two_rows(self, users):
        a, _ = users
        ns.notify(a.id, ns.REVIEW_ASSIGNED, "case A", project_id=1,
                  ref_type="test_item", ref_id="u1")
        ns.notify(a.id, ns.REVIEW_ASSIGNED, "case B", project_id=1,
                  ref_type="test_item", ref_id="u2")
        db.session.commit()
        rows = ns.list_for(a.id)
        assert [n.title for n in rows] == ["case B", "case A"]
        assert all(n.count == 1 for n in rows)
        assert ns.unread_count(a.id) == 2

    def test_default_group_key_is_per_referenced_object(self, app):
        assert ns._default_group_key(ns.REVIEW_ASSIGNED, 3, "u1") \
            != ns._default_group_key(ns.REVIEW_ASSIGNED, 3, "u2")
        # Without a ref the old project-scoped shape is kept.
        assert ns._default_group_key(ns.TASK_FINISHED, 3, "") == "task.finished:3"


class TestCollapsing:
    """Opt-in behaviour: only reachable with a non-zero window."""

    @pytest.fixture(autouse=True)
    def _window(self, app):
        app.config["LM_NOTIFY_GROUP_SECONDS"] = 60
        return app

    def test_same_group_collapses_into_one_row(self, users):
        a, _ = users
        for i in range(50):
            ns.notify(a.id, ns.TASK_FINISHED, f"run {i}", group_key="g1")
        db.session.commit()
        rows = ns.list_for(a.id)
        # 50 cases submitted at once must not bury everything else in the bell.
        assert len(rows) == 1
        assert rows[0].count == 50
        assert ns.unread_count(a.id) == 1

    def test_different_groups_stay_separate(self, users):
        a, _ = users
        ns.notify(a.id, ns.TASK_FINISHED, "run", group_key="g1")
        ns.notify(a.id, ns.REVIEW_ASSIGNED, "review", group_key="g2")
        db.session.commit()
        assert len(ns.list_for(a.id)) == 2

    def test_default_group_key_separates_by_type_and_project(self, users):
        a, _ = users
        ns.notify(a.id, ns.TASK_FINISHED, "p1", project_id=1)
        ns.notify(a.id, ns.TASK_FINISHED, "p2", project_id=2)
        db.session.commit()
        # A run finishing in another project is a separate piece of news.
        assert len(ns.list_for(a.id)) == 2

    def test_read_rows_are_not_collapsed_into(self, users):
        a, _ = users
        ns.notify(a.id, ns.TASK_FINISHED, "first", group_key="g1", commit=True)
        ns.mark_read(a.id)
        ns.notify(a.id, ns.TASK_FINISHED, "second", group_key="g1", commit=True)
        # Merging into an already-read row would make the new event invisible.
        assert ns.unread_count(a.id) == 1
        assert len(ns.list_for(a.id)) == 2

    def test_events_outside_the_window_do_not_collapse(self, users, app):
        a, _ = users
        first = ns.notify(a.id, ns.TASK_FINISHED, "old", group_key="g1", commit=True)
        window = app.config["LM_NOTIFY_GROUP_SECONDS"]
        first.created_at = first.created_at - timedelta(seconds=window + 60)
        db.session.commit()
        ns.notify(a.id, ns.TASK_FINISHED, "new", group_key="g1", commit=True)
        assert len(ns.list_for(a.id)) == 2

    def test_collapsing_disabled_when_window_is_zero(self, users, app):
        a, _ = users
        app.config["LM_NOTIFY_GROUP_SECONDS"] = 0
        for i in range(3):
            ns.notify(a.id, ns.TASK_FINISHED, f"r{i}", group_key="g1")
        db.session.commit()
        assert len(ns.list_for(a.id)) == 3

    def test_group_key_is_truncated_to_the_column(self, users):
        a, _ = users
        row = ns.notify(a.id, ns.TASK_FINISHED, "x", group_key="k" * 500,
                        commit=True)
        assert len(row.group_key) <= 120


class TestReads:
    def test_notifications_are_isolated_per_user(self, users):
        a, b = users
        ns.notify(a.id, ns.TASK_FINISHED, "for alice", commit=True)
        assert ns.unread_count(b.id) == 0
        assert ns.list_for(b.id) == []

    def test_mark_all_read(self, users):
        a, _ = users
        ns.notify(a.id, ns.TASK_FINISHED, "1", group_key="g1")
        ns.notify(a.id, ns.REVIEW_ASSIGNED, "2", group_key="g2")
        db.session.commit()
        assert ns.mark_read(a.id) == 2
        assert ns.unread_count(a.id) == 0

    def test_mark_specific_ids_only(self, users):
        a, _ = users
        one = ns.notify(a.id, ns.TASK_FINISHED, "1", group_key="g1", commit=True)
        ns.notify(a.id, ns.REVIEW_ASSIGNED, "2", group_key="g2", commit=True)
        assert ns.mark_read(a.id, [one.id]) == 1
        assert ns.unread_count(a.id) == 1

    def test_mark_read_ignores_other_users_ids(self, users):
        a, b = users
        theirs = ns.notify(b.id, ns.TASK_FINISHED, "bob", commit=True)
        # Passing someone else's id must not clear their notification.
        assert ns.mark_read(a.id, [theirs.id]) == 0
        assert ns.unread_count(b.id) == 1

    def test_mark_read_with_garbage_ids_is_a_noop(self, users):
        a, _ = users
        ns.notify(a.id, ns.TASK_FINISHED, "1", commit=True)
        assert ns.mark_read(a.id, ["abc", None, ""]) == 0
        assert ns.unread_count(a.id) == 1

    def test_only_unread_filter(self, users):
        a, _ = users
        one = ns.notify(a.id, ns.TASK_FINISHED, "1", group_key="g1", commit=True)
        ns.notify(a.id, ns.REVIEW_ASSIGNED, "2", group_key="g2", commit=True)
        ns.mark_read(a.id, [one.id])
        assert len(ns.list_for(a.id, only_unread=True)) == 1
        assert len(ns.list_for(a.id)) == 2

    def test_newest_first(self, users):
        a, _ = users
        for i in range(3):
            ns.notify(a.id, ns.TASK_FINISHED, f"n{i}", group_key=f"g{i}",
                      commit=True)
        assert ns.list_for(a.id)[0].title == "n2"

    def test_limit_is_capped(self, users):
        a, _ = users
        for i in range(5):
            ns.notify(a.id, ns.TASK_FINISHED, f"n{i}", group_key=f"g{i}")
        db.session.commit()
        assert len(ns.list_for(a.id, limit=2)) == 2
        # An absurd limit must not let one call drag the table into memory.
        assert len(ns.list_for(a.id, limit=100000)) == 5

    def test_to_dict_shape(self, users):
        a, _ = users
        row = ns.notify(a.id, ns.REVIEW_ASSIGNED, "t", body="b", project_id=7,
                        link_url="/x", ref_type="test_item", ref_id="u1",
                        commit=True)
        d = row.to_dict()
        assert d["type"] == ns.REVIEW_ASSIGNED
        assert d["project_id"] == 7
        assert d["link_url"] == "/x"
        assert d["ref_id"] == "u1"
        assert d["is_read"] is False
        assert d["count"] == 1


class TestRetention:
    def test_read_and_old_rows_are_purged(self, users):
        a, _ = users
        row = ns.notify(a.id, ns.TASK_FINISHED, "old", commit=True)
        row.created_at = row.created_at - timedelta(days=200)
        row.is_read = True
        db.session.commit()
        assert ns.purge_old(90) == 1

    def test_unread_rows_survive_regardless_of_age(self, users):
        a, _ = users
        row = ns.notify(a.id, ns.REVIEW_ASSIGNED, "ancient review", commit=True)
        row.created_at = row.created_at - timedelta(days=2000)
        db.session.commit()
        # An unread notification is outstanding work; deleting it by age is how
        # a review request gets silently lost.
        assert ns.purge_old(90) == 0
        assert ns.unread_count(a.id) == 1

    def test_recent_read_rows_survive(self, users):
        a, _ = users
        ns.notify(a.id, ns.TASK_FINISHED, "recent", commit=True)
        ns.mark_read(a.id)
        assert ns.purge_old(90) == 0

    def test_zero_retention_disables_purging(self, users):
        a, _ = users
        row = ns.notify(a.id, ns.TASK_FINISHED, "old", commit=True)
        row.created_at = row.created_at - timedelta(days=2000)
        row.is_read = True
        db.session.commit()
        assert ns.purge_old(0) == 0
        assert Notification.query.count() == 1


class TestScopesAndArchive:
    """The bell's two tabs, and filing an item away without deleting it."""

    def test_history_holds_read_rows_and_unread_excludes_them(self, users):
        a, _ = users
        ns.notify(a.id, ns.TASK_FINISHED, "n1", commit=True)
        ns.notify(a.id, ns.REVIEW_ASSIGNED, "n2", commit=True)
        ns.mark_read(a.id, [Notification.query.filter_by(title="n1").one().id])

        unread = ns.list_for(a.id, scope=ns.SCOPE_UNREAD)
        history = ns.list_for(a.id, scope=ns.SCOPE_HISTORY)
        assert [n.title for n in unread] == ["n2"]
        assert [n.title for n in history] == ["n1"]
        assert len(ns.list_for(a.id, scope=ns.SCOPE_ALL)) == 2

    def test_default_scope_is_all_for_existing_callers(self, users):
        a, _ = users
        ns.notify(a.id, ns.TASK_FINISHED, "n1", commit=True)
        ns.mark_read(a.id)
        # Adding a scope argument must not narrow what old call sites receive.
        assert len(ns.list_for(a.id)) == 1

    def test_mark_read_stamps_read_at(self, users):
        a, _ = users
        row = ns.notify(a.id, ns.TASK_FINISHED, "n1", commit=True)
        assert row.read_at is None
        ns.mark_read(a.id)
        assert Notification.query.get(row.id).read_at is not None

    def test_archive_moves_row_to_history_and_clears_badge(self, users):
        a, _ = users
        row = ns.notify(a.id, ns.TASK_FINISHED, "n1", commit=True)
        assert ns.unread_count(a.id) == 1
        assert ns.archive(a.id, [row.id]) == 1
        assert ns.unread_count(a.id) == 0
        assert [n.title for n in ns.list_for(a.id, scope=ns.SCOPE_HISTORY)] == ["n1"]

    def test_archived_rows_are_not_revived_by_collapsing(self, users):
        a, _ = users
        row = ns.notify(a.id, ns.TASK_FINISHED, "run done", ref_type="task",
                        ref_id="T1", commit=True)
        ns.archive(a.id, [row.id])
        # The same event again must create a NEW row rather than bumping the
        # count on the one the user explicitly filed away.
        again = ns.notify(a.id, ns.TASK_FINISHED, "run done", ref_type="task",
                          ref_id="T1", commit=True)
        assert again.id != row.id
        assert ns.unread_count(a.id) == 1

    def test_archive_without_ids_files_everything(self, users):
        a, _ = users
        ns.notify(a.id, ns.TASK_FINISHED, "n1", commit=True)
        ns.notify(a.id, ns.REVIEW_ASSIGNED, "n2", commit=True)
        assert ns.archive(a.id) == 2
        assert ns.unread_count(a.id) == 0

    def test_clear_history_never_deletes_unread(self, users):
        a, _ = users
        keep = ns.notify(a.id, ns.REVIEW_ASSIGNED, "pending", commit=True)
        gone = ns.notify(a.id, ns.TASK_FINISHED, "done", commit=True)
        ns.mark_read(a.id, [gone.id])
        assert ns.clear_history(a.id) == 1
        remaining = [n.title for n in ns.list_for(a.id, scope=ns.SCOPE_ALL)]
        assert remaining == ["pending"]
        assert Notification.query.get(keep.id) is not None

    def test_history_count_tracks_read_and_archived(self, users):
        a, _ = users
        r1 = ns.notify(a.id, ns.TASK_FINISHED, "n1", commit=True)
        r2 = ns.notify(a.id, ns.REVIEW_ASSIGNED, "n2", commit=True)
        ns.notify(a.id, ns.REVIEW_APPROVED, "n3", commit=True)
        assert ns.history_count(a.id) == 0
        ns.mark_read(a.id, [r1.id])
        ns.archive(a.id, [r2.id])
        assert ns.history_count(a.id) == 2


class TestRetentionAgesByReadAt:
    def test_old_row_read_today_survives(self, users):
        a, _ = users
        row = ns.notify(a.id, ns.TASK_FINISHED, "ancient", commit=True)
        row.created_at = row.created_at - timedelta(days=200)
        db.session.commit()
        ns.mark_read(a.id, [row.id])
        # Ageing by created_at would delete this the instant it was opened.
        assert ns.purge_old(30) == 0
        assert Notification.query.count() == 1

    def test_row_read_long_ago_is_purged(self, users):
        a, _ = users
        row = ns.notify(a.id, ns.TASK_FINISHED, "stale", commit=True)
        ns.mark_read(a.id, [row.id])
        row = Notification.query.get(row.id)
        row.read_at = row.read_at - timedelta(days=31)
        db.session.commit()
        assert ns.purge_old(30) == 1

    def test_archived_rows_age_out(self, users):
        a, _ = users
        row = ns.notify(a.id, ns.TASK_FINISHED, "filed", commit=True)
        ns.archive(a.id, [row.id])
        row = Notification.query.get(row.id)
        row.read_at = None
        row.archived_at = row.archived_at - timedelta(days=31)
        db.session.commit()
        assert ns.purge_old(30) == 1


class TestLinkBuilders:
    """Every link the bell emits must resolve under the /lanmatrix prefix."""

    def test_task_link_carries_prefix_and_deep_link(self, app):
        assert ns.task_link(4, "T-9") == "/lanmatrix/projects/4/tasks?task=T-9"

    def test_task_link_without_key(self, app):
        assert ns.task_link(4, "") == "/lanmatrix/projects/4/tasks"

    def test_task_link_escapes_the_key(self, app):
        assert "%2F" in ns.task_link(4, "a/b")

    def test_review_item_link_opens_the_case(self, app):
        assert ns.review_item_link(7, "u-1") \
            == "/lanmatrix/projects/7?row=u-1&from=workspace"

    def test_review_item_link_without_row_falls_back_to_the_project(self, app):
        assert ns.review_item_link(7, "") == "/lanmatrix/projects/7"
        assert ns.review_item_link(None, "u-1") == "/lanmatrix/home?view=reviews"

    def test_project_and_review_links(self, app):
        assert ns.project_link(7) == "/lanmatrix/projects/7"
        assert ns.review_queue_link(7) == "/lanmatrix/home?view=reviews&project_id=7"
        assert ns.review_queue_link(None) == "/lanmatrix/home?view=reviews"
