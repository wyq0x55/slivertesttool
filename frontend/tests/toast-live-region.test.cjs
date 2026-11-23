/* Toast live region (WCAG 4.1.3 Status Messages).
 *
 * Every toast in the signed-in console was silent to assistive tech: #lm-toast
 * starts [hidden], and un-hiding it in the same frame as its text change gives
 * the screen reader no container that was already in the page. The fix is the
 * shape the persistent-toast guide prescribes: a live region that is present on
 * load, written together with the visible strip.
 *
 * These checks are static + scripted (no browser): they lock
 *  - the template structure: a permanently mounted, visually hidden, named
 *    region under the same render condition as the visible toast,
 *  - the stylesheet rule keeps the node in the accessibility tree,
 *  - ui.js writes both hosts, announces a repeated identical message again,
 *  - the three page-local toast() copies announce through LMUI.announce and
 *    still work where ui.js is not loaded (the collab process).
 */
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const root = path.resolve(__dirname, "../..");
// Normalise CRLF: the checkout uses CRLF, the markers below assume LF.
const read = (file) =>
  fs.readFileSync(path.join(root, file), "utf8").replace(/\r\n/g, "\n");

// Jinja comments may mention tags in prose; drop them before scanning markup.
const template = read("app/templates/lanmatrix/base_lm.html").replace(
  /\{#[\s\S]*?#\}/g,
  ""
);
const uiSource = read("app/static/js/lanmatrix/ui.js");

const VISIBLE_TOAST = '<div id="lm-toast" class="lm-toast" hidden></div>';
const NBSP = String.fromCharCode(160);
// What was announced, with the repeat-announcement separator removed.
const spoken = (text) => text.split(NBSP).join("");

/* --- helpers ------------------------------------------------------------ */

// Extracts a top-level function declaration by brace depth (same technique as
// ui-backdrop-dismiss.test.cjs).
function functionSource(source, name) {
  const start = source.indexOf("function " + name + "(");
  assert.ok(start >= 0, name + " must exist in " + source.slice(0, 40));
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

function cssBlocks(source) {
  const blocks = [];
  const re = /([^{}]+)\{([^{}]*)\}/g;
  let match;
  while ((match = re.exec(source))) {
    blocks.push({
      selectors: match[1].split(",").map((s) => s.trim()).filter(Boolean),
      body: match[2],
    });
  }
  return blocks;
}

function cssRuleFor(selector) {
  for (const file of [
    "app/static/css/lanmatrix.css",
    "app/static/css/design-system.css",
  ]) {
    const source = read(file).replace(/\/\*[\s\S]*?\*\//g, "");
    const block = cssBlocks(source).find((b) => b.selectors.includes(selector));
    if (block) return block.body;
  }
  assert.fail(`expected a CSS rule whose selector list includes ${selector}`);
}

function startTag(id) {
  const match = template.match(new RegExp('<[a-z]+[^>]*\\bid="' + id + '"[^>]*>'));
  assert.ok(match, `#${id} must exist in base_lm.html`);
  return match[0];
}

function attr(tag, name) {
  const match = tag.match(new RegExp('\\s' + name + '="([^"]*)"'));
  return match ? match[1] : null;
}

function hasAttr(tag, name) {
  return new RegExp("\\s" + name + "(\\s|=|/?>)").test(tag);
}

// Minimal element shim: the fields the toast/announce code touches.
function makeNode(id) {
  return { id, textContent: "", className: "", hidden: true };
}

// Runs the real ui.js in a VM so LMUI.toast / LMUI.announce are exercised, not
// grepped. `nodeMap` lets a test start with the live region missing.
function uiHarness(nodeMap) {
  const nodes =
    nodeMap === undefined
      ? { "lm-toast": makeNode("lm-toast"), "lm-toast-live": makeNode("lm-toast-live") }
      : nodeMap;
  const timers = [];
  const sandbox = {
    document: {
      getElementById: (id) => nodes[id] || null,
      // Only reached when <dialog> is unsupported, which it never is here.
      createElement: () => ({ setAttribute() {}, appendChild() {}, addEventListener() {} }),
      activeElement: null,
    },
    setTimeout: (fn, ms) => {
      timers.push({ fn, ms });
      return timers.length;
    },
    clearTimeout: () => {},
  };
  sandbox.window = sandbox;
  vm.createContext(sandbox);
  vm.runInContext(uiSource, sandbox, { filename: "ui.js" });
  return { window: sandbox.window, nodes, timers };
}

// Loads one page-local toast() copy (executed as source, not pattern-matched).
function pageHarness(file) {
  const source = functionSource(read(file), "toast");
  const visible = makeNode("lm-toast");
  const nodes = { "lm-toast": visible };
  const calls = [];
  const timers = [];
  const sandbox = {
    document: { getElementById: (id) => nodes[id] || null },
    $: (id) => nodes[id] || null,
    setTimeout: (fn, ms) => {
      timers.push({ fn, ms });
      return timers.length;
    },
    clearTimeout: () => {},
    LMUI: { announce: (msg) => calls.push(msg) },
  };
  sandbox.window = sandbox;
  const toast = vm.runInNewContext(source + "\n;toast;", sandbox);
  return { toast, visible, calls, timers, sandbox };
}

/* --- template ----------------------------------------------------------- */

test("the visible toast host keeps its shape", () => {
  assert.ok(
    template.includes(VISIBLE_TOAST),
    "the visible #lm-toast must stay [hidden] at load and keep its class"
  );
  assert.equal(hasAttr(startTag("lm-toast"), "aria-live"), false);
});

test("a live region is mounted on load, next to the visible toast", () => {
  assert.equal(
    template.split('id="lm-toast-live"').length - 1,
    1,
    "exactly one #lm-toast-live host"
  );

  const tag = startTag("lm-toast-live");
  assert.equal(attr(tag, "aria-live"), "assertive");
  assert.equal(attr(tag, "aria-atomic"), "true");
  // Named region: the guide asks for a container that can be identified with a
  // screen reader.
  assert.equal(attr(tag, "role"), "region");
  assert.ok((attr(tag, "aria-label") || "").trim(), "the region needs an accessible name");
  assert.equal(attr(tag, "class"), "lm-visually-hidden");
  // It must not be hidden: content changes inside a hidden subtree are not
  // announced.
  assert.equal(hasAttr(tag, "hidden"), false, "the live region must never be [hidden]");
  assert.equal(hasAttr(tag, "style"), false, "visibility belongs in the stylesheet");
});

test("the live region renders exactly where the visible toast does", () => {
  const visibleAt = template.indexOf(VISIBLE_TOAST);
  const liveAt = template.indexOf('id="lm-toast-live"');
  assert.ok(visibleAt >= 0 && liveAt > visibleAt, "live region follows the visible host");

  // No Jinja control flow between them: both are rendered or dropped together.
  assert.equal(
    template.slice(visibleAt + VISIBLE_TOAST.length, liveAt).includes("{%"),
    false,
    "#lm-toast and #lm-toast-live must share the same render condition"
  );

  const nextScript = template.indexOf("<script", liveAt);
  const tagEnd = liveAt + startTag("lm-toast-live").length;
  assert.ok(nextScript === -1 || tagEnd < nextScript, "both hosts precede the scripts");
});

test("the visually-hidden utility stays in the accessibility tree", () => {
  const body = cssRuleFor(".lm-visually-hidden");
  assert.match(body, /position:\s*absolute/);
  assert.match(body, /width:\s*1px/);
  assert.doesNotMatch(body, /display:\s*none/);
  assert.doesNotMatch(body, /visibility:\s*hidden/);
});

/* --- shared toast primitive --------------------------------------------- */

test("LMUI.toast writes the visible strip and the live region", () => {
  const h = uiHarness();
  h.window.LMUI.toast("更新失败", false);

  const visible = h.nodes["lm-toast"];
  const live = h.nodes["lm-toast-live"];
  assert.equal(visible.textContent, "更新失败");
  assert.equal(visible.className, "lm-toast lm-err");
  assert.equal(visible.hidden, false);
  assert.equal(spoken(live.textContent), "更新失败");
  assert.equal(h.timers.length, 1);
  assert.equal(h.timers[0].ms, 3200);

  h.timers[0].fn();
  assert.equal(visible.hidden, true, "the visible strip still auto-hides");
  assert.equal(live.textContent === "", false, "the announcement survives the hide");
});

test("LMUI.toast keeps the success styling", () => {
  const h = uiHarness();
  h.window.LMUI.toast("已保存", true);
  assert.equal(h.nodes["lm-toast"].className, "lm-toast lm-ok");
  assert.equal(spoken(h.nodes["lm-toast-live"].textContent), "已保存");
});

test("a repeated identical message still mutates the live region", () => {
  const h = uiHarness();
  const live = h.nodes["lm-toast-live"];
  h.window.LMUI.announce("已通过");
  const first = live.textContent;
  h.window.LMUI.announce("已通过");
  const second = live.textContent;

  assert.equal(spoken(first), "已通过");
  assert.equal(spoken(second), "已通过");
  assert.notEqual(first, second, "assistive tech only speaks on mutation");
});

test("LMUI.toast and LMUI.announce are no-ops without their hosts", () => {
  const h = uiHarness({});
  assert.doesNotThrow(() => h.window.LMUI.toast("x", true));
  assert.doesNotThrow(() => h.window.LMUI.announce("x"));
  assert.equal(typeof h.window.LMUI.announce, "function");
});

/* --- page-local copies --------------------------------------------------- */

const COPIES = [
  "app/static/js/lanmatrix/admin_console.js",
  "app/static/js/lanmatrix/admin_db.js",
  "app/static/js/lanmatrix/editor.js",
];

for (const file of COPIES) {
  test(`${file}: toast announces through LMUI.announce`, () => {
    const h = pageHarness(file);
    h.toast("保存失败", false);

    assert.equal(h.visible.textContent, "保存失败");
    assert.equal(h.visible.className, "lm-toast lm-err");
    assert.equal(h.visible.hidden, false);
    assert.deepEqual(h.calls, ["保存失败"]);
    assert.equal(h.timers.length, 1);
    assert.equal(h.timers[0].ms, 3200);

    h.timers[0].fn();
    assert.equal(h.visible.hidden, true);
  });

  test(`${file}: toast works where LMUI is not loaded`, () => {
    const h = pageHarness(file);
    h.sandbox.LMUI = undefined;
    assert.doesNotThrow(() => h.toast("仅系统管理员可访问", false));
    assert.equal(h.visible.textContent, "仅系统管理员可访问");
    assert.equal(h.visible.className, "lm-toast lm-err");
  });
}