"""Execution-server path browsing for the project binding pickers."""

from __future__ import annotations

import os

import pytest

from app.services.lanmatrix.errors import ServiceError
from app.services.ut_pipeline import server_paths


@pytest.fixture()
def tree(tmp_path):
    (tmp_path / "Soft").mkdir()
    (tmp_path / "beta").mkdir()
    (tmp_path / "index.CSV").write_text("x", encoding="utf-8")
    (tmp_path / "notes.txt").write_text("x", encoding="utf-8")
    return tmp_path


def names(listing):
    return [(entry["name"], entry["type"]) for entry in listing["entries"]]


def test_directory_listing_holds_only_directories_sorted(tree):
    listing = server_paths.browse(str(tree), "directory")
    assert listing["path"] == str(tree)
    assert listing["parent"] == str(tree.parent)
    assert names(listing) == [("beta", "directory"), ("Soft", "directory")]
    assert listing["entries"][1]["path"] == str(tree / "Soft")


def test_csv_listing_adds_csv_files_after_directories(tree):
    assert names(server_paths.browse(str(tree), "csv")) == [
        ("beta", "directory"), ("Soft", "directory"), ("index.CSV", "file")]


def test_bound_file_opens_its_directory_with_the_file_selected(tree):
    listing = server_paths.browse(str(tree / "index.CSV"), "csv")
    assert listing["path"] == str(tree)
    assert listing["selected"] == str(tree / "index.CSV")


def test_missing_path_opens_nearest_existing_ancestor(tree):
    listing = server_paths.browse(str(tree / "Soft" / "gone" / "deeper"), "directory")
    assert listing["path"] == str(tree / "Soft")
    assert listing["selected"] is None


def test_empty_path_lists_file_system_roots():
    listing = server_paths.browse("", "directory")
    assert listing["path"] == "" and listing["parent"] is None
    assert listing["entries"] and all(os.path.isdir(entry["path"]) for entry in listing["entries"])


def test_root_parent_is_the_root_list():
    root = os.path.abspath(os.sep)
    assert server_paths.browse(root, "directory")["parent"] == ""


@pytest.mark.parametrize("path, kind", [("relative/dir", "directory"), ("", "everything")])
def test_rejects_relative_paths_and_unknown_kinds(path, kind):
    with pytest.raises(ServiceError) as raised:
        server_paths.browse(path, kind)
    assert raised.value.code == "VALIDATION_ERROR"


@pytest.fixture()
def api(app_ctx):
    from app.extensions import db
    from app.models import LMUser, ProjectMember
    from app.services.lanmatrix import projects_service

    with app_ctx.app_context():
        admin = LMUser.query.filter_by(is_system_admin=True).first()
        project = projects_service.create_project(
            admin, code="UTPATH", name="UT 路径", project_type="unit_test")
        reader = LMUser(username="ut-path-reader", password_hash="unused", status="active")
        db.session.add(reader)
        db.session.flush()
        db.session.add(ProjectMember(project_id=project.id, user_id=reader.id, role="reader"))
        db.session.commit()
        users = {"admin": admin.id, "reader": reader.id}
        pid = project.id

    def call(user, **query):
        client = app_ctx.test_client()
        with client.session_transaction() as sess:
            sess["lm_user_id"] = users[user]
        resp = client.get(f"/api/v1/projects/{pid}/ut/server-paths", query_string=query)
        return resp.status_code, resp.get_json()

    return call


def test_editor_browses_server_paths(api, tree):
    status, body = api("admin", kind="csv", path=str(tree))
    assert status == 200, body
    assert [entry["name"] for entry in body["data"]["entries"]] == ["beta", "Soft", "index.CSV"]


def test_reader_cannot_browse_server_paths(api, tree):
    status, body = api("reader", path=str(tree))
    assert status == 403
    assert body["error"]["code"] == "PERMISSION_DENIED"


def test_bad_kind_and_relative_path_are_client_errors(api):
    assert api("admin", kind="all")[0] == 400
    assert api("admin", path="relative")[0] == 400
