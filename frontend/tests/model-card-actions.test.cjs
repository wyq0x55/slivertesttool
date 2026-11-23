// Guards the project model cards after replacing the card-level
// `role="button"` with a stretched native button. A role="button" card that
// contains the real action buttons is an invalid nested interactive:
// assistive technology flattens the inner buttons and the card keydown
// handler competes with theirs. The card is now a plain div; one invisible
// `.mcard-select` button covers it and the visible action row sits above it.
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const root = path.resolve(__dirname, "../..");
const scriptPath = path.join(root, "app/static/js/lanmatrix/project_models.js");
const source = fs.readFileSync(scriptPath, "utf8");

// Source slice of a top-level `function name(...) { ... }` inside the IIFE.
// The extracted bodies contain balanced braces only (template `${...}` pairs
// included), so plain depth counting is enough here.
function functionSource(text, name) {
  const start = text.indexOf(`  function ${name}(`);
  assert.notEqual(start, -1, `${name} should exist`);
  const open = text.indexOf("{", start);
  let depth = 0;
  for (let i = open; i < text.length; i += 1) {
    if (text[i] === "{") depth += 1;
    else if (text[i] === "}") {
      depth -= 1;
      if (depth === 0) return text.slice(start, i + 1);
    }
  }
  assert.fail(`${name} body is not closed`);
}

// Runs the production modelCard() with the real esc() helper and the real
// label/icon constants; `canManage` is the module-level flag it reads.
const constantNames = ["KIND_LABEL", "CUBE", "CHECK"];
const constants = constantNames.map((name) => {
  const match = source.match(new RegExp(`const ${name} = .*;`));
  assert.ok(match, `${name} should exist`);
  return match[0];
});
const context = { canManage: true };
const { modelCard } = vm.runInNewContext(
  `${functionSource(source, "esc")}\n` +
    `${constants.join("\n")}\n` +
    `${functionSource(source, "modelCard")}\n` +
    `;({ modelCard });`,
  context,
);

function card(fields = {}) {
  return modelCard(Object.assign({
    id: 7, name: "m1.sil", kind: "path", exists: true, version: "1.0",
  }, fields));
}

test("a selectable card is a plain div with one stretched select button", () => {
  context.canManage = true;
  const html = card();
  const rootTag = html.trim().match(/^<div[^>]*>/);
  assert.ok(rootTag, "the card starts with its root div");
  assert.doesNotMatch(rootTag[0], /\brole=/, "no button role on the card");
  assert.doesNotMatch(rootTag[0], /\btabindex=/, "no tabindex on the card");
  assert.doesNotMatch(rootTag[0], /\btitle=/, "no title on the card");

  // First child, and exactly one: the card's whole surface is this button.
  const trimmed = html.trim();
  assert.match(trimmed, /^<div[^>]*>\s*<button type="button" class="mcard-select"/);
  const buttons = html.match(/<button\b[^>]*class="mcard-select"[^>]*>[^<]*<\/button>/g) || [];
  assert.equal(buttons.length, 1, "exactly one .mcard-select button");
  assert.match(buttons[0], /^<button type="button" class="mcard-select"[^>]*>设为当前模型：m1\.sil<\/button>$/);
  assert.match(buttons[0], /data-model-id="7"/);
  assert.match(buttons[0], /data-name="m1\.sil"/);
});

test("current and non-manager cards get no select button", () => {
  context.canManage = true;
  assert.doesNotMatch(card({ is_current: true }), /mcard-select/);
  context.canManage = false;
  assert.doesNotMatch(card(), /mcard-select/);
});

test("a bundle model keeps its own visible action buttons", () => {
  context.canManage = true;
  const html = card({ kind: "bundle" });
  for (const cls of ["lm-model-cur", "lm-model-ver", "lm-model-sbs", "lm-model-del"]) {
    assert.match(html, new RegExp(`<button\\b[^>]*class="[^"]*\\b${cls}\\b[^"]*"`), `${cls} should exist`);
  }
  // The visible "make current" button is unchanged and still names the model.
  assert.match(html, /class="[^"]*\blm-model-cur\b[^"]*"[^>]*>设为当前<\/button>/);
});

test("render() binds the stretched button by click and has no card keydown", () => {
  context.canManage = true;
  const render = functionSource(source, "render");
  assert.doesNotMatch(render, /\.mcard\.selectable/, "no card-level listener");
  assert.doesNotMatch(render, /addEventListener\("keydown"/, "native buttons handle Enter/Space");
  assert.match(render, /querySelectorAll\("\.mcard-select"\)\s*\.forEach/);
  assert.match(render, /addEventListener\("click", \(\) => setCurrent\(/);
  // The other listeners, with their stopPropagation, stay as they were.
  for (const cls of ["lm-model-del", "lm-model-sbs", "lm-model-ver", "lm-model-cur"]) {
    assert.match(render, new RegExp(`querySelectorAll\\("\\.${cls}"\\)`), `${cls} listener should stay`);
  }
  assert.match(render, /e\.stopPropagation\(\)/);
});