from datetime import datetime, timezone

from alembic import command
from alembic.migration import MigrationContext
from sqlalchemy import inspect, text

from app.extensions import db
from app.migrations.runner import HEAD_REVISION, get_alembic_config, run_migrations
from app.models import CurveView, LMUser, Project, ProjectMember, Task, TestBook as SilverBook


def test_saved_curve_views_are_scoped_to_the_book_of_the_task(client, app_ctx, tmp_path):
    with app_ctx.app_context():
        owner = LMUser(username="book-curve-owner", display_name="Owner", password_hash="x")
        db.session.add(owner)
        db.session.flush()
        project = Project(code="BOOKVIEWS", name="Books", owner_id=owner.id, project_type="silver_books")
        foreign = Project(code="FOREIGNVIEWS", name="Foreign", owner_id=owner.id, project_type="silver_books")
        db.session.add_all([project, foreign])
        db.session.flush()
        db.session.add(ProjectMember(project_id=project.id, user_id=owner.id, role="project_admin"))
        first_book = SilverBook(project_id=project.id, name="First")
        second_book = SilverBook(project_id=project.id, name="Second")
        other_book = SilverBook(project_id=foreign.id, name="Foreign")
        db.session.add_all([first_book, second_book, other_book])
        db.session.flush()
        tasks = []
        for book, task_key, signal in [
            (first_book, "T000010", "first_signal"),
            (second_book, "T000011", "second_signal"),
        ]:
            workspace = tmp_path / task_key
            results = workspace / "log" / "001"
            results.mkdir(parents=True)
            (results / "output.csv").write_text(f'"time","{signal}"\n0,0\n1,1\n')
            tasks.append(Task(task_key=task_key, project_id=project.id, test_id="001",
                              book_id=book.id, workspace=str(workspace), status="passed"))
        tasks.extend([
            Task(task_key="T000012", project_id=project.id, test_id="first-only", book_id=first_book.id),
            Task(task_key="T000013", project_id=project.id, test_id="deleted", book_id=first_book.id,
                 deleted_at=datetime.now(timezone.utc)),
        ])
        legacy = CurveView(project_id=project.id, test_id="001", name="Shared name",
                           layout={"lanes": [{"name": "ambiguous"}]}, created_by=owner.id)
        db.session.add_all([*tasks, legacy])
        db.session.commit()
        project_id, owner_id, legacy_id = project.id, owner.id, legacy.id
        first_id, second_id, foreign_id = first_book.id, second_book.id, other_book.id

    with client.session_transaction() as session:
        session["lm_user_id"] = owner_id
        session["csrf_token"] = "t"
    headers = {"X-CSRF-Token": "t"}
    base = f"/api/v1/projects/{project_id}"
    url = f"{base}/curve-views"

    def create(book_id, test_id="001", signal="first_signal", name="Shared name"):
        return client.post(url, headers=headers, json={
            "book_id": book_id, "test_id": test_id, "name": name,
            "layout": {"lanes": [{"name": signal}]},
        })

    def list_book(book_id, test_id="001"):
        return client.get(url, query_string={"book_id": book_id, "test_id": test_id})

    assert client.get(url, query_string={"test_id": "001"}).status_code == 400
    assert list_book("invalid").status_code == 400
    assert create(None).status_code == 400
    assert create(True).status_code == 400
    for book_id, test_id in [(foreign_id, "001"), (second_id, "first-only"), (first_id, "deleted")]:
        assert list_book(book_id, test_id).status_code == 404
        assert create(book_id, test_id).status_code == 404

    for book_id, task_key, signal in [
        (first_id, "T000010", "first_signal"),
        (second_id, "T000011", "second_signal"),
    ]:
        waveform = client.get(f"{base}/tasks/{task_key}/curves")
        assert waveform.status_code == 200
        context = waveform.get_json()["data"]
        assert context["task"]["book_id"] == book_id
        assert [column["name"] for column in context["columns"]] == [signal]
        assert list_book(book_id).get_json()["data"]["views"] == []
        assert create(book_id, signal=signal).status_code == 201
        assert create(book_id, signal=signal).status_code == 409

    first_view, = list_book(first_id).get_json()["data"]["views"]
    second_view, = list_book(second_id).get_json()["data"]["views"]
    assert first_view["id"] != second_view["id"]
    assert first_view["layout"]["lanes"] == [{"name": "first_signal"}]
    assert second_view["layout"]["lanes"] == [{"name": "second_signal"}]
    assert create(first_id, name="Existing").status_code == 201
    assert client.put(f"{url}/{first_view['id']}", headers=headers,
                      json={"name": "Existing"}).status_code == 409
    assert client.put(f"{url}/{second_view['id']}", headers=headers,
                      json={"name": "Existing"}).status_code == 200
    assert client.delete(f"{url}/{second_view['id']}", headers=headers).status_code == 200
    assert list_book(second_id).get_json()["data"]["views"] == []
    assert len(list_book(first_id).get_json()["data"]["views"]) == 2
    assert client.put(f"{url}/{legacy_id}", headers=headers, json={}).status_code == 404
    assert client.delete(f"{url}/{legacy_id}", headers=headers).status_code == 404
    with app_ctx.app_context():
        assert db.session.get(CurveView, legacy_id).book_id is None


def test_curve_view_migration_preserves_legacy_rows_and_uses_transaction(app_ctx):
    with app_ctx.app_context():
        project = Project(code="CURVEMIGRATION", name="Migration", project_type="silver_books")
        db.session.add(project)
        db.session.commit()
        project_id = project.id
        db.session.remove()
        with db.engine.begin() as connection:
            assert connection.dialect.name == "postgresql"
            command.downgrade(get_alembic_config(connection), "0008_test_books")
            connection.execute(text(
                "INSERT INTO curve_views (project_id, test_id, name, layout, created_at, updated_at) "
                "VALUES (:project_id, '001', 'Legacy', '{\"lanes\": [{\"name\": \"old\"}]}', now(), now())"
            ), {"project_id": project_id})

        with db.engine.connect() as connection:
            transaction = connection.begin()
            assert run_migrations(connection) == HEAD_REVISION
            assert "book_id" in {column["name"] for column in inspect(connection).get_columns("curve_views")}
            transaction.rollback()
            assert MigrationContext.configure(connection).get_current_revision() == "0008_test_books"
            assert "book_id" not in {column["name"] for column in inspect(connection).get_columns("curve_views")}

        assert run_migrations(db.engine) == HEAD_REVISION
        assert run_migrations(db.engine) == HEAD_REVISION
        view = CurveView.query.one()
        assert view.book_id is None and view.name == "Legacy"
        assert view.layout == {"lanes": [{"name": "old"}]}
