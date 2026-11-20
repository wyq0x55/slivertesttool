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

test("SBS escape is intercepted at keydown, before the UA close request", () => {
  const cancel = sbsEditorJs.match(
    /addEventListener\(\s*"cancel"\s*,\s*\(e\)\s*=>\s*\{[\s\S]*?\n      \}\)/,
  );
  assert.ok(cancel, "SBS dialog must keep listening for the native cancel event");
  assert.match(cancel[0], /preventDefault\(\)/, "cancel must be prevented so close() decides");
  assert.match(cancel[0], /close\(\)/, "the existing close() (dirty confirm) must run");

  // Once a dialog's cancel has been prevented, Chromium delivers the next
  // close request with cancelable:false and closes it anyway, so Escape must be
  // stopped while it is still a keydown default action.
  const handler = sbsEditorJs.match(
    /addEventListener\(\s*"keydown"\s*,[\s\S]*?\},\s*true\s*\)/,
  );
  assert.ok(handler, "the SBS keydown handler must be registered in the capture phase");
  assert.match(handler[0], /e\.key === "Escape"/, "Escape must be handled explicitly");
  assert.match(handler[0], /e\.preventDefault\(\)/, "Escape must prevent the native close request");
  assert.match(handler[0], /close\(\)/, "Escape must go through the dirty-checking close()");
  assert.match(
    handler[0],
    /confirmOpenAbove\(\)/,
    "an open confirm dialog owns Escape; the editor must defer to it",
  );
  assert.match(sbsEditorJs, /dialog\[open\]/, "the guard must look for the confirm's <dialog>");
  assert.match(sbsEditorJs, /confirmOpenAbove\(\)/, "the guard must be shared by keydown and cancel");
  // Ctrl/Cmd+S must be preserved.
  assert.match(sbsEditorJs, /e\.ctrlKey\s*\|\|\s*e\.metaKey/, "Ctrl/Cmd+S shortcut must remain");
});
test("backdrop dismissal needs one uninterrupted gesture on the backdrop", () => {
  for (const [label, src, el] of [
    ["version", projectModelsJs, "verModal"],
    ["SBS", sbsEditorJs, "modal"],
  ]) {
    // A click target is the nearest common ancestor, so a press inside the
    // panel released outside still clicks the full-viewport dialog.
    assert.doesNotMatch(
      src,
      /\.addEventListener\("click", \(e\) => \{\s*if \(e\.target ===/,
      `${label} dialog must not dismiss on a click-target check`,
    );
    assert.match(
      src,
      new RegExp(`backdropPress = e\\.target === ${el}`),
      `${label} dialog must record where the pointer went down`,
    );
    assert.match(
      src,
      new RegExp(`pointerup", \\(e\\) => \\{\\s*if \\(backdropPress && e\\.target === ${el}\\)`),
      `${label} dialog must dismiss only when the same pointer lifts on the backdrop`,
    );
    assert.match(src, /addEventListener\("pointercancel"/, `${label} must clear the gesture on pointercancel`);
  }
});

test("version dialog restores focus to its trigger, even after save re-renders", () => {
  assert.match(
    projectModelsJs,
    /openVersion\(models\.find\([\s\S]*?\), b\)/,
    "the clicked button must be handed to openVersion as the trigger",
  );
  assert.match(projectModelsJs, /verTrigger = trigger \|\| null/);
  assert.match(
    projectModelsJs,
    /verTrigger && verTrigger\.isConnected/,
    "focus must go to the trigger when it is still in the document",
  );
  const save = projectModelsJs.match(/async function saveVersion\(\)\s*\{[\s\S]*?\n  \}/);
  assert.ok(save, "saveVersion must exist");
  assert.match(save[0], /const id = verTarget;/, "the model id must be captured before closeVersion()");
  assert.match(
    save[0],
    /render\(data\.models \|\| \[\]\);[\s\S]*?lm-model-ver\[data-model-id=/,
    "after render() focus must move to the replacement button",
  );
});
