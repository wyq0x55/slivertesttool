# Project binding and authored ProjectManifest documents

The binding page accepts an existing JSON ProjectManifest or a form-authored
document. Import is a local draft operation; only saving updates the binding.
The page shows project/source locations, pointer width and test requirements by
default. Compiler compatibility, excluded directories, environment assumptions,
AI assistance and tool paths live in a collapsed advanced section. Boolean
options use native checkboxes. The page preserves additional imported fields and
exposes the full JSON for advanced editing. Server filesystem paths remain appropriate for the source
tree, function index and execution tools, but are no longer the primary way to
author a ProjectManifest.

## API and ownership

- `GET /api/v1/projects/<id>/ut/config` returns the existing settings plus
  `manifest_document`, `manifest_sha256` and `manifest_error`. Unreadable or
  invalid legacy bindings retain their path and return a generic error without
  exposing file contents or validation excerpts.
- `PUT .../config` accepts a JSON object in `manifest_document` alongside the
  existing settings. A nonempty `manifest_path` and document cannot be supplied
  together. Existing path-only callers remain supported.
- `POST .../manifest/validate` accepts `{manifest_document: {...}}`, validates
  without updating project configuration, and returns its canonical SHA-256 and
  available baseline/provenance information.

Viewing uses the existing `project.view` permission. Validation and saving use
`project.edit` and the existing CSRF protection. Read-only users can inspect the
binding but cannot validate, import or save changes.

The platform owns transport and storage. It passes a temporary JSON file to the
external `ut-agent bridge constraints` command, which owns schema validation,
versioned baseline references, approved interface contracts and rule packs. The
platform does not copy those semantic rules or import ut-agent Python modules.
Successful validation does not itself establish execution support, Baseline
acceptance or delivery validation.

## Revision storage

Validated authored documents are serialized as UTF-8 JSON with sorted keys and
stored at `WORKSPACE_DIR/ut/p<project_id>/manifests/<sha256>.json`. A complete
validated temporary file is published atomically without replacing an existing
revision. Repeated identical saves reuse the same revision. A hash collision or
changed file at an existing address fails rather than overwriting it.

The existing config model continues to store `manifest_path`, so no new database
migration or web-process initialization is needed. A batch snapshots the path to
that immutable revision; later saves produce a different file and leave the old
batch's manifest bytes available. Legacy external path bindings keep their
existing behavior until the user saves document content through the editor.

JSON documents are limited to 256 KiB after canonical serialization. The browser
also checks imported file size. Repository resource references remain IDs and
versions resolved by ut-agent, independent of the managed document location.

## Verification

`tests/test_ut_manifest.py` covers content round trips, nested imported fields,
content-addressed paths, queued batch revisions, validation without saving,
failed validation, malformed/oversized payloads, permissions and CSRF. Its opt-in
external bridge cases use `UT_HOST_TEST_AGENT_ROOT` and `UT_HOST_TEST_PYTHON` to
verify the actual schema and reject unrelated server JSON as a file reader.

Frontend tests cover local import, draft preservation and edit/save interactions.
Browser component verification uses the actual template, script and design
system with synthetic configuration. It does not mutate production projects or
invoke an AI provider.

The final 2026-10-22 policy integration passed 129 targeted PostgreSQL/backend
tests and all 156 frontend Node tests. The production frontend build passed.
The backend run included the real external bridge, extractor, formal generation,
Clang and LLVM tools for both legacy and explicit policies, followed by human
review and approved export through the platform API. Its model transport was
faked; it does not claim model quality or WinAMS/RH850 execution.

Browser component checks used the actual template, script and design system
with synthetic API responses. They verified explicit conversion, unselected
choices, editing and saving; earlier Manifest component checks also covered
import, nested fields, backend/stub controls, read-only users, light/dark themes
and a 390px viewport. No production projects were modified.


## Explicit test policy

A new ProjectManifest uses `test_policy` instead of a Baseline ID and version.
New documents start with all five checkboxes unchecked and save explicit boolean
values, including `false`; users do not need to open five yes/no menus.
`combination_group` remains an independently enabled presentation option and is
valid only when `mcdc` is true. Turning MC/DC off clears and disables its group
checkbox; re-enabling MC/DC does not silently restore that choice. Fixed minimum,
maximum and boundary values are always required and are not selectable.

Imported documents that contain `baseline`, `profile.mcdc_enabled` or a non-null
`rules.project_pack` cannot be saved until those removed fields are explicitly
cleared. Users must review the original requirements before creating a new batch.
Missing or malformed imported policy flags stay visibly indeterminate and block
saving until explicitly confirmed; unrelated edits do not guess their values.
An indeterminate grouping checkbox remains available to confirm it off when
MC/DC is off, without requiring the user to temporarily enable extra coverage.
Unknown imported fields and the JSON editor round trip are preserved.

There are no form controls for removed project rule packs, unused `build.config`
or provenance-only exception identifiers. WinAMS-specific memory/projection
parameters and optional interface declarations remain available through imported
or advanced JSON. Removing their duplicate controls never strips their values
or existing delivery locks. When model Stub candidates are disabled, the page
omits inactive candidate-model settings from updates; the backend retains saved
values without validating hidden inputs as active candidate settings.

The platform stores the bridge response fields `test_policy`,
`effective_test_policy`, `test_policy_sha256`, `test_rules_ref` and `test_rules`
with the validated manifest response. Batch generation continues to freeze the
complete constraints response, so those fields are retained in the batch snapshot
when the bridge supplies them. The platform does not derive a policy locally.

The read API validates an exact bounded snapshot before exposing a bound
document, so concurrent changes to a legacy file cannot mix one revision's
content with another revision's constraints. The browser refuses imported or
raw JSON numeric values it cannot preserve exactly; unsafe large numbers in a
bound document also block the editor's read response. The original file remains
available through the established server-path binding instead of being silently
rounded by the editor.

## Settings simplification verification (2026-10-29)

The isolated settings change passes all 33 binding-component Node tests. The
broader frontend run passes 225 of 226 tests; the remaining failure is the
unchanged job-detail legacy Baseline text assertion in `ut-project.test.cjs`.
It is outside this settings change and is not reported as a full-suite pass.

Browser component checks used the actual template, settings script and shared
design system with synthetic data only. They verified checkbox save behavior,
MC/DC grouping dependencies, the single compiler-default option, collapsed
advanced settings, 390px layout, dark mode, and automatic expansion/focus on a
JSON error. No production bindings, queued batches or database rows were changed.
