/* SBS 编辑器状态行：polite live region（WCAG 4.1.3 Status Messages）。
 *
 * Guide `accessibility`（live regions / status messages）:
 *  - "Announce dynamic errors via live regions."
 *  - Live Region Urgency Table: "Standard | Toast/Banner | polite | Announces at
 *    next graceful break | Search results, 'Saved' status"; assertive is for
 *    "Critical ... Interrupts immediately, clears speech queue".
 * Guide `persistent-toast-notifications`:
 *  - "make the container a region, with an accessible name, so that it can be
 *    easily identified using a screen reader."
 *  - "Use an ARIA live region, ensuring that the container is present in the
 *    page on load, and then add the messages to the live region container when
 *    relevant."
 *
 * These are static checks: no browser or screen reader is involved, so they
 * lock structure and write behaviour, not real announcement timing.
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

const template = read("app/templates/lanmatrix/models.html");
const js = read("app/static/js/lanmatrix/sbs_editor.js");
const css = read("app/static/css/lanmatrix.css").replace(/\/\*[\s\S]*?\*\//g, "");

const escapeRe = (s) => s.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");

// The full opening tag of the single <div> carrying this id.
function tagFor(id, html) {
  const found = [
    ...html.matchAll(new RegExp(`<div\\b[^>]*\\bid="${escapeRe(id)}"[^>]*>`, "g")),
  ];
  assert.equal(found.length, 1, `expected exactly one <div id="${id}">`);
  return found[0][0];
}

function attrsOf(tag) {
  return new Map(
    [...tag.matchAll(/([a-zA-Z-]+)="([^"]*)"/g)].map((m) => [m[1], m[2]]),
  );
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
  const block = cssBlocks(css).find((b) => b.selectors.includes(selector));
  assert.ok(block, `expected a CSS rule whose selector list includes ${selector}`);
  return block.body;
}

// Pull `function setStatus(...) { ... }` out of the IIFE so its real body can
// run against an element shim. Braces inside strings/comments are ignored.
function extractFunction(source, signature) {
  const start = source.indexOf(signature);
  assert.ok(start >= 0, `${signature} must exist`);
  const open = source.indexOf("{", start);
  assert.ok(open > start, `${signature} must have a body`);
  let depth = 0;
  let quote = null;
  let lineComment = false;
  let blockComment = false;
  for (let i = open; i < source.length; i += 1) {
    const ch = source[i];
    const next = source[i + 1];
    if (lineComment) {
      if (ch === "\n") { lineComment = false; }
    } else if (blockComment) {
      if (ch === "*" && next === "/") { blockComment = false; i += 1; }
    } else if (quote) {
      if (ch === "\\") { i += 1; } else if (ch === quote) { quote = null; }
    } else if (ch === "/" && next === "/") {
      lineComment = true; i += 1;
    } else if (ch === "/" && next === "*") {
      blockComment = true; i += 1;
    } else if (ch === '"' || ch === "'" || ch === "`") {
      quote = ch;
    } else if (ch === "{") {
      depth += 1;
    } else if (ch === "}") {
      depth -= 1;
      if (depth === 0) { return source.slice(start, i + 1); }
    }
  }
  assert.fail(`unterminated body for ${signature}`);
}

function loadSetStatus(getById) {
  const body = extractFunction(js, "function setStatus(msg, kind) {");
  const factory = vm.runInNewContext(`(function ($) { return ${body}; })`, {});
  return factory(getById);
}

// Element shim that records every textContent assignment: a live region only
// reacts to a change, so the tests count writes, not just the final value.
function statusElement() {
  const el = {
    className: "",
    _text: "",
    writes: [],
    get textContent() { return el._text; },
    set textContent(value) { el._text = value; el.writes.push(value); },
  };
  return el;
}

test("the SBS status container is a polite live region", () => {
  const tag = tagFor("lm-sbs-status", template);
  assert.match(tag, /\brole="status"/, "role=status makes it a polite live region");
  assert.match(tag, /\baria-live="polite"/);
  assert.match(tag, /\baria-atomic="true"/, "the whole one-line message is announced as one unit");
  assert.doesNotMatch(tag, /\brole="alert"/);
  assert.doesNotMatch(tag, /\baria-live="assertive"/);
});

test("the live region has an accessible name", () => {
  const attrs = attrsOf(tagFor("lm-sbs-status", template));
  assert.ok(
    (attrs.get("aria-label") || "").trim() ||
      (attrs.get("aria-labelledby") || "").trim(),
    "role=status needs an accessible name; the nearby heading is not associated",
  );
});

test("the status line stays mounted and visible", () => {
  const tag = tagFor("lm-sbs-status", template);
  assert.doesNotMatch(tag, /\shidden(?=[\s=>])/, "must not carry a hidden attribute");
  assert.doesNotMatch(tag, /\baria-hidden=/, "must not be hidden from assistive tech");
  assert.equal(
    attrsOf(tag).get("class"),
    "lm-sbs-status",
    "no visually-hidden class may be added",
  );

  // The container must be inside the SBS dialog, present in the page on load.
  const modalStart = template.indexOf('id="lm-sbs-modal"');
  const statusAt = template.indexOf('id="lm-sbs-status"');
  const modalEnd = template.indexOf("</dialog>", modalStart);
  assert.ok(modalStart >= 0, "the SBS dialog must exist");
  assert.ok(
    statusAt > modalStart && statusAt < modalEnd,
    "the live region must sit inside the SBS dialog and exist on load",
  );
});

test("the status line is not visually hidden by CSS", () => {
  const body = cssRuleFor(".lm-sbs-status");
  assert.doesNotMatch(body, /\bdisplay\s*:\s*none\b/);
  assert.doesNotMatch(body, /\bvisibility\s*:\s*hidden\b/);
  assert.doesNotMatch(body, /\bopacity\s*:\s*0\b/);
  assert.doesNotMatch(body, /\bclip(?:-path)?\s*:/);
  assert.doesNotMatch(body, /\bposition\s*:\s*absolute\b/);
});

test("the ok/warn/err status classes remain styled", () => {
  const blocks = cssBlocks(css);
  for (const kind of ["ok", "warn", "err"]) {
    const selector = `.lm-sbs-status.lm-sbs-${kind}`;
    assert.ok(
      blocks.some((b) => b.selectors.includes(selector)),
      `${selector} rule must remain`,
    );
  }
});

test("setStatus still writes the message and preserves the class names", () => {
  const el = statusElement();
  const setStatus = loadSetStatus((id) => (id === "lm-sbs-status" ? el : null));

  setStatus("保存失败：该文件已被他人修改", "err");
  assert.equal(el.textContent, "保存失败：该文件已被他人修改");
  assert.equal(el.className, "lm-sbs-status lm-sbs-err");

  setStatus("已载入服务器版本", "ok");
  assert.equal(el.textContent, "已载入服务器版本");
  assert.equal(el.className, "lm-sbs-status lm-sbs-ok");

  setStatus("保存中…", "warn");
  assert.equal(el.textContent, "保存中…");
  assert.equal(el.className, "lm-sbs-status lm-sbs-warn");

  setStatus("无改动");
  assert.equal(el.className, "lm-sbs-status", "no kind means no kind class is left behind");

  setStatus(null);
  assert.equal(el.textContent, "", "a missing message clears the status line");
  assert.equal(el.className, "lm-sbs-status");
});

test("a repeated identical message is cleared and rewritten so it announces", () => {
  const el = statusElement();
  const setStatus = loadSetStatus((id) => (id === "lm-sbs-status" ? el : null));

  setStatus("无改动");
  assert.deepEqual(el.writes, ["无改动"]);

  setStatus("无改动");
  assert.deepEqual(
    el.writes,
    ["无改动", "", "无改动"],
    "a repeated identical message must produce a change (clear, then rewrite)",
  );

  // A different message is written once, with no needless clear.
  setStatus("已载入服务器版本", "ok");
  assert.deepEqual(el.writes, ["无改动", "", "无改动", "已载入服务器版本"]);
});

test("setStatus keeps its missing-element guard", () => {
  const body = extractFunction(js, "function setStatus(msg, kind) {");
  assert.match(body, /if \(!el\) \{ return; \}/);
  const setStatus = loadSetStatus(() => null);
  assert.doesNotThrow(() => setStatus("载入失败：x", "err"));
});

test("failure messages still flow through the live region", () => {
  for (const call of [
    'setStatus("载入失败：" + ex.message, "err")',
    'setStatus("保存失败：该文件已被他人修改", "err")',
    'setStatus("保存失败：" + ex.message, "err")',
  ]) {
    assert.ok(js.includes(call), `${call} must remain unchanged`);
  }
});

test("the controller never hides the status line", () => {
  const lookups = [...js.matchAll(/\$\("lm-sbs-status"\)/g)];
  assert.equal(lookups.length, 1, "the status element is looked up only once, in setStatus");
  assert.doesNotMatch(js, /\bel\.hidden\b/, "setStatus must not hide the status line");
  assert.doesNotMatch(js, /aria-hidden/, "this controller must not hide content from assistive tech");
});