const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const root = path.resolve(__dirname, "../..");
const read = (file) => fs.readFileSync(path.join(root, file), "utf8");

const source = read("app/static/js/lanmatrix/steps_editor.js");

function section(start, end) {
  const first = source.indexOf(start);
  const last = source.indexOf(end, first);
  assert.ok(first >= 0 && last > first, `production section ${start}..${end} must exist`);
  return source.slice(first, last);
}

// The JSON-drawer error wiring, verbatim from the production class. Extracting
// it keeps the test on the real source instead of on a copied re-implementation.
const ERROR_CALLBACK = section("_ensureJsonEditor() {", "_toggleJson() {");

// Element shim. `hidden` is a tracked accessor so a test can prove the
// production code never toggles it; a role="alert" region must stay mounted and
// announce through textContent alone.
function makeErrorNode() {
  const node = {
    textContent: "",
    hiddenWrites: [],
    _hidden: false,
  };
  Object.defineProperty(node, "hidden", {
    configurable: true,
    get() { return node._hidden; },
    set(value) { node.hiddenWrites.push(value); node._hidden = value; },
  });
  return node;
}

// Fake LMCodeEditor: records the created instance and exposes the registered
// onError callback so a test can fire it the way json_editor.js setError() does.
function fakeCodeEditorClass(box) {
  return class FakeCodeEditor {
    constructor(host, opts) {
      this.host = host;
      this.opts = opts || {};
      this._errCb = null;
      box.editor = this;
    }
    onError(cb) { this._errCb = typeof cb === "function" ? cb : null; }
    emitError(msg) { if (this._errCb) this._errCb(msg); }
  };
}

function mountCallback() {
  const box = { editor: null };
  const window = { LMCodeEditor: fakeCodeEditorClass(box) };
  const context = vm.createContext({ window });
  const instance = vm.runInContext(
    `(function () { class Harness { ${ERROR_CALLBACK} } return new Harness(); })()`,
    context,
  );
  return { instance, box };
}

test("the JSON error box is a mounted alert region", () => {
  const html = read("app/templates/lanmatrix/_steps_dialog.html");
  const tags = [...html.matchAll(/<div\b[^>]*\bid="lm-steps-json-error"[^>]*>/g)];
  assert.equal(tags.length, 1, "expected exactly one #lm-steps-json-error div");
  const tag = tags[0][0];
  assert.match(tag, /\bclass="login-err"/, "keeps the login-err styling hook");
  assert.match(tag, /\brole="alert"/, "must be an alert live region");
  assert.doesNotMatch(tag, /\shidden(?=[\s>])/, "must not start hidden");
});

test("steps_editor.js never toggles hidden on the JSON error box", () => {
  assert.doesNotMatch(ERROR_CALLBACK, /jsonErrEl\.hidden\s*=/, "error callback must only write textContent");
  assert.doesNotMatch(source, /jsonErrEl\.hidden\s*=/, "nothing else may hide the mounted region either");
});

test("an invalid-JSON message is announced through textContent", () => {
  const { instance, box } = mountCallback();
  const node = makeErrorNode();
  instance.jsonHost = { id: "lm-steps-json-body" };
  instance.jsonErrEl = node;
  instance.jsonEditor = null;

  assert.equal(instance._ensureJsonEditor(), box.editor, "editor is constructed from the host");
  assert.ok(box.editor, "LMCodeEditor shim was constructed");
  assert.equal(typeof box.editor.opts.onChange, "function", "live-sync handler stays bound");

  const message = "JSON 解析错误：Unexpected token } in JSON at position 12";
  box.editor.emitError(message);
  assert.equal(node.textContent, message, "message lands in the live region");
  assert.deepEqual(node.hiddenWrites, [], "announcement must not toggle hidden");
  assert.equal(node.hidden, false, "region stays mounted after the error");
});

test("clearing the error empties the text without hiding the region", () => {
  const { instance, box } = mountCallback();
  const node = makeErrorNode();
  instance.jsonHost = {};
  instance.jsonErrEl = node;
  instance.jsonEditor = null;

  instance._ensureJsonEditor();
  box.editor.emitError("JSON 解析错误：boom");
  box.editor.emitError("");
  assert.equal(node.textContent, "", "cleared message removes the text");
  assert.deepEqual(node.hiddenWrites, [], "clearing must not toggle hidden");
});

test("a missing error element is tolerated", () => {
  const { instance } = mountCallback();
  instance.jsonHost = {};
  instance.jsonErrEl = null;
  instance.jsonEditor = null;
  instance._ensureJsonEditor();
  assert.doesNotThrow(() => instance.jsonEditor._errCb && instance.jsonEditor._errCb("x"));
});

test("the empty JSON error box collapses its top margin", () => {
  const css = read("app/static/css/lanmatrix.css");
  const base = css.indexOf("#lm-steps-json-error {");
  assert.ok(base >= 0, "base margin-top rule must exist");
  const rule = css.match(/#lm-steps-json-error:empty\s*\{([^}]*)\}/);
  assert.ok(rule, "#lm-steps-json-error:empty collapse rule must exist");
  const at = css.indexOf("#lm-steps-json-error:empty");
  assert.ok(at > base, "collapse rule follows the base rule");
  assert.match(rule[1], /margin-top:\s*0/, "empty box drops the 8px gap");
  // .login-err (design-system.css) paints a padded danger band; the box stays
  // mounted for announcements, so the empty state must be invisible rather
  // than a permanent pale-red strip.
  assert.match(rule[1], /padding:\s*0/, "empty box drops the danger-band padding");
  assert.match(rule[1], /background:\s*transparent/, "empty box drops the danger-band fill");
});