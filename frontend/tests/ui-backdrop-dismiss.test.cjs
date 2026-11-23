// Guards LMUI dialog backdrop dismissal (frontend modernization).
//
// A modal <dialog> that covers the viewport receives the click when a press
// starts inside the panel and is released over the backdrop. Dismissing on that
// click would throw away a type-to-confirm or a half-written prompt, so the
// dismissal must be one uninterrupted gesture that starts AND ends on the
// dialog itself. The helper is executed against a stub dialog so the assertions
// cover real event sequences rather than source text.
const vm = require("node:vm"),
  fs = require("node:fs"),
  path = require("node:path"),
  assert = require("node:assert/strict"),
  { test } = require("node:test");

const root = path.resolve(__dirname, "../..");
const source = fs.readFileSync(path.join(root, "app/static/js/lanmatrix/ui.js"), "utf8");

// Extracts a top-level function declaration by brace depth.
function functionSource(name) {
  const start = source.indexOf("function " + name + "(");
  assert.ok(start >= 0, name + " must exist in ui.js");
  const open = source.indexOf("{", start);
  let depth = 0;
  for (let i = open; i < source.length; i++) {
    if (source[i] === "{") depth++;
    else if (source[i] === "}") {
      depth--;
      if (depth === 0) return source.slice(start, i + 1);
    }
  }
  throw new Error("unbalanced braces in " + name);
}

function makeDialog() {
  const listeners = new Map();
  return {
    listeners,
    addEventListener(type, fn) {
      if (!listeners.has(type)) listeners.set(type, []);
      listeners.get(type).push(fn);
    },
    fire(type, event) {
      // A real pointer event always carries `button`; the cases below describe
      // primary-button gestures unless they say otherwise.
      (listeners.get(type) || []).forEach((fn) => fn(Object.assign({ button: 0 }, event)));
    },
  };
}

function harness() {
  const dialog = makeDialog();
  let dismissals = 0;
  const context = { onDismiss: () => dismissals++ };
  vm.runInNewContext(functionSource("wireBackdropDismiss"), context);
  context.wireBackdropDismiss(dialog, context.onDismiss);
  return { dialog, dismissals: () => dismissals };
}

const panel = { name: "panel" };

test("a press and release both on the backdrop dismisses", () => {
  const { dialog, dismissals } = harness();
  dialog.fire("pointerdown", { target: dialog, pointerId: 1 });
  dialog.fire("pointerup", { target: dialog, pointerId: 1 });
  assert.equal(dismissals(), 1);
});

test("a gesture that starts inside the panel does not dismiss", () => {
  const { dialog, dismissals } = harness();
  dialog.fire("pointerdown", { target: panel, pointerId: 1 });
  dialog.fire("pointerup", { target: dialog, pointerId: 1 });
  assert.equal(dismissals(), 0);
});

test("a gesture that ends inside the panel does not dismiss", () => {
  const { dialog, dismissals } = harness();
  dialog.fire("pointerdown", { target: dialog, pointerId: 1 });
  dialog.fire("pointerup", { target: panel, pointerId: 1 });
  assert.equal(dismissals(), 0);
});

test("a second pointer cannot continue the first pointer's press", () => {
  const { dialog, dismissals } = harness();
  dialog.fire("pointerdown", { target: dialog, pointerId: 1 });
  dialog.fire("pointerup", { target: dialog, pointerId: 2 });
  assert.equal(dismissals(), 0);
});

test("a cancelled gesture does not dismiss on release", () => {
  const { dialog, dismissals } = harness();
  dialog.fire("pointerdown", { target: dialog, pointerId: 1 });
  dialog.fire("pointercancel", { target: dialog, pointerId: 1 });
  dialog.fire("pointerup", { target: dialog, pointerId: 1 });
  assert.equal(dismissals(), 0);
});

test("the next clean backdrop gesture still dismisses", () => {
  const { dialog, dismissals } = harness();
  dialog.fire("pointerdown", { target: panel, pointerId: 1 });
  dialog.fire("pointerup", { target: dialog, pointerId: 1 });
  dialog.fire("pointerdown", { target: dialog, pointerId: 2 });
  dialog.fire("pointerup", { target: dialog, pointerId: 2 });
  assert.equal(dismissals(), 1);
});

test("the dialog no longer dismisses from a bare click on the dialog", () => {
  const { dialog, dismissals } = harness();
  dialog.fire("click", { target: dialog });
  assert.equal(dismissals(), 0);
});

test("ui.js wires the helper instead of a click handler", () => {
  assert.match(source, /wireBackdropDismiss\(dlg, reject\)/);
  assert.doesNotMatch(source, /addEventListener\("click", function \(e\) \{\s*if \(e\.target === dlg\)/);
});
test("a secondary button on the backdrop does not dismiss", () => {
  // `click` only fires for the primary button, so the old listener ignored a
  // right-click, middle-click or pen barrel press. Pointer events fire for
  // every button and must be filtered the same way.
  const { dialog, dismissals } = harness();
  const panel = { tag: "form" };

  dialog.fire("pointerdown", { target: dialog, pointerId: 1, button: 2 });
  dialog.fire("pointerup", { target: dialog, pointerId: 1, button: 2 });
  assert.equal(dismissals(), 0, "a right-click on the backdrop must not cancel");

  dialog.fire("pointerdown", { target: dialog, pointerId: 1, button: 1 });
  dialog.fire("pointerup", { target: dialog, pointerId: 1, button: 1 });
  assert.equal(dismissals(), 0, "a middle-click on the backdrop must not cancel");

  // A right-click mid-gesture must not swallow the armed primary press.
  dialog.fire("pointerdown", { target: dialog, pointerId: 7, button: 0 });
  dialog.fire("pointerup", { target: panel, pointerId: 8, button: 2 });
  dialog.fire("pointerup", { target: dialog, pointerId: 7, button: 0 });
  assert.equal(dismissals(), 1, "the primary gesture still completes");
});
