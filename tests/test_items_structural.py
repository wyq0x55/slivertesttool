"""Service and route tests for arbitrary-anchor row moves and multi-row inserts."""
from __future__ import annotations

import unittest
from flask import g, session, url_for
from sqlalchemy.pool import StaticPool

from app import create_app
from app.config import Config
from app.extensions import db
from app.models import (
    FieldDefinition, LMUser, Project, ProjectMember, TestItemRow as ItemRow,
)
from app.services.lanmatrix import audit, fields as fld, fields_service, service
from app.services.lanmatrix.service import ServiceError


class _StructuralTestConfig(Config):
    TESTING = True
    SECRET_KEY = "test-secret"
    WTF_CSRF_ENABLED = False
    SQLALCHEMY_DATABASE_URI = "sqlite://"
    SQLALCHEMY_ENGINE_OPTIONS = {
        "connect_args": {"check_same_thread": False},
        "poolclass": StaticPool,
    }


def _make_test_app():
    return create_app(_StructuralTestConfig)


class TestItemsStructural(unittest.TestCase):
    def setUp(self):
        self.app = _make_test_app()
        self.ctx = self.app.app_context()
        self.ctx.push()
        db.create_all()

        self.user = LMUser(
            username="tester",
            display_name="Tester",
            password_hash="x",
            is_system_admin=True,
        )
        db.session.add(self.user)
        db.session.flush()

        self.project = Project(
            code="PRJ_STRUCT",
            name="Structural Test Project",
            status="active",
            created_by=self.user.id,
        )
        db.session.add(self.project)
        db.session.flush()

        member = ProjectMember(
            project_id=self.project.id,
            user_id=self.user.id,
            role="admin",
        )
        db.session.add(member)
        db.session.flush()

        if hasattr(fields_service, "seed_default_fields"):
            fields_service.seed_default_fields(self.project.id)
        else:
            for attr in ("DEFAULT_FIELDS", "TEST_FIELDS", "SEED_FIELDS", "BUILTIN_FIELDS"):
                if hasattr(fld, attr):
                    fields_service.ensure_fields(
                        self.user, self.project, getattr(fld, attr))
                    break
        fields_service.ensure_fields(self.user, self.project, fld.CONST_FIELDS)
        db.session.commit()

        self.client = self.app.test_client()
        with self.client.session_transaction() as sess:
            sess["lm_user_id"] = self.user.id
            sess["user_id"] = self.user.id
            sess["csrf_token"] = "test-csrf-token"
            sess["lm_csrf_token"] = "test-csrf-token"
        self._headers = {"X-CSRF-Token": "test-csrf-token"}

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.ctx.pop()

    def _items_url(self) -> str:
        with self.app.test_request_context():
            return url_for("lanmatrix_projects.create_item",
                           project_id=self.project.id)

    def _move_url(self) -> str:
        with self.app.test_request_context():
            return url_for("lanmatrix_projects.move_items",
                           project_id=self.project.id)

    def _ordered_rows(self, sheet: str | None = None) -> list[ItemRow]:
        q = ItemRow.query.filter(
            ItemRow.project_id == self.project.id,
            ItemRow.deleted_at.is_(None),
        )
        if sheet is not None:
            q = q.filter(ItemRow.sheet == sheet)
        return q.order_by(ItemRow.row_order.asc()).all()

    def _audit_actions(self, action: str) -> list:
        from app.models import AuditLog
        return (
            AuditLog.query.filter_by(project_id=self.project.id, action=action)
            .order_by(AuditLog.id.asc())
            .all()
        )

    def _post(self, url: str, payload: dict):
        return self.client.post(url, json=payload, headers=self._headers)

    # ------------------------------------------------------------------ #
    # Multi-row positional insert tests
    # ------------------------------------------------------------------ #
    def test_single_row_post_backwards_compatible_payload(self):
        resp = self._post(self._items_url(), {
            "values": {"test_name": "First"},
            "draft": True,
        })
        self.assertEqual(resp.status_code, 201, resp.get_data(as_text=True))
        payload = resp.get_json()
        data = payload.get("data", payload)
        self.assertEqual(data["created"], 1)
        self.assertIn("item", data)
        self.assertEqual(len(data["items"]), 1)
        self.assertEqual(data["item"]["id"], data["items"][0]["id"])
        self.assertEqual(data["item"]["test_name"], "First")

    def test_multi_row_append_without_anchor_and_bad_anchor_validation(self):
        resp = self._post(self._items_url(), {
            "test_name": "BatchDraft",
            "count": 4,
            "draft": True,
        })
        self.assertEqual(resp.status_code, 201, resp.get_data(as_text=True))
        data = resp.get_json().get("data", resp.get_json())
        self.assertEqual(data["created"], 4)
        self.assertEqual(len(data["items"]), 4)
        self.assertEqual([r.row_order for r in self._ordered_rows("test")],
                         [1, 2, 3, 4])
        self.assertEqual(len({it["case_id"] for it in data["items"]}), 4)

        for bad_anchor in ("abc", True):
            bad_resp = self._post(self._items_url(), {
                "values": {},
                "count": 2,
                "draft": True,
                "anchor_id": bad_anchor,
            })
            self.assertEqual(bad_resp.status_code, 400)

    def test_multi_row_positional_insert_below_and_above_with_inheritance(self):
        r1 = service.create_item(
            self.user, self.project,
            {"title": "R1", "category": "CAT-A", "category_name": "Group A"},
            draft=True,
        )
        r2 = service.create_item(
            self.user, self.project,
            {"title": "R2", "category": "CAT-B", "category_name": "Group B"},
            draft=True,
        )

        # Insert 3 draft rows below r1 via HTTP route
        resp = self._post(self._items_url(), {
            "values": {},
            "count": 3,
            "draft": True,
            "anchor_id": r1.id,
            "place": "below",
        })
        self.assertEqual(resp.status_code, 201, resp.get_data(as_text=True))
        data = resp.get_json().get("data", resp.get_json())
        self.assertEqual(data["created"], 3)
        self.assertEqual(len(data["items"]), 3)
        self.assertEqual(data["item"]["id"], data["items"][0]["id"])

        below_ids = [it["id"] for it in data["items"]]
        below_case_ids = [it["case_id"] for it in data["items"]]
        self.assertEqual(len(set(below_case_ids)), 3, "each row gets a unique case_id")
        for it in data["items"]:
            self.assertEqual(it.get("category"), "CAT-A")
            self.assertEqual(it.get("category_name"), "Group A")

        rows = self._ordered_rows("test")
        self.assertEqual([r.id for r in rows], [r1.id, *below_ids, r2.id])
        self.assertEqual([r.row_order for r in rows], [1, 2, 3, 4, 5])

        # Insert 2 draft rows above r2 via HTTP route; r2's category is inherited
        resp2 = self._post(self._items_url(), {
            "values": {},
            "count": 2,
            "draft": True,
            "anchor_id": r2.id,
            "place": "above",
        })
        self.assertEqual(resp2.status_code, 201, resp2.get_data(as_text=True))
        data2 = resp2.get_json().get("data", resp2.get_json())
        above_ids = [it["id"] for it in data2["items"]]
        for it in data2["items"]:
            self.assertEqual(it.get("category"), "CAT-B")
            self.assertEqual(it.get("category_name"), "Group B")

        rows = self._ordered_rows("test")
        self.assertEqual([r.id for r in rows],
                         [r1.id, *below_ids, *above_ids, r2.id])
        self.assertEqual([r.row_order for r in rows], [1, 2, 3, 4, 5, 6, 7])

        all_case_ids = [r.case_id for r in rows]
        self.assertEqual(len(all_case_ids), len(set(all_case_ids)))
        self.assertEqual(len(self._audit_actions("item.create")), 7)

    def test_create_items_explicit_category_overrides_anchor(self):
        anchor = service.create_item(
            self.user, self.project,
            {"title": "Anchor", "category": "CAT-ANCHOR", "category_name": "Anchor Name"},
            draft=True,
        )
        created = service.create_items(
            self.user, self.project,
            {"category": "CAT-EXPLICIT"},
            count=2,
            draft=True,
            anchor_id=anchor.id,
            place="below",
        )
        self.assertEqual(len(created), 2)
        for item in created:
            self.assertEqual(item.get_field("category"), "CAT-EXPLICIT")
            self.assertEqual(item.get_field("category_name"), "Anchor Name")

    def test_create_items_count_validation(self):
        for bad in (0, -2, 201, "abc", True, 2.5):
            with self.assertRaises(ServiceError) as ctx:
                service.create_items(self.user, self.project, {}, count=bad, draft=True)
            self.assertEqual(ctx.exception.code, "VALIDATION_ERROR")

            resp = self._post(self._items_url(), {
                "values": {},
                "count": bad,
                "draft": True,
            })
            self.assertEqual(resp.status_code, 400, f"bad count={bad!r} must return 400")

    # ------------------------------------------------------------------ #
    # Arbitrary-anchor row move tests
    # ------------------------------------------------------------------ #
    def test_move_items_arbitrary_anchor_up_down_noop_and_errors(self):
        t1 = service.create_item(self.user, self.project, {"title": "T1"}, draft=True)
        c1 = service.create_item(
            self.user, self.project, {"const_name": "C1"},
            draft=True, sheet="const",
        )
        t2 = service.create_item(self.user, self.project, {"title": "T2"}, draft=True)
        t3 = service.create_item(self.user, self.project, {"title": "T3"}, draft=True)
        t4 = service.create_item(self.user, self.project, {"title": "T4"}, draft=True)
        t5 = service.create_item(self.user, self.project, {"title": "T5"}, draft=True)

        # Downward arbitrary-span block move: [t1, t2] after t4
        resp = self._post(self._move_url(), {
            "ids": [t1.id, t2.id],
            "target_id": t4.id,
            "place": "after",
        })
        self.assertEqual(resp.status_code, 200, resp.get_data(as_text=True))
        data = resp.get_json().get("data", resp.get_json())
        self.assertEqual(data["moved"], 2)
        self.assertEqual([r.id for r in self._ordered_rows("test")],
                         [t3.id, t4.id, t1.id, t2.id, t5.id])
        db.session.refresh(c1)
        self.assertEqual(c1.row_order, 2, "foreign-sheet row keeps its slot")

        reorders = self._audit_actions("item.reorder")
        self.assertEqual(len(reorders), 1)
        self.assertEqual(reorders[-1].new_value, {
            "moved": sorted([t1.id, t2.id]),
            "target_id": t4.id,
            "place": "after",
        })

        # Upward non-contiguous move with place="above" normalized to "before"
        resp2 = self._post(self._move_url(), {
            "ids": [t5.id, t1.id],
            "target_id": t3.id,
            "place": "above",
        })
        self.assertEqual(resp2.status_code, 200, resp2.get_data(as_text=True))
        self.assertEqual([r.id for r in self._ordered_rows("test")],
                         [t1.id, t5.id, t3.id, t4.id, t2.id])
        reorders = self._audit_actions("item.reorder")
        self.assertEqual(len(reorders), 2)
        self.assertEqual(reorders[-1].new_value["place"], "before")

        # Self-drop no-op writes no new audit record
        resp_noop = self._post(self._move_url(), {
            "ids": [t1.id, t5.id],
            "target_id": t5.id,
            "place": "before",
        })
        self.assertEqual(resp_noop.status_code, 200)
        self.assertEqual(len(self._audit_actions("item.reorder")), 2)

        # Already-in-position no-op writes no new audit record
        resp_noop2 = self._post(self._move_url(), {
            "ids": [t5.id],
            "target_id": t1.id,
            "place": "after",
        })
        self.assertEqual(resp_noop2.status_code, 200)
        self.assertEqual(len(self._audit_actions("item.reorder")), 2)

        # Cross-sheet target rejected with 400 VALIDATION_ERROR
        resp_cross = self._post(self._move_url(), {
            "ids": [t1.id],
            "target_id": c1.id,
            "place": "before",
        })
        self.assertEqual(resp_cross.status_code, 400)

        # Missing target_id rejected with 404 NOT_FOUND
        resp_missing = self._post(self._move_url(), {
            "ids": [t1.id],
            "target_id": 999999,
            "place": "before",
        })
        self.assertEqual(resp_missing.status_code, 404)

        # Bad target_id and bad place rejected with 400 VALIDATION_ERROR
        for bad_target in ("not_int", True):
            resp_bad_t = self._post(self._move_url(), {
                "ids": [t1.id],
                "target_id": bad_target,
                "place": "before",
            })
            self.assertEqual(resp_bad_t.status_code, 400)

        resp_bad_p = self._post(self._move_url(), {
            "ids": [t1.id],
            "target_id": t3.id,
            "place": "diagonal",
        })
        self.assertEqual(resp_bad_p.status_code, 400)

        # Backwards-compatible direction="down" when target_id is omitted
        resp_dir = self._post(self._move_url(), {
            "ids": [t1.id],
            "direction": "down",
        })
        self.assertEqual(resp_dir.status_code, 200)
        self.assertEqual([r.id for r in self._ordered_rows("test")],
                         [t5.id, t1.id, t3.id, t4.id, t2.id])
        self.assertEqual(self._audit_actions("item.reorder")[-1].new_value, {
            "moved": [t1.id],
            "direction": "down",
        })

    def test_all_jinja_templates_compile_and_lanmatrix_pages_render(self):
        templates = sorted(self.app.jinja_env.list_templates())
        self.assertGreater(len(templates), 0)
        for name in templates:
            self.app.jinja_env.get_template(name)

        resp_editor = self.client.get(f"/lanmatrix/projects/{self.project.id}")
        self.assertEqual(resp_editor.status_code, 200)
        html_editor = resp_editor.get_data(as_text=True)
        self.assertIn('id="lm-toolbar-right"', html_editor)
        self.assertIn('id="lm-run-selected"', html_editor)
        # AI 生成手顺 / 默认信号集 / 车规洁净室 live on the 执行配置 settings page now.
        self.assertNotIn('id="lm-ai-generate"', html_editor)
        self.assertNotIn('id="lm-topbar-ai"', html_editor)
        self.assertIn("vendor/univer/esm/univer-core.js", html_editor)

        resp_home = self.client.get("/lanmatrix/home")
        self.assertEqual(resp_home.status_code, 200)
        self.assertIn('data-esm="1"', resp_home.get_data(as_text=True))

        resp_tasks = self.client.get(f"/lanmatrix/projects/{self.project.id}/tasks")
        self.assertEqual(resp_tasks.status_code, 200)
        self.assertIn('data-esm="1"', resp_tasks.get_data(as_text=True))

        ut_project = Project(
            code="PRJ_UT_RENDER",
            name="UT Render Project",
            project_type="unit_test",
            status="active",
            created_by=self.user.id,
        )
        db.session.add(ut_project)
        outsider = LMUser(
            username="outsider",
            display_name="Outsider",
            password_hash="x",
            is_system_admin=False,
        )
        db.session.add(outsider)
        db.session.commit()

        resp_ut_tasks = self.client.get(f"/lanmatrix/projects/{ut_project.id}/tasks")
        self.assertEqual(resp_ut_tasks.status_code, 200)
        self.assertIn('data-esm="1"', resp_ut_tasks.get_data(as_text=True))

        outsider_client = self.app.test_client()
        with outsider_client.session_transaction() as sess:
            sess["lm_user_id"] = outsider.id
        g.pop("_page_user", None)
        resp_denied = outsider_client.get(f"/lanmatrix/projects/{self.project.id}")
        self.assertEqual(resp_denied.status_code, 200)
        denied_html = resp_denied.get_data(as_text=True)
        self.assertIn("无法访问项目", denied_html)
        self.assertNotIn('id="lm-run-selected"', denied_html)


if __name__ == "__main__":
    unittest.main()
