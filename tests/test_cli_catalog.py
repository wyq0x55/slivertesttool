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


def test_catalog_transitive_hints_and_kinds():
    from silver_cli.catalog import get_operation

    assert get_operation("ai.get_draft").path == "/api/v1/ai/drafts/<int:draft_id>"
    assert get_operation("imports_exports.import_const").file_fields == ["file"]
    assert "mode" in get_operation("imports_exports.import_const").form_fields
    assert get_operation("tasks.project_task_stream").response_kind == "stream"
    assert get_operation("audit_trash.audit_logs_csv").response_kind == "download"
    assert get_operation("imports_exports.create_export").response_kind == "download"
    assert get_operation("items.patch_item").path_params == {"project_id": "int", "item_id": "int"}
    assert "version" in get_operation("items.patch_item").body_fields
    assert "files" in get_operation("tasks.upload_project_tree").file_fields
    assert "paths" in get_operation("tasks.upload_project_tree").form_fields
    assert "project_id" in get_operation("ai.list_drafts").query_params
    assert get_operation("projects.create_project").mutating is True
    assert get_operation("projects.list_projects").mutating is False
    with pytest.raises(ValueError):
        get_operation("not_a_real_command")
