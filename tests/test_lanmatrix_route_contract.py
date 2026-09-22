"""Route-contract snapshot for the issue #5 mechanical split.

The split may move handlers between Python modules, but the public Flask
contract must remain byte-for-byte equivalent at the routing layer: URL rule,
HTTP method, endpoint identity, and wrapped view identity.
"""

from __future__ import annotations


def _projects_items_contract(app):
    rows = []
    for rule in app.url_map.iter_rules():
        if not rule.endpoint.startswith("lanmatrix_projects."):
            continue
        methods = tuple(sorted(
            method for method in rule.methods
            if method not in {"HEAD", "OPTIONS"}
        ))
        view = app.view_functions[rule.endpoint]
        rows.append((rule.rule, methods, rule.endpoint, view.__name__))
    return sorted(rows)


EXPECTED = [
    ("/api/v1/imports/<int:job_id>", ("GET",), "lanmatrix_projects.get_import", "get_import"),
    ("/api/v1/imports/<int:job_id>/commit", ("POST",), "lanmatrix_projects.commit_import", "commit_import"),
    ("/api/v1/projects", ("GET",), "lanmatrix_projects.list_projects", "list_projects"),
    ("/api/v1/projects", ("POST",), "lanmatrix_projects.create_project", "create_project"),
    ("/api/v1/projects/<int:project_id>", ("DELETE",), "lanmatrix_projects.delete_project", "delete_project"),
    ("/api/v1/projects/<int:project_id>", ("GET",), "lanmatrix_projects.get_project", "get_project"),
    ("/api/v1/projects/<int:project_id>", ("PATCH",), "lanmatrix_projects.patch_project", "patch_project"),
    ("/api/v1/projects/<int:project_id>/audit-logs.csv", ("GET",), "lanmatrix_projects.audit_logs_csv", "audit_logs_csv"),
    ("/api/v1/projects/<int:project_id>/audit-logs", ("GET",), "lanmatrix_projects.audit_logs", "audit_logs"),
    ("/api/v1/projects/<int:project_id>/audit-logs/actions", ("GET",), "lanmatrix_projects.audit_log_actions", "audit_log_actions"),
    ("/api/v1/projects/<int:project_id>/categories", ("GET",), "lanmatrix_projects.list_project_categories", "list_project_categories"),
    ("/api/v1/projects/<int:project_id>/collab-token", ("POST",), "lanmatrix_projects.collab_token", "collab_token"),
    ("/api/v1/projects/<int:project_id>/const/export", ("GET",), "lanmatrix_projects.export_const", "export_const"),
    ("/api/v1/projects/<int:project_id>/const/import", ("POST",), "lanmatrix_projects.import_const", "import_const"),
    ("/api/v1/projects/<int:project_id>/dashboard", ("GET",), "lanmatrix_projects.project_dashboard_data", "project_dashboard_data"),
    ("/api/v1/projects/<int:project_id>/excel/template", ("GET",), "lanmatrix_projects.excel_template", "excel_template"),
    ("/api/v1/projects/<int:project_id>/exemptions", ("GET",), "lanmatrix_projects.list_project_exemptions", "list_project_exemptions"),
    ("/api/v1/projects/<int:project_id>/exemptions/bulk", ("POST",), "lanmatrix_projects.decide_exemptions_bulk", "decide_exemptions_bulk"),
    ("/api/v1/projects/<int:project_id>/exports", ("POST",), "lanmatrix_projects.create_export", "create_export"),
    ("/api/v1/projects/<int:project_id>/fields", ("GET",), "lanmatrix_projects.list_fields", "list_fields"),
    ("/api/v1/projects/<int:project_id>/fields", ("POST",), "lanmatrix_projects.add_field", "add_field"),
    ("/api/v1/projects/<int:project_id>/fields/<int:field_id>", ("DELETE",), "lanmatrix_projects.delete_field", "delete_field"),
    ("/api/v1/projects/<int:project_id>/fields/<int:field_id>", ("PATCH",), "lanmatrix_projects.patch_field", "patch_field"),
    ("/api/v1/projects/<int:project_id>/imports", ("POST",), "lanmatrix_projects.create_import", "create_import"),
    ("/api/v1/projects/<int:project_id>/io/export", ("GET",), "lanmatrix_projects.export_io", "export_io"),
    ("/api/v1/projects/<int:project_id>/io/extract", ("POST",), "lanmatrix_projects.extract_io", "extract_io"),
    ("/api/v1/projects/<int:project_id>/io/import", ("POST",), "lanmatrix_projects.import_io", "import_io"),
    ("/api/v1/projects/<int:project_id>/items", ("GET",), "lanmatrix_projects.list_items", "list_items"),
    ("/api/v1/projects/<int:project_id>/items", ("POST",), "lanmatrix_projects.create_item", "create_item"),
    ("/api/v1/projects/<int:project_id>/items/<int:item_id>", ("DELETE",), "lanmatrix_projects.delete_item", "delete_item"),
    ("/api/v1/projects/<int:project_id>/items/<int:item_id>", ("PATCH",), "lanmatrix_projects.patch_item", "patch_item"),
    ("/api/v1/projects/<int:project_id>/items/<int:item_id>/comments", ("GET",), "lanmatrix_projects.list_comments", "list_comments"),
    ("/api/v1/projects/<int:project_id>/items/<int:item_id>/comments", ("POST",), "lanmatrix_projects.add_comment", "add_comment"),
    ("/api/v1/projects/<int:project_id>/items/<int:item_id>/duplicate", ("POST",), "lanmatrix_projects.duplicate_item", "duplicate_item"),
    ("/api/v1/projects/<int:project_id>/items/<int:item_id>/restore", ("POST",), "lanmatrix_projects.restore_item", "restore_item"),
    ("/api/v1/projects/<int:project_id>/items/<row_uuid>/exemption", ("POST",), "lanmatrix_projects.decide_item_exemption", "decide_item_exemption"),
    ("/api/v1/projects/<int:project_id>/items/<row_uuid>/review", ("POST",), "lanmatrix_projects.review_item", "review_item"),
    ("/api/v1/projects/<int:project_id>/items/<row_uuid>/reviewer", ("POST",), "lanmatrix_projects.assign_item_reviewer", "assign_item_reviewer"),
    ("/api/v1/projects/<int:project_id>/items/batch-preview", ("POST",), "lanmatrix_projects.batch_preview", "batch_preview"),
    ("/api/v1/projects/<int:project_id>/items/batch-undo", ("POST",), "lanmatrix_projects.batch_undo", "batch_undo"),
    ("/api/v1/projects/<int:project_id>/items/batch-update", ("POST",), "lanmatrix_projects.batch_update", "batch_update"),
    ("/api/v1/projects/<int:project_id>/items/bulk-delete", ("POST",), "lanmatrix_projects.bulk_delete_items", "bulk_delete_items"),
    ("/api/v1/projects/<int:project_id>/items/bulk-duplicate", ("POST",), "lanmatrix_projects.bulk_duplicate_items", "bulk_duplicate_items"),
    ("/api/v1/projects/<int:project_id>/items/move", ("POST",), "lanmatrix_projects.move_items", "move_items"),
    ("/api/v1/projects/<int:project_id>/libfunc/export", ("GET",), "lanmatrix_projects.export_libfunc", "export_libfunc"),
    ("/api/v1/projects/<int:project_id>/libfunc/import", ("POST",), "lanmatrix_projects.import_libfunc", "import_libfunc"),
    ("/api/v1/projects/<int:project_id>/members", ("GET",), "lanmatrix_projects.list_members", "list_members"),
    ("/api/v1/projects/<int:project_id>/members", ("POST",), "lanmatrix_projects.add_member", "add_member"),
    ("/api/v1/projects/<int:project_id>/members/<int:member_id>", ("DELETE",), "lanmatrix_projects.remove_member", "remove_member"),
    ("/api/v1/projects/<int:project_id>/members/<int:member_id>", ("PATCH",), "lanmatrix_projects.patch_member", "patch_member"),
    ("/api/v1/projects/<int:project_id>/members/candidates", ("GET",), "lanmatrix_projects.member_candidates", "member_candidates"),
    ("/api/v1/projects/<int:project_id>/models", ("DELETE",), "lanmatrix_projects.remove_project_model", "remove_project_model"),
    ("/api/v1/projects/<int:project_id>/models", ("GET",), "lanmatrix_projects.list_project_models", "list_project_models"),
    ("/api/v1/projects/<int:project_id>/models", ("POST",), "lanmatrix_projects.add_project_model", "add_project_model"),
    ("/api/v1/projects/<int:project_id>/models/current", ("POST",), "lanmatrix_projects.set_current_project_model", "set_current_project_model"),
    ("/api/v1/projects/<int:project_id>/models/deprecate", ("POST",), "lanmatrix_projects.deprecate_project_model", "deprecate_project_model"),
    ("/api/v1/projects/<int:project_id>/models/sbs", ("GET",), "lanmatrix_projects.get_model_sbs", "get_model_sbs"),
    ("/api/v1/projects/<int:project_id>/models/sbs", ("PUT",), "lanmatrix_projects.save_model_sbs", "save_model_sbs"),
    ("/api/v1/projects/<int:project_id>/models/sbs/revisions", ("GET",), "lanmatrix_projects.list_model_sbs_revisions", "list_model_sbs_revisions"),
    ("/api/v1/projects/<int:project_id>/models/sbs/revisions/<int:revision_id>", ("GET",), "lanmatrix_projects.get_model_sbs_revision", "get_model_sbs_revision"),
    ("/api/v1/projects/<int:project_id>/models/sbs/revisions/<int:revision_id>/restore", ("POST",), "lanmatrix_projects.restore_model_sbs_revision", "restore_model_sbs_revision"),
    ("/api/v1/projects/<int:project_id>/models/upload", ("POST",), "lanmatrix_projects.upload_project_model", "upload_project_model"),
    ("/api/v1/projects/<int:project_id>/models/version", ("PATCH",), "lanmatrix_projects.update_project_model_version", "update_project_model_version"),
    ("/api/v1/projects/<int:project_id>/pool/<sheet>/entries", ("POST",), "lanmatrix_projects.add_pool_entry", "add_pool_entry"),
    ("/api/v1/projects/<int:project_id>/pool/<sheet>/fields", ("POST",), "lanmatrix_projects.ensure_pool_fields", "ensure_pool_fields"),
    ("/api/v1/projects/<int:project_id>/review_policy", ("PUT",), "lanmatrix_projects.set_review_policy", "set_review_policy"),
    ("/api/v1/projects/<int:project_id>/reviews", ("GET",), "lanmatrix_projects.list_project_reviews", "list_project_reviews"),
    ("/api/v1/projects/<int:project_id>/reviews/bulk", ("POST",), "lanmatrix_projects.review_items_bulk", "review_items_bulk"),
    ("/api/v1/projects/<int:project_id>/testmatrix/export", ("GET",), "lanmatrix_projects.export_test_matrix", "export_test_matrix"),
    ("/api/v1/projects/<int:project_id>/testmatrix/import", ("POST",), "lanmatrix_projects.import_test_matrix", "import_test_matrix"),
    ("/api/v1/projects/<int:project_id>/trash", ("GET",), "lanmatrix_projects.list_trash", "list_trash"),
    ("/api/v1/projects/<int:project_id>/trash/purge", ("POST",), "lanmatrix_projects.purge_from_trash", "purge_from_trash"),
    ("/api/v1/projects/<int:project_id>/trash/restore", ("POST",), "lanmatrix_projects.restore_from_trash", "restore_from_trash"),
]


def test_projects_items_route_contract(app_ctx):
    assert _projects_items_contract(app_ctx) == sorted(EXPECTED)
