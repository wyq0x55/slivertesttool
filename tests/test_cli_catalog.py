import pytest


def test_catalog_matches_registered_business_api(monkeypatch):
    from flask import Flask
    from app.routes.lanmatrix import BLUEPRINTS
    from silver_cli.catalog import operations

    app = Flask(__name__)
    for blueprint in BLUEPRINTS:
        app.register_blueprint(blueprint)
    expected = {(method, rule.rule) for rule in app.url_map.iter_rules()
                if rule.rule.startswith("/api/v1/")
                for method in rule.methods - {"HEAD", "OPTIONS"}}
    actual = operations()
    assert {(item.method, item.path) for item in actual} == expected
    assert len(actual) == len(expected) == len({item.name for item in actual})


def test_shipped_catalog_matches_route_source():
    from scripts.export_cli_catalog import render
    from silver_cli.catalog import CATALOG_PATH

    assert CATALOG_PATH.read_text(encoding="utf-8") == render(), (
        "Regenerate with: python scripts/export_cli_catalog.py")


def test_zipapp_runs_without_platform_source(tmp_path):
    import json
    import subprocess
    import sys

    from silver_cli.bundle import build

    archive = tmp_path / "silver-cli.pyz"
    archive.write_bytes(build())
    assert build() == archive.read_bytes()
    result = subprocess.run([sys.executable, "-I", str(archive), "describe", "ut_pipeline.export_approved"],
                            cwd=tmp_path, capture_output=True, text=True, check=True)
    assert json.loads(result.stdout)["data"]["response_kind"] == "download"


def test_catalog_transitive_hints_and_kinds():
    from silver_cli.catalog import get_operation

    assert get_operation("ai.get_draft").path == "/api/v1/ai/drafts/<int:draft_id>"
    assert get_operation("imports_exports.import_const").file_fields == ["file"]
    assert "mode" in get_operation("imports_exports.import_const").form_fields
    assert get_operation("tasks.project_task_stream").response_kind == "stream"
    assert get_operation("audit_trash.audit_logs_csv").response_kind == "download"
    assert get_operation("imports_exports.create_export").response_kind == "download"
    assert get_operation("ut_pipeline.attempt_artifact").response_kind == "download"
    assert get_operation("items.patch_item").path_params == {"project_id": "int", "item_id": "int"}
    assert "version" in get_operation("items.patch_item").body_fields
    assert "files" in get_operation("tasks.upload_project_tree").file_fields
    assert "paths" in get_operation("tasks.upload_project_tree").form_fields
    assert "project_id" in get_operation("ai.list_drafts").query_params
    assert get_operation("projects.create_project").mutating is True
    assert get_operation("projects.list_projects").mutating is False
    with pytest.raises(ValueError):
        get_operation("not_a_real_command")


def test_server_serves_the_zipapp_it_would_build():
    from flask import Flask
    from app.routes.lanmatrix import auth_bp
    from silver_cli.bundle import build

    app = Flask(__name__)
    app.register_blueprint(auth_bp)
    response = app.test_client().get("/api/v1/cli/silver-cli.pyz")
    assert response.status_code == 200
    assert response.headers["Content-Disposition"] == 'attachment; filename="silver-cli.pyz"'
    assert response.data == build()
