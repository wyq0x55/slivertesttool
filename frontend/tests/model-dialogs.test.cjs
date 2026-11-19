const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");

const read = (rel) => fs.readFileSync(path.resolve(__dirname, "../..", rel), "utf8");

const modelsHtml = read("app/templates/lanmatrix/models.html");
const projectModelsJs = read("app/static/js/lanmatrix/project_models.js");
const sbsEditorJs = read("app/static/js/lanmatrix/sbs_editor.js");

const dialogTag = (id) => {
  const tags = modelsHtml.match(new RegExp(`<dialog\\b[^>]*\\bid="${id}"[^>]*>`, "g")) || [];
  assert.equal(tags.length, 1, `expected exactly one <dialog id="${id}">`);
  return tags[0];
};

test("both models-page modals are native <dialog>s named by their headings", () => {
  const ver = dialogTag("lm-ver-modal");
  assert.match(ver, /\saria-labelledby="lm-ver-heading"/, "version dialog must reference its heading");
  assert.doesNotMatch(ver, /\shidden\b/, "a <dialog> must rely on the UA closed state, not hidden");

  const sbs = dialogTag("lm-sbs-modal");
  assert.match(sbs, /\saria-labelledby="lm-sbs-heading"/, "SBS dialog must reference its heading");
  assert.doesNotMatch(sbs, /\shidden\b/, "a <dialog> must rely on the UA closed state, not hidden");
});

test("the fake ARIA modal role and the backdrop div are gone", () => {
  assert.doesNotMatch(modelsHtml, /\brole="dialog"/, "<dialog> already supplies role=dialog");
  assert.doesNotMatch(modelsHtml, /\baria-modal=/, "<dialog> already supplies aria-modal");
  assert.doesNotMatch(modelsHtml, /lm-modal-backdrop/, "the backdrop is now the UA ::backdrop");
});

test("#lm-ver-err is announced as an alert", () => {
  const tags = modelsHtml.match(/<[^>]*\bid="lm-ver-err"[^>]*>/g) || [];
  assert.equal(tags.length, 1, "expected exactly one #lm-ver-err element");
  assert.match(tags[0], /\brole="alert"/, "validation errors must be announced, not just shown");
});

test("version modal opens with showModal and closes with close(), not hidden", () => {
  assert.match(projectModelsJs, /verModal\.showModal\(\)/, "version modal must be opened modally");
  assert.match(projectModelsJs, /verModal\.close\(\)/, "version modal must be closed via dialog.close()");
  assert.doesNotMatch(projectModelsJs, /verModal\.hidden\s*=/, "must not toggle hidden on the dialog");
});

test("SBS editor opens with showModal and closes with close(), not hidden", () => {
  assert.match(sbsEditorJs, /modal\.showModal\(\)/, "SBS modal must be opened modally");
  assert.match(sbsEditorJs, /modal\.close\(\)/, "SBS modal must be closed via dialog.close()");
  assert.doesNotMatch(sbsEditorJs, /\bmodal\.hidden\b/, "must not read or write hidden on the dialog");
  assert.doesNotMatch(sbsEditorJs, /\bdlg\.hidden\b/, "the keydown guard must use dialog.open");
});

test("SBS escape cancels natively, then runs the dirty-checking close()", () => {
  const handler = sbsEditorJs.match(
    /addEventListener\(\s*"cancel"\s*,\s*\(e\)\s*=>\s*\{[^}]*\}/,
  );
  assert.ok(handler, "SBS dialog must listen for the native cancel event");
  assert.match(handler[0], /preventDefault\(\)/, "cancel must be prevented so close() decides");
  assert.match(handler[0], /close\(\)/, "the existing close() (dirty confirm) must run");

  // Esc must not be handled directly in the document keydown path any more.
  assert.doesNotMatch(
    sbsEditorJs,
    /e\.key\s*===\s*"Escape"\)\s*\{\s*close\(\)/,
    "document keydown must not call close() on Escape; cancel owns that path",
  );
  // Ctrl/Cmd+S must be preserved.
  assert.match(sbsEditorJs, /e\.ctrlKey\s*\|\|\s*e\.metaKey/, "Ctrl/Cmd+S shortcut must remain");
});