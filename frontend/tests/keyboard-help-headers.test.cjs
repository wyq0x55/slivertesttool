const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

// editor.js is a browser IIFE that touches the whole editor on load, and
// frontend/node_modules is not present in every checkout, so the real module
// cannot be imported here. Instead `renderHelp` is lifted out of the source and
// run against a tiny fake DOM: the assertions below then inspect the exact
// nodes the real DOM would receive, not the spelling of the source.
const root = path.resolve(__dirname, "../..");
const editorSource = fs.readFileSync(path.join(root, "app/static/js/lanmatrix/editor.js"), "utf8");
const designSystem = fs
  .readFileSync(path.join(root, "app/static/css/design-system.css"), "utf8")
  .replace(/\/\*[\s\S]*?\*\//g, "");

// Returns the index of the `}` that closes the block opened at `openIndex`,
// ignoring braces inside strings and comments.
function matchingBrace(source, openIndex) {
  assert.equal(source[openIndex], "{", "matchingBrace must start on an opening brace");
  let depth = 0;
  let quote = null;
  for (let i = openIndex; i < source.length; i += 1) {
    const ch = source[i];
    if (quote) {
      if (ch === "\\") i += 1;
      else if (ch === quote || ch === "\n") quote = null;
      continue;
    }
    if (ch === '"' || ch === "'" || ch === "`") {
      quote = ch;
      continue;
    }
    if (ch === "/" && source[i + 1] === "/") {
      const end = source.indexOf("\n", i);
      if (end === -1) break;
      i = end;
      continue;
    }
    if (ch === "/" && source[i + 1] === "*") {
      const end = source.indexOf("*/", i + 2);
      assert.notEqual(end, -1, "unterminated block comment in renderHelp");
      i = end + 1;
      continue;
    }
    if (ch === "{") depth += 1;
    else if (ch === "}") {
      depth -= 1;
      if (depth === 0) return i;
    }
  }
  throw new Error("unbalanced braces while extracting renderHelp");
}

function extractRenderHelp(source) {
  const start = source.indexOf("function renderHelp()");
  assert.notEqual(start, -1, "editor.js must define renderHelp()");
  const open = source.indexOf("{", start);
  const close = matchingBrace(source, open);
  return source.slice(start, close + 1);
}

class FakeElement {
  constructor(tagName) {
    this.tagName = tagName.toUpperCase();
    this.children = [];
    this.attributes = {};
    this.className = "";
    this._text = "";
  }

  setAttribute(name, value) {
    this.attributes[name.toLowerCase()] = String(value);
  }

  getAttribute(name) {
    return Object.prototype.hasOwnProperty.call(this.attributes, name.toLowerCase())
      ? this.attributes[name.toLowerCase()]
      : null;
  }

  appendChild(child) {
    this.children.push(child);
    return child;
  }

  replaceChildren(...nodes) {
    this.children = nodes;
  }

  set textContent(value) {
    this._text = String(value);
    this.children = [];
  }

  get textContent() {
    return this._text + this.children.map((child) => child.textContent).join("");
  }
}

function runRenderHelp() {
  const bodies = {
    "lm-help-keys": new FakeElement("tbody"),
    "lm-help-gestures": new FakeElement("tbody"),
  };
  const document = {
    getElementById: (id) => bodies[id] || null,
    createElement: (tag) => new FakeElement(tag),
    createTextNode: (text) => {
      const node = new FakeElement("#text");
      node.textContent = text;
      return node;
    },
  };
  const context = vm.createContext({
    document,
    helpDialog: {},
    global_LMRowTools: () => true,
    LMRowTools: {
      shortcutRows: () => [
        { keys: ["Ctrl", "S"], scope: "编辑器", desc: "保存" },
        { keys: ["?"], scope: "编辑器", desc: "打开帮助" },
      ],
      GESTURES: [
        { what: "双击单元格", desc: "开始编辑" },
        { what: "拖拽列宽", desc: "调整列宽" },
      ],
    },
  });
  const renderHelp = vm.runInContext(`(${extractRenderHelp(editorSource)})`, context);
  renderHelp();
  return { keys: bodies["lm-help-keys"], gestures: bodies["lm-help-gestures"] };
}

function cellAt(row, index) {
  const cell = row.children[index];
  assert.ok(cell, `row has no cell at index ${index}`);
  return cell;
}

test("shortcut rows label the keys cell with a row header", () => {
  const { keys } = runRenderHelp();
  assert.equal(keys.children.length, 2, "every shortcut row must be rendered");

  const row = keys.children[0];
  assert.equal(row.tagName, "TR");
  assert.equal(row.children.length, 3, "a shortcut row keeps its three cells");

  const header = cellAt(row, 0);
  assert.equal(header.tagName, "TH", "the keys cell must be a row header");
  assert.equal(header.getAttribute("scope"), "row");
  assert.equal(header.className, "lm-help-keys");
  assert.equal(
    row.children.filter((child) => child.tagName === "TH").length,
    1,
    "only the first cell of a shortcut row is a header",
  );

  const kbds = header.children.filter((child) => child.tagName === "KBD");
  assert.deepEqual(kbds.map((kbd) => kbd.textContent), ["Ctrl", "S"]);
  assert.equal(header.children[1].textContent, " + ", "keys keep their + separator");

  const scope = cellAt(row, 1);
  assert.equal(scope.tagName, "TD", "the scope cell remains a data cell");
  assert.equal(scope.className, "lm-muted");
  assert.equal(scope.textContent, "编辑器");

  const desc = cellAt(row, 2);
  assert.equal(desc.tagName, "TD", "the description cell remains a data cell");
  assert.equal(desc.textContent, "保存");
});

test("gesture rows label the gesture cell with a row header", () => {
  const { gestures } = runRenderHelp();
  assert.equal(gestures.children.length, 2, "every gesture row must be rendered");

  const row = gestures.children[0];
  assert.equal(row.tagName, "TR");
  assert.equal(row.children.length, 2, "a gesture row keeps its two cells");

  const header = cellAt(row, 0);
  assert.equal(header.tagName, "TH", "the gesture cell must be a row header");
  assert.equal(header.getAttribute("scope"), "row");
  assert.equal(header.textContent, "双击单元格");
  assert.equal(
    row.children.filter((child) => child.tagName === "TH").length,
    1,
    "only the first cell of a gesture row is a header",
  );

  const desc = cellAt(row, 1);
  assert.equal(desc.tagName, "TD", "the gesture description remains a data cell");
  assert.equal(desc.textContent, "开始编辑");
});

// Flat-rule reader in the style used by the other CSS suites: comments are
// stripped above so selector lookups only see real rules.
function flatRules(source) {
  const rules = [];
  const pattern = /([^{}]+)\{([^{}]*)\}/g;
  let match;
  while ((match = pattern.exec(source))) {
    rules.push({
      selectors: match[1].split(",").map((entry) => entry.trim()),
      body: match[2],
    });
  }
  return rules;
}

function bodiesFor(source, selector) {
  const wanted = new RegExp(`^(?:${selector})$`);
  return flatRules(source)
    .filter((rule) => rule.selectors.some((entry) => wanted.test(entry)))
    .map((rule) => rule.body);
}

function assertBodyMatching(bodies, pattern, label) {
  assert.ok(
    bodies.some((body) => pattern.test(body)),
    `expected a help-table rule ${label}`,
  );
}

test("the help-table body-cell rules also cover row headers", () => {
  const shared = bodiesFor(designSystem, "\\.lm-help-table\\s+th");
  assertBodyMatching(shared, /padding:\s*6px 10px 6px 0\s*;/, "with the td padding");
  assertBodyMatching(shared, /vertical-align:\s*top\s*;/, "with the td vertical alignment");
  assertBodyMatching(shared, /font-size:\s*13px\s*;/, "with the td font size");
  assertBodyMatching(shared, /border-bottom:\s*1px solid var\(--line\)\s*;/, "with the td border");

  assertBodyMatching(bodiesFor(designSystem, "\\.lm-help-table\\s+td"), /padding:\s*6px 10px 6px 0\s*;/, "still targeting td");

  assertBodyMatching(
    bodiesFor(designSystem, "\\.lm-help-table\\s+tr:last-child\\s+th"),
    /border-bottom:\s*none\s*;/,
    "for the last row header",
  );
  assertBodyMatching(
    bodiesFor(designSystem, "\\.lm-help-table\\s+tr:last-child\\s+td"),
    /border-bottom:\s*none\s*;/,
    "for the last data cell",
  );
  assertBodyMatching(
    bodiesFor(designSystem, "\\.lm-help-table\\s+th\\.lm-help-keys"),
    /white-space:\s*nowrap\s*;\s*width:\s*1%\s*;/,
    "for the narrow keys column",
  );
  assertBodyMatching(
    bodiesFor(designSystem, "\\.lm-help-table\\s+th\\.lm-muted"),
    /color:\s*var\(--text-3\)\s*;\s*white-space:\s*nowrap\s*;\s*width:\s*1%\s*;/,
    "for the narrow muted column",
  );
});

test("default browser header styling is neutralized inside the help table", () => {
  const thBodies = bodiesFor(designSystem, "\\.lm-help-table\\s+th");
  assertBodyMatching(thBodies, /font-weight:\s*(inherit|normal)\s*;/, "resetting font-weight");
  assertBodyMatching(thBodies, /text-align:\s*(start|left)\s*;/, "resetting text-align");
});