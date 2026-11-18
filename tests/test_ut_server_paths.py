"""Execution-server path browsing for the project binding pickers."""

from __future__ import annotations

import json
import os
import subprocess

import pytest

from app import create_app
from app.config import Config
from app.extensions import db
from app.services.lanmatrix.errors import ServiceError
from app.services.lanmatrix.permissions import PermissionDenied
from app.services.ut_pipeline import host, pipeline, server_paths, toolchains


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
def ut_sqlite_app(tmp_path, tree):
    ws_dir = tmp_path / "ws"
    inst_dir = tmp_path / "instance"
    up_dir = tmp_path / "uploads"
    rep_dir = tmp_path / "reports"
    for folder in (ws_dir, inst_dir, up_dir, rep_dir):
        folder.mkdir(parents=True, exist_ok=True)

    class TestCfg(Config):
        TESTING = True
        SECRET_KEY = "ut-server-paths-sqlite-test-secret-32b"
        SQLALCHEMY_DATABASE_URI = f"sqlite:///{(tmp_path / 'ut_paths.db').as_posix()}"
        SQLALCHEMY_ENGINE_OPTIONS = {}
        WORKSPACE_DIR = ws_dir
        INSTANCE_DIR = inst_dir
        UPLOAD_DIR = up_dir
        REPORT_DIR = rep_dir
        UT_ALLOWED_SOURCE_ROOTS = [str(tree)]
        UT_ALLOWED_TOOLCHAINS = []
        UT_TOOLCHAIN_PROFILES = ""
        GLOBAL_LOGIN_REQUIRED = False

    application = create_app(TestCfg)
    with application.app_context():
        db.create_all()
        yield application
        db.session.remove()
        db.drop_all()
        db.engine.dispose()


@pytest.fixture()
def api(ut_sqlite_app, tree):
    from app.models import LMUser, ProjectMember
    from app.services.lanmatrix import projects_service

    with ut_sqlite_app.app_context():
        admin = LMUser(
            username="ut-sys-admin",
            password_hash="unused",
            status="active",
            is_system_admin=True,
        )
        editor = LMUser(username="ut-path-editor", password_hash="unused", status="active")
        reader = LMUser(username="ut-path-reader", password_hash="unused", status="active")
        db.session.add_all([admin, editor, reader])
        db.session.flush()
        project = projects_service.create_project(
            admin, code="UTPATH", name="UT 路径", project_type="unit_test")
        db.session.add(ProjectMember(project_id=project.id, user_id=editor.id, role="project_admin"))
        db.session.add(ProjectMember(project_id=project.id, user_id=reader.id, role="reader"))
        db.session.commit()
        users = {"admin": admin.id, "editor": editor.id, "reader": reader.id}
        pid = project.id

    def call(user, method="get", subpath="/server-paths", json_body=None, **query):
        client = ut_sqlite_app.test_client()
        with client.session_transaction() as sess:
            sess["lm_user_id"] = users[user]
            sess["csrf_token"] = "t"
        url = f"/api/v1/projects/{pid}/ut{subpath}"
        if method == "put":
            resp = client.put(url, json=json_body, headers={"X-CSRF-Token": "t"})
        else:
            resp = client.get(url, query_string=query)
        return resp.status_code, resp.get_json()

    call.app = ut_sqlite_app
    call.pid = pid
    call.users = users
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


def test_toolchain_registry_profiles_and_validation(tmp_path):
    llvm_bin = tmp_path / "llvm18" / "bin"
    llvm_bin.mkdir(parents=True)
    clang_18 = str(llvm_bin / "clang.exe")
    cov_18 = str(llvm_bin / "llvm-cov.exe")
    prof_18 = str(llvm_bin / "llvm-profdata.exe")
    rt_18 = str(llvm_bin / "clang_rt.profile.lib")
    extra_allowed = str(tmp_path / "extra" / "custom-clang.exe")

    cfg = {
        "UT_TOOLCHAIN_PROFILES": json.dumps({
            "llvm-18-win64": {
                "label": "LLVM 18 Win64",
                "host_clang": clang_18,
                "host_llvm_cov": cov_18,
                "host_llvm_profdata": prof_18,
                "host_profile_runtime": rt_18,
            }
        }),
        "UT_ALLOWED_TOOLCHAINS": [extra_allowed, "clang-custom-bare"],
    }

    profiles = toolchains.list_profiles(cfg)
    assert [p["id"] for p in profiles] == ["default", "llvm-18-win64"]
    assert profiles[0] == {
        "id": "default",
        "label": "System Default (clang / llvm-cov / llvm-profdata)",
        "host_clang": "clang",
        "host_llvm_cov": "llvm-cov",
        "host_llvm_profdata": "llvm-profdata",
        "host_profile_runtime": "",
    }
    resolved = toolchains.resolve_profile("llvm-18-win64", cfg)
    assert resolved["host_clang"] == clang_18

    with pytest.raises(ServiceError) as bad_id:
        toolchains.list_profiles({"UT_TOOLCHAIN_PROFILES": '{"../bad": {}}'})
    assert bad_id.value.code == "VALIDATION_ERROR"

    # Non-admin can use exact profile match, but cannot override individual binaries.
    toolchains.verify_toolchain_config(
        {"toolchain_profile": "llvm-18-win64", **resolved},
        allow_custom_admin=False,
        config=cfg,
    )
    with pytest.raises(ServiceError) as non_admin_override:
        toolchains.verify_toolchain_config(
            {**resolved, "toolchain_profile": "llvm-18-win64", "host_clang": extra_allowed},
            allow_custom_admin=False,
            config=cfg,
        )
    assert non_admin_override.value.code == "PERMISSION_DENIED"

    # Admin can use UT_ALLOWED_TOOLCHAINS (resolved path or bare command), but never untrusted paths.
    toolchains.verify_toolchain_config(
        {"toolchain_profile": "default", "host_clang": extra_allowed},
        allow_custom_admin=True,
        config=cfg,
    )
    toolchains.verify_toolchain_config(
        {"toolchain_profile": "default", "host_clang": "clang-custom-bare"},
        allow_custom_admin=True,
        config=cfg,
    )
    with pytest.raises(ServiceError) as untrusted:
        toolchains.verify_toolchain_config(
            {"toolchain_profile": "default", "host_clang": str(tmp_path / "untrusted.exe")},
            allow_custom_admin=True,
            config=cfg,
        )
    assert untrusted.value.code == "PERMISSION_DENIED"


def test_named_profile_selection_available_toolchains_and_no_unchanged_bypass(api, tree, tmp_path):
    llvm_dir = tmp_path / "opt_llvm18"
    clang_p = str(llvm_dir / "clang")
    cov_p = str(llvm_dir / "llvm-cov")
    prof_p = str(llvm_dir / "llvm-profdata")
    rt_p = str(llvm_dir / "libclang_rt.profile.a")
    admin_extra = str(tmp_path / "admin_allowed" / "clang-19")

    api.app.config["UT_TOOLCHAIN_PROFILES"] = json.dumps({
        "llvm-18": {
            "label": "LLVM 18 Toolchain",
            "host_clang": clang_p,
            "host_llvm_cov": cov_p,
            "host_llvm_profdata": prof_p,
            "host_profile_runtime": rt_p,
        }
    })
    api.app.config["UT_ALLOWED_TOOLCHAINS"] = [admin_extra]

    # 1. GET /config exposes available_toolchains alongside config=None initially.
    status, body = api("reader", method="get", subpath="/config")
    assert status == 200, body
    assert body["data"] is None
    status, body = api("reader", method="get", subpath="/config", include_toolchains="1")
    assert status == 200, body
    assert body["data"]["config"] is None
    assert [p["id"] for p in body["data"]["available_toolchains"]] == ["default", "llvm-18"]

    # 2. project.edit user selects named profile -> populates host_* automatically.
    status, body = api("editor", method="put", subpath="/config", json_body={
        "product_root": str(tree / "Soft"),
        "index_csv": str(tree / "index.CSV"),
        "toolchain_profile": "llvm-18",
    })
    assert status == 200, body
    assert body["data"]["toolchain_profile"] == "llvm-18"
    assert body["data"]["host_clang"] == clang_p
    assert body["data"]["host_llvm_cov"] == cov_p
    assert body["data"]["host_llvm_profdata"] == prof_p
    assert body["data"]["host_profile_runtime"] == rt_p

    # 3. project.edit user cannot override host_clang away from selected profile, even to UT_ALLOWED_TOOLCHAINS.
    status, body = api("editor", method="put", subpath="/config", json_body={
        "toolchain_profile": "llvm-18",
        "host_clang": admin_extra,
    })
    assert status == 403
    assert body["error"]["code"] == "PERMISSION_DENIED"

    # 4. System admin can override to an allowed binary, but NOT to an untrusted path.
    status, body = api("admin", method="put", subpath="/config", json_body={
        "toolchain_profile": "llvm-18",
        "host_clang": admin_extra,
    })
    assert status == 200, body
    assert body["data"]["host_clang"] == admin_extra

    # Non-admin editing unrelated fields keeps valid admin-approved toolchain intact.
    status, body = api("editor", method="put", subpath="/config", json_body={
        "max_repair_rounds": 3,
    })
    assert status == 200, body
    assert body["data"]["host_clang"] == admin_extra

    status, body = api("admin", method="put", subpath="/config", json_body={
        "host_clang": str(tmp_path / "evil_clang"),
    })
    assert status == 403
    assert body["error"]["code"] == "PERMISSION_DENIED"

    # 5. Tamper existing DB row directly: saving an unrelated field MUST NOT skip validation ("unchanged" bypass removed).
    with api.app.app_context():
        cfg_row = pipeline.get_config(api.pid)
        cfg_row.host_clang = str(tmp_path / "legacy_tampered_clang")
        db.session.commit()

    status, body = api("editor", method="put", subpath="/config", json_body={
        "max_repair_rounds": 4,
    })
    assert status == 403
    assert body["error"]["code"] == "PERMISSION_DENIED"

    status, body = api("admin", method="put", subpath="/config", json_body={
        "max_repair_rounds": 4,
    })
    assert status == 403
    assert body["error"]["code"] == "PERMISSION_DENIED"

    # Selecting a valid profile heals the row cleanly.
    status, body = api("editor", method="put", subpath="/config", json_body={
        "toolchain_profile": "default",
    })
    assert status == 200, body
    assert body["data"]["host_clang"] == "clang"


def test_worker_execution_time_toolchain_verification_blocks_tampered_snapshot(
    ut_sqlite_app, tmp_path, monkeypatch
):
    from app.models import LMUser, UtAttempt, UtBatch, UtJob, UtTarget
    from app.models.ut_pipeline import GateCode
    from app.services.lanmatrix import projects_service

    called = []
    monkeypatch.setattr(
        host, "execute", lambda **kwargs: called.append(kwargs)
    )

    csv_bytes = b'mod,"a.c/f","f",1,1\r\n#COMMENT,x,ret\r\n,0,1\r\n'
    csv_file = tmp_path / "candidate.csv"
    csv_file.write_bytes(csv_bytes)
    import hashlib
    digest = hashlib.sha256(csv_bytes).hexdigest()

    with ut_sqlite_app.app_context():
        admin = LMUser(
            username="worker-check-admin",
            password_hash="unused",
            status="active",
            is_system_admin=True,
        )
        db.session.add(admin)
        db.session.flush()
        project = projects_service.create_project(
            admin, code="UTWRK", name="UT Worker Verify", project_type="unit_test"
        )
        target = UtTarget(
            project_id=project.id,
            row_no=1,
            callcnt=0,
            source_name="a.c",
            function="f",
            source_rel="src/a.c",
            unit_rel="WinAMS/src/a.c/f",
        )
        db.session.add(target)
        db.session.flush()
        batch = UtBatch(
            project_id=project.id,
            label="tampered",
            status=UtBatch.STATUS_RUNNING,
            created_by=admin.id,
            config_snapshot={
                "toolchain_profile": "default",
                "host_clang": str(tmp_path / "evil_tampered_clang.exe"),
                "host_llvm_cov": "llvm-cov",
                "host_llvm_profdata": "llvm-profdata",
                "host_profile_runtime": "",
            },
        )
        db.session.add(batch)
        db.session.flush()
        job = UtJob(
            batch_id=batch.id,
            project_id=project.id,
            target_id=target.id,
            state=UtJob.EXECUTING,
            reference_csv=str(csv_file),
            generation={"ir_json": "ir.json", "intent_manifest": "intents.json"},
        )
        db.session.add(job)
        db.session.flush()
        attempt = UtAttempt(
            job_id=job.id,
            seq=1,
            origin=UtAttempt.ORIGIN_GENERATOR,
            csv_path=str(csv_file),
            csv_sha256=digest,
        )
        db.session.add(attempt)
        db.session.flush()
        job.current_attempt_id = attempt.id
        db.session.commit()
        job_id = job.id

        pipeline.advance_job(job_id)

        refreshed = db.session.get(UtJob, job_id)
        assert called == []
        assert refreshed.state == UtJob.ERROR
        assert refreshed.gate_code == GateCode.INTEGRITY_ERROR
        assert "host_clang" in refreshed.reason
