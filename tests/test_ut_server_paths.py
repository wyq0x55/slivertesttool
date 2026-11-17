"""Execution-server path browsing for the project binding pickers."""

from __future__ import annotations

import os
import subprocess

import pytest

from app.services.lanmatrix.errors import ServiceError
from app.services.lanmatrix.permissions import PermissionDenied
from app.services.ut_pipeline import server_paths


@pytest.fixture()
def tree(tmp_path):
    allowed = tmp_path / "allowed_root"
    allowed.mkdir()
    (allowed / "Soft").mkdir()
    (allowed / "beta").mkdir()
    (allowed / "index.CSV").write_text("x", encoding="utf-8")
    (allowed / "notes.txt").write_text("x", encoding="utf-8")
    (tmp_path / "outside_secret").mkdir()
    return allowed


def names(listing):
    return [(entry["name"], entry["type"]) for entry in listing["entries"]]


def test_directory_listing_holds_only_directories_sorted(tree):
    listing = server_paths.browse(str(tree), "directory", allowed_roots=[tree])
    assert listing["path"] == str(tree)
    assert listing["parent"] == ""
    assert names(listing) == [("beta", "directory"), ("Soft", "directory")]
    assert listing["entries"][1]["path"] == str(tree / "Soft")


def test_csv_listing_adds_csv_files_after_directories(tree):
    assert names(server_paths.browse(str(tree), "csv", allowed_roots=[tree])) == [
        ("beta", "directory"), ("Soft", "directory"), ("index.CSV", "file")]


def test_bound_file_opens_its_directory_with_the_file_selected(tree):
    listing = server_paths.browse(str(tree / "index.CSV"), "csv", allowed_roots=[tree])
    assert listing["path"] == str(tree)
    assert listing["selected"] == str(tree / "index.CSV")


def test_missing_path_opens_nearest_existing_ancestor(tree):
    listing = server_paths.browse(
        str(tree / "Soft" / "gone" / "deeper"), "directory", allowed_roots=[tree]
    )
    assert listing["path"] == str(tree / "Soft")
    assert listing["selected"] is None


def test_empty_path_lists_only_configured_allowed_roots(tree):
    listing = server_paths.browse("", "directory", allowed_roots=[tree])
    assert listing["path"] == "" and listing["parent"] is None
    assert listing["entries"] == [
        {"name": "allowed_root", "path": str(tree), "type": "directory"}
    ]


def test_out_of_bounds_and_drive_roots_are_rejected(tree):
    outside = tree.parent / "outside_secret"
    for forbidden in (str(outside), str(tree.parent), os.path.abspath(os.sep)):
        with pytest.raises(PermissionDenied):
            server_paths.browse(forbidden, "directory", allowed_roots=[tree])


def test_reparse_point_or_symlink_escape_is_blocked(tree):
    outside = tree.parent / "outside_secret"
    link = tree / "escape_link"
    created = False
    try:
        os.symlink(outside, link, target_is_directory=True)
        created = True
    except OSError:
        if os.name == "nt":
            proc = subprocess.run(
                ["cmd", "/c", "mklink", "/J", str(link), str(outside)],
                capture_output=True, text=True, check=False,
            )
            created = proc.returncode == 0
    if not created:
        pytest.skip("Platform did not permit symlink or junction creation")
    try:
        listing = server_paths.browse(str(tree), "directory", allowed_roots=[tree])
        assert "escape_link" not in [entry["name"] for entry in listing["entries"]]
        with pytest.raises(PermissionDenied):
            server_paths.browse(str(link), "directory", allowed_roots=[tree])
    finally:
        try:
            os.rmdir(link) if os.name == "nt" else os.unlink(link)
        except OSError:
            pass


def test_default_configured_roots_isolate_projects_under_workspace(tmp_path):
    cfg = {"WORKSPACE_DIR": tmp_path / "ws", "UT_ALLOWED_SOURCE_ROOTS": []}
    roots_a = server_paths.configured_roots(cfg, project_id=11)
    roots_b = server_paths.configured_roots(cfg, project_id=22)
    assert roots_a != roots_b
    assert roots_a[0].is_dir() and roots_b[0].is_dir()
    with pytest.raises(PermissionDenied):
        server_paths.browse(str(roots_b[0]), "directory", allowed_roots=roots_a)


@pytest.mark.parametrize("path, kind", [("relative/dir", "directory"), ("", "everything")])
def test_rejects_relative_paths_and_unknown_kinds(path, kind, tree):
    with pytest.raises(ServiceError) as raised:
        server_paths.browse(path, kind, allowed_roots=[tree])
    assert raised.value.code == "VALIDATION_ERROR"


@pytest.fixture()
def api(app_ctx, tree):
    from app.extensions import db
    from app.models import LMUser, ProjectMember
    from app.services.lanmatrix import projects_service

    app_ctx.config["UT_ALLOWED_SOURCE_ROOTS"] = [str(tree)]
    with app_ctx.app_context():
        admin = LMUser.query.filter_by(is_system_admin=True).first()
        project = projects_service.create_project(
            admin, code="UTPATH", name="UT 路径", project_type="unit_test")
        editor = LMUser(username="ut-path-editor", password_hash="unused", status="active")
        reader = LMUser(username="ut-path-reader", password_hash="unused", status="active")
        db.session.add_all([editor, reader])
        db.session.flush()
        db.session.add(ProjectMember(project_id=project.id, user_id=editor.id, role="editor"))
        db.session.add(ProjectMember(project_id=project.id, user_id=reader.id, role="reader"))
        db.session.commit()
        users = {"admin": admin.id, "editor": editor.id, "reader": reader.id}
        pid = project.id

    def call(user, method="get", subpath="/server-paths", json_body=None, **query):
        client = app_ctx.test_client()
        with client.session_transaction() as sess:
            sess["lm_user_id"] = users[user]
            sess["csrf_token"] = "t"
        url = f"/api/v1/projects/{pid}/ut{subpath}"
        if method == "put":
            resp = client.put(url, json=json_body, headers={"X-CSRF-Token": "t"})
        else:
            resp = client.get(url, query_string=query)
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


def test_editor_cannot_escape_allowed_roots_or_override_host_toolchain(api, tree):
    outside = tree.parent / "outside_secret"
    status, body = api("editor", path=str(outside))
    assert status == 403
    assert body["error"]["code"] == "PERMISSION_DENIED"

    status, body = api("editor", method="put", subpath="/config", json_body={
        "host_clang": "C:\\evil\\malicious_clang.bat",
    })
    assert status == 403
    assert body["error"]["code"] == "PERMISSION_DENIED"

    status, body = api("editor", method="put", subpath="/config", json_body={
        "product_root": str(outside),
        "index_csv": str(tree / "index.CSV"),
    })
    assert status == 403
    assert body["error"]["code"] == "PERMISSION_DENIED"

    status, body = api("editor", method="put", subpath="/config", json_body={
        "product_root": str(tree / "Soft"),
        "index_csv": str(tree / "index.CSV"),
        "host_clang": "clang",
        "host_llvm_cov": "llvm-cov",
        "host_llvm_profdata": "llvm-profdata",
    })
    assert status == 200, body
