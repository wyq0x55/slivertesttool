const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const root = path.resolve(__dirname, "../..");
const read = (file) => fs.readFileSync(path.join(root, file), "utf8");

const UI_SOURCE = read("app/static/js/lanmatrix/ui.js");
const TEMPLATE = read("app/templates/lanmatrix/base_lm.html");
const CSS = read("app/static/css/lanmatrix.css");

// Minimal element shim. `textContent` is a tracked accessor so a test can prove
// the announcer is cleared before the message is written; `hidden` is a plain
// property because the visual strip's reveal is what we assert on.
function makeNode(id) {
  const node = {
    id,
    textContentWrites: [],
    _textContent: "",
    className: "",
    hidden: true,
  };
  Object.defineProperty(node, "textContent", {
    configurable: true,
    get() { return node._textContent; },
    set(value) { node.textContentWrites.push(value); node._textContent = value; },
  });
  return node;
}

// Run the production ui.js against the given elements. Timers are captured
// instead of scheduled so tests never wait 3.2 s and can inspect the delay.
function makeHarness(nodes = {}) {
  const timers = [];
  const document = {
    getElementById: (id) => nodes[id] || null,
    createElement: () => ({ showModal() {} }),
  };
  const window = {};
  const context = vm.createContext({
    window,
    document,
    setTimeout: (fn, ms) => { timers.push({ fn, ms }); return timers.length; },
    clearTimeout: () => {},
  });
  vm.runInContext(UI_SOURCE, context, { filename: "app/static/js/lanmatrix/ui.js" });
  return { window, document, timers, toast: window.LMUI.toast };
}

test("base template declares a polite, atomic live region beside the toast", () => {
  const tag = TEMPLATE.match(/<div\b[^>]*\bid="lm-toast-live"[^>]*>/);
  assert.ok(tag, "base_lm.html must declare #lm-toast-live");
  assert.match(tag[0], /\brole="status"/, "#lm-toast-live must be a status region");
  assert.match(tag[0], /\baria-live="polite"/, "#lm-toast-live must be politely live");
  assert.match(tag[0], /\baria-atomic="true"/, "#lm-toast-live must announce atomically");
  assert.doesNotMatch(tag[0], /\shidden(?=[\s>])/, "#lm-toast-live must stay in the accessibility tree");

  // The visual strip keeps its own contract: hidden until a message arrives.
  const strip = TEMPLATE.match(/<div\b[^>]*\bid="lm-toast"[^>]*>/);
  assert.ok(strip, "base_lm.html must keep #lm-toast");
  assert.match(strip[0], /\shidden(?=[\s>])/, "#lm-toast keeps starting hidden");
});

test("the live region is clipped visually rather than hidden from assistive tech", () => {
  const rule = CSS.match(/#lm-toast-live\s*\{([^}]*)\}/);
  assert.ok(rule, "lanmatrix.css must style #lm-toast-live");
  const body = rule[1];
  assert.match(body, /position:\s*absolute/);
  assert.match(body, /width:\s*1px/);
  assert.match(body, /height:\s*1px/);
  assert.match(body, /overflow:\s*hidden/);
  assert.match(body, /clip-path:\s*inset\(50%\)/);
  assert.doesNotMatch(body, /display\s*:\s*none/, "display:none removes the live region");
  assert.doesNotMatch(body, /visibility\s*:\s*hidden/, "visibility:hidden removes the live region");
});

test("toast mirrors the message into the live region and shows the visual strip", () => {
  const strip = makeNode("lm-toast");
  const live = makeNode("lm-toast-live");
  const harness = makeHarness({ "lm-toast": strip, "lm-toast-live": live });

  harness.toast("已保存", true);

  assert.equal(live.textContent, "已保存");
  assert.deepEqual(live.textContentWrites, ["", "已保存"]);
  assert.equal(strip.textContent, "已保存");
  assert.equal(strip.hidden, false);
  assert.equal(strip.className, "lm-toast lm-ok");
  assert.equal(harness.timers.length, 1);
  assert.equal(harness.timers[0].ms, 3200);
});

test("a repeated identical message is cleared first so it announces again", () => {
  const strip = makeNode("lm-toast");
  const live = makeNode("lm-toast-live");
  const harness = makeHarness({ "lm-toast": strip, "lm-toast-live": live });

  harness.toast("已保存", true);
  harness.toast("已保存", true);

  // The empty write between the two identical strings is what makes the
  // second message a fresh mutation for assistive technology.
  assert.deepEqual(live.textContentWrites, ["", "已保存", "", "已保存"]);
  assert.equal(live.textContent, "已保存");
});

test("toast does not throw when the visual strip is absent", () => {
  const live = makeNode("lm-toast-live");
  const withLive = makeHarness({ "lm-toast-live": live });
  assert.doesNotThrow(() => withLive.toast("已保存", true));

  const withNeither = makeHarness({});
  assert.doesNotThrow(() => withNeither.toast("失败", false));
});

test("page scripts delegate toast to the shared LMUI implementation", () => {
  const scripts = ["admin_console.js", "admin_db.js", "editor.js"];
  for (const name of scripts) {
    const source = read(`app/static/js/lanmatrix/${name}`);
    const start = source.indexOf("function toast(msg, ok) {");
    assert.ok(start >= 0, `${name} must define toast(msg, ok)`);
    const end = source.indexOf("\n  }", start);
    assert.ok(end > start, `${name} toast body boundary not found`);
    const body = source.slice(start, end);
    assert.match(body, /LMUI\.toast\(msg, ok\)/, `${name} must delegate to LMUI.toast`);
    assert.doesNotMatch(body, /getElementById\("lm-toast"\)|\$\("lm-toast"\)/,
      `${name} must not write #lm-toast directly anymore`);
  }
});