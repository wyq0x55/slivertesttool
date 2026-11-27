"""Silver multi-book projects: per-book identity, IO isolation and export."""

from __future__ import annotations

import io
import zipfile

import pytest
from openpyxl import load_workbook


def _steps(purpose: str) -> dict:
    return {
        "input_signals": [],
        "expected_signals": [],
        "steps": [{
            "no": 1,
            "purpose": purpose,
            "operation": "CALL",
            "subroutine": "",
            "args": "",
            "inputs": [],
            "expecteds": [],
            "timing": "",
        }],
    }


def _item(test_id: str, purpose: str, category: int = 1) -> dict:
    return {
        "test_id": test_id,
        "category": category,
        "sheet_name": "SHEET",
        "viewpoint": purpose,
        "test_no": 1,
        "item_id": f"ITEM-{test_id}-001",
        "purpose": purpose,
        "steps": _steps(purpose),
    }


def _book_bytes(items: list[dict]) -> bytes:
    from app.services.lanmatrix import book_excel

    wb = book_excel.build_workbook(items)
    wb.create_sheet("Cover", 0)
    wb.create_sheet("1.Introduction", 1)
    wb.create_sheet("2.Strategy", 2)
    wb.create_sheet("3.Overview", 3)
    wb.create_sheet("5.History")
    wb["Cover"]["A1"] = "PRESERVED COVER"
    wb["5.History"]["A1"] = "PRESERVED HISTORY"
    out = io.BytesIO()
    wb.save(out)
    return out.getvalue()


def _setup(app, tmp_path):
    from app.extensions import db
    from app.models import LMUser, Project, ProjectMember, ProjectModel

    model_file = tmp_path / "plant.sil"
    model_file.write_text("# mock model\n", encoding="utf-8")
    user = LMUser(username="books-editor", display_name="Books Editor",
                  password_hash="x")
    db.session.add(user)
    db.session.commit()
    project = Project(
        code="BOOKS-TEST", name="Books", project_type="silver_books",
        owner_id=user.id, created_by=user.id,
    )
    db.session.add(project)
    db.session.commit()
    db.session.add(ProjectMember(
        project_id=project.id, user_id=user.id, role="project_admin"))
    model = ProjectModel(
        project_id=project.id, name="plant", version="v1",
        kind="path", sil_path=str(model_file), is_current=True,
    )
    db.session.add(model)
    db.session.commit()
    return user, project, model


def _import_book(user, project, model, *, filename: str, book_name: str,
                 items: list[dict], book_id=None):
    from app.services.lanmatrix import import_job_service

    job = import_job_service.create_special_preview(
        user, project, io.BytesIO(_book_bytes(items)),
        import_format="book", mode="upsert",
        original_filename=filename, book_name=book_name,
        model_id=model.id if book_id is None else None,
        book_id=book_id,
    )
    return import_job_service.commit_special_import(user, project, job)


def test_books_isolate_ids_io_and_export_original_sheets(app_ctx, tmp_path):
    from app.extensions import db
    from app.models import TestBook, TestItemRow
    from app.services.lanmatrix import (
        books_service, io_excel, libconst_bridge,
    )

    with app_ctx.app_context():
        user, project, model = _setup(app_ctx, tmp_path)
        first = _import_book(
            user, project, model,
            filename="Qualification_Test_OPEINHIB.xlsx",
            book_name="OPEINHIB",
            items=[_item("QTS-001", "standard-1"),
                   _item("QTS-002", "standard-2", category=2)],
        )
        second = _import_book(
            user, project, model,
            filename="Qualification_Test_OPEINHIB_STD.xlsx",
            book_name="OPEINHIB_STD",
            items=[_item("QTS-001", "std-1"),
                   _item("QTS-002", "std-2", category=2)],
        )
        assert first["book_id"] != second["book_id"]

        books = {book.name: book for book in TestBook.query.filter_by(
            project_id=project.id).all()}
        assert set(books) == {"OPEINHIB", "OPEINHIB_STD"}
        for name, book in books.items():
            rows = TestItemRow.query.filter_by(
                project_id=project.id, book_id=book.id, sheet="test",
                deleted_at=None,
            ).order_by(TestItemRow.case_id).all()
            assert [row.case_id for row in rows] == ["QTS-001", "QTS-002"]
            assert len({row.uuid for row in rows}) == 2
            assert books_service.book_source_path(project, book).is_file()

        # IO is per book; Lib/Const stay project-wide.
        for book, signal in ((books["OPEINHIB"], "SIG-A"),
                             (books["OPEINHIB_STD"], "SIG-B")):
            db.session.add(TestItemRow(
                project_id=project.id, book_id=book.id, sheet="io",
                case_id=signal, custom_values={
                    "io_name": signal, "io_path": f"/{signal.lower()}",
                }))
        db.session.add(TestItemRow(
            project_id=project.id, book_id=None, sheet="lib",
            case_id="INIT", custom_values={"lib_func": "Init"}))
        db.session.commit()

        parsed_a = io_excel.parse_workbook(
            libconst_bridge.export_io(project, books["OPEINHIB"].id))
        parsed_b = io_excel.parse_workbook(
            libconst_bridge.export_io(project, books["OPEINHIB_STD"].id))
        assert [row["io_name"] for row in parsed_a["items"]] == ["SIG-A"]
        assert [row["io_name"] for row in parsed_b["items"]] == ["SIG-B"]

        # Re-importing one book merges by ID and leaves the other book alone.
        _import_book(
            user, project, model,
            filename="Qualification_Test_OPEINHIB.xlsx",
            book_name="OPEINHIB",
            items=[_item("QTS-001", "standard-1-updated")],
            book_id=books["OPEINHIB"].id,
        )
        first_rows = TestItemRow.query.filter_by(
            project_id=project.id, book_id=books["OPEINHIB"].id,
            sheet="test", deleted_at=None,
        ).all()
        second_rows = TestItemRow.query.filter_by(
            project_id=project.id, book_id=books["OPEINHIB_STD"].id,
            sheet="test", deleted_at=None,
        ).all()
        assert len(first_rows) == 2
        assert len(second_rows) == 2
        assert {row.case_id for row in first_rows} == {"QTS-001", "QTS-002"}

        exported = books_service.export_book(project, books["OPEINHIB"])
        wb = load_workbook(io.BytesIO(exported.getvalue()), data_only=True)
        assert wb.sheetnames[:5] == [
            "Cover", "1.Introduction", "2.Strategy", "3.Overview",
            "4.TestRequirement",
        ]
        assert wb.sheetnames[-1] == "5.History"
        assert wb["Cover"]["A1"].value == "PRESERVED COVER"
        assert wb["5.History"]["A1"].value == "PRESERVED HISTORY"
        assert wb["4.TestRequirement"].max_row == 3

        from app.services.lanmatrix import book_excel

        reparsed = book_excel.parse_workbook(
            io.BytesIO(exported.getvalue()),
            source_filename="Qualification_Test_OPEINHIB.xlsx")
        purposes = {item["test_id"]: item["purpose"]
                    for item in reparsed["items"]}
        assert purposes["QTS-001"] == "standard-1-updated"
        assert purposes["QTS-002"] == "standard-2"

        archive = books_service.export_all_books(project)
        with zipfile.ZipFile(archive) as bundle:
            assert sorted(bundle.namelist()) == [
                "Qualification_Test_OPEINHIB.xlsx",
                "Qualification_Test_OPEINHIB_STD.xlsx",
            ]
            std = load_workbook(
                io.BytesIO(bundle.read("Qualification_Test_OPEINHIB_STD.xlsx")),
                data_only=True)
            assert std["Cover"]["A1"].value == "PRESERVED COVER"


def test_requeue_uses_row_uuid_not_shared_test_id(app_ctx, tmp_path):
    from app.extensions import db
    from app.models import Task, TaskStatus
    from app.services import task_service

    with app_ctx.app_context():
        user, project, _model = _setup(app_ctx, tmp_path)
        running = Task(
            task_key="T000001", task_name="QTS-001", file_name="x",
            submitter=user.username, test_id="QTS-001",
            sil_relpath=str(tmp_path / "a.sil"), sil_name="plant",
            sil_version="v1", status=TaskStatus.RUNNING.value,
            workspace=str(tmp_path), project_id=project.id,
            book_id=1, row_uuid="row-a",
        )
        retest = Task(
            task_key="T000002", task_name="QTS-001", file_name="x",
            submitter=user.username, test_id="QTS-001",
            sil_relpath=str(tmp_path / "a.sil"), sil_name="plant",
            sil_version="v1", status=TaskStatus.FAILED.value,
            workspace=str(tmp_path), project_id=project.id,
            book_id=2, row_uuid="row-b", run_count=1,
        )
        db.session.add_all([running, retest])
        db.session.commit()

        task_service.requeue_task(
            retest, sil_relpath=str(tmp_path / "a.sil"),
            workspace=str(tmp_path), sil_name="plant", sil_version="v1",
            commit=True,
        )
        db.session.refresh(retest)
        db.session.refresh(running)
        assert retest.status == TaskStatus.QUEUED.value
        assert running.status == TaskStatus.RUNNING.value


def test_dashboard_compare_and_history_are_book_scoped(app_ctx, tmp_path):
    from app.extensions import db
    from app.models import TestBook, TestItemRow, TestRunRecord
    from app.services.lanmatrix import dashboard_service

    with app_ctx.app_context():
        user, project, model = _setup(app_ctx, tmp_path)
        _import_book(
            user, project, model,
            filename="opeinhb.xlsx", book_name="OPEINHIB",
            items=[_item("QTS-001", "a")])
        _import_book(
            user, project, model,
            filename="opeinhb_std.xlsx", book_name="OPEINHIB_STD",
            items=[_item("QTS-001", "b")])
        books = {book.name: book for book in TestBook.query.filter_by(
            project_id=project.id).all()}
        rows = {
            book.name: TestItemRow.query.filter_by(
                project_id=project.id, book_id=book.id, sheet="test",
                deleted_at=None).one()
            for book in books.values()
        }
        for book_name, outcome in (
                ("OPEINHIB", "pass"), ("OPEINHIB_STD", "pass")):
            db.session.add(TestRunRecord(
                project_id=project.id, book_id=books[book_name].id,
                row_uuid=rows[book_name].uuid, test_id="QTS-001",
                verdict=outcome.upper(), outcome=outcome,
                model_name="plant", model_version="v1",
                executor_name="Editor", executed_on="2026-04-01",
            ))
        db.session.add(TestRunRecord(
            project_id=project.id, book_id=books["OPEINHIB"].id,
            row_uuid=rows["OPEINHIB"].uuid, test_id="QTS-001",
            verdict="PASS", outcome="pass",
            model_name="plant", model_version="v2",
            executor_name="Editor", executed_on="2026-04-02",
        ))
        db.session.add(TestRunRecord(
            project_id=project.id, book_id=books["OPEINHIB_STD"].id,
            row_uuid=rows["OPEINHIB_STD"].uuid, test_id="QTS-001",
            verdict="FAIL", outcome="fail",
            model_name="plant", model_version="v2",
            executor_name="Editor", executed_on="2026-04-02",
        ))
        db.session.commit()

        data = dashboard_service.compare_versions(
            project.id, "plant@v1", "plant@v2")
        assert data["total"] == 2
        by_book = {item["book_name"]: item for item in data["items"]}
        assert by_book["OPEINHIB"]["change"] == "unchanged"
        assert by_book["OPEINHIB_STD"]["change"] == "pass_to_fail"
        assert by_book["OPEINHIB"]["book_id"] == books["OPEINHIB"].id

        history = dashboard_service.test_run_history(
            project.id, "QTS-001", row_uuid=rows["OPEINHIB_STD"].uuid)
        assert history["total"] == 2
        assert {item["outcome"] for item in history["items"]} == {"pass", "fail"}

        scoped = dashboard_service.test_run_history(
            project.id, "QTS-001", book_id=books["OPEINHIB"].id)
        assert scoped["book_name"] == "OPEINHIB"
        assert scoped["total"] == 2
        assert {item["outcome"] for item in scoped["items"]} == {"pass"}


def test_io_extract_requires_a_book_for_multi_book_projects(app_ctx, tmp_path):
    with app_ctx.app_context():
        user, project, _model = _setup(app_ctx, tmp_path)
        client = app_ctx.test_client()
        with client.session_transaction() as sess:
            sess["lm_user_id"] = user.id
            sess["csrf_token"] = "books-csrf"
        response = client.post(
            f"/api/v1/projects/{project.id}/io/extract",
            json={"sheets": ["lib"]},
            headers={"X-CSRF-Token": "books-csrf"},
        )
        assert response.status_code == 400
        body = response.get_json()
        assert body["error"]["code"] == "VALIDATION_ERROR"


def test_delete_book_is_blocked_while_tasks_reference_it(app_ctx, tmp_path):
    from app.extensions import db
    from app.models import Task, TaskStatus
    from app.services.lanmatrix import books_service
    from app.services.lanmatrix.errors import ServiceError

    with app_ctx.app_context():
        user, project, model = _setup(app_ctx, tmp_path)
        book = books_service.create_book(
            user, project, name="EMPTY", source_filename="empty.xlsx",
            model_id=model.id, commit=True,
        )
        db.session.add(Task(
            task_key="T900001", task_name="QTS-009", file_name="x",
            submitter=user.username, test_id="QTS-009",
            sil_relpath=str(tmp_path / "a.sil"), sil_name="plant",
            sil_version="v1", status=TaskStatus.FAILED.value,
            workspace=str(tmp_path), project_id=project.id,
            book_id=book.id, row_uuid="row-empty",
        ))
        db.session.commit()

        with pytest.raises(ServiceError) as excinfo:
            books_service.delete_book(user, project, book, commit=False)
        assert excinfo.value.code == "BOOK_HAS_TASKS"


def test_soft_deleted_id_can_be_reimported_within_the_same_book(app_ctx, tmp_path):
    import datetime as _dt

    from app.extensions import db
    from app.models import TestBook, TestItemRow

    with app_ctx.app_context():
        user, project, model = _setup(app_ctx, tmp_path)
        _import_book(
            user, project, model, filename="book.xlsx", book_name="BOOK",
            items=[_item("QTS-001", "first")])
        book = TestBook.query.filter_by(project_id=project.id).one()
        row = TestItemRow.query.filter_by(
            project_id=project.id, book_id=book.id, case_id="QTS-001",
            deleted_at=None).one()
        row.deleted_at = _dt.datetime.now(_dt.timezone.utc)
        db.session.commit()

        _import_book(
            user, project, model, filename="book.xlsx", book_name="BOOK",
            items=[_item("QTS-001", "again")], book_id=book.id)
        live = TestItemRow.query.filter_by(
            project_id=project.id, book_id=book.id, case_id="QTS-001",
            deleted_at=None).all()
        assert len(live) == 1
        assert live[0].get_field("purpose") == "again"


def test_run_input_snapshot_scopes_by_exact_row_uuid(app_ctx, tmp_path):
    from app.extensions import db
    from app.models import TestItemRow
    from app.services.lanmatrix import books_service, run_input_service

    with app_ctx.app_context():
        user, project, model = _setup(app_ctx, tmp_path)
        first = books_service.create_book(
            user, project, name="FIRST", source_filename="first.xlsx",
            model_id=model.id, commit=True)
        second = books_service.create_book(
            user, project, name="SECOND", source_filename="second.xlsx",
            model_id=model.id, commit=True)
        rows = {}
        for book, prefix in ((first, "a"), (second, "b")):
            for test_id in ("QTS-001", "QTS-002"):
                row = TestItemRow(
                    project_id=project.id, book_id=book.id, sheet="test",
                    case_id=test_id)
                db.session.add(row)
                db.session.flush()
                rows[f"{prefix}-{test_id}"] = row
        db.session.commit()

        expected = {
            "tests": [
                {"uuid": rows["a-QTS-001"].uuid, "test_id": "QTS-001", "steps": ""},
                {"uuid": rows["b-QTS-002"].uuid, "test_id": "QTS-002", "steps": ""},
            ],
            "lib": [],
            "const": [],
        }
        # Exact UUID scope accepts a cross-book selection.
        run_input_service.assert_materialized(
            project.id, expected,
            row_uuids=[rows["a-QTS-001"].uuid, rows["b-QTS-002"].uuid])

        # The old id x book scope pulls in unselected rows in both books.
        with pytest.raises(run_input_service.RunInputsNotMaterialized):
            run_input_service.assert_materialized(
                project.id, expected,
                test_ids=["QTS-001", "QTS-002"],
                book_ids=[first.id, second.id])
