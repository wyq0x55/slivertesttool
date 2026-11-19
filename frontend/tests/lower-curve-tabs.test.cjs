// The curve panel's second tablist (明细: 事件列表 / 判定检查点 / 原始数据) must be
// a complete ARIA tab widget: every tab controls a labelled tabpanel, and arrow
// keys / Home / End move selection with a roving tabindex, exactly like the
// upper 判定结果 / 变量曲线 tabs. task_curves.js is executed against a minimal DOM
// shim so the assertions cover the real handler, not only source text.
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const root = path.resolve(__dirname, "../..");
const template = fs.readFileSync(
  path.join(root, "app/templates/lanmatrix/project_tasks.html"), "utf8");
const source = fs.readFileSync(
  path.join(root, "app/static/js/lanmatrix/task_curves.js"), "utf8");

const NAMES = ["events", "checks", "raw"];

// ---------------------------------------------------------------- template

function tagFor(pattern) {
  const match = template.match(pattern);
  assert.ok(match, `template must contain ${pattern}`);
  return match[0];
}

function attrs(tag) {
  const map = new Map();
  tag.replace(/<[a-z]+/i, "").replace(/\/?>$/, "").replace(
    /([^\s=/]+)(?:\s*=\s*"([^"]*)")?/g,
    (_, name, value) => { map.set(name, value === undefined ? "" : value); return ""; });
  return map;
}

test("each lower tab controls a labelled tabpanel via matching ids", () => {
  for (const name of NAMES) {
    const tab = attrs(tagFor(new RegExp(`<button[^>]*id="cv-lower-tab-${name}"[^>]*>`)));
    const pane = attrs(tagFor(new RegExp(`<div[^>]*id="cv-lower-pane-${name}"[^>]*>`)));

    assert.equal(tab.get("role"), "tab", `${name} tab keeps role=tab`);
    assert.equal(tab.get("data-lower"), name, `${name} tab keeps data-lower`);
    assert.equal(tab.get("aria-controls"), `cv-lower-pane-${name}`,
      `${name} tab points at its pane`);

    assert.equal(pane.get("role"), "tabpanel", `${name} pane is a tabpanel`);
    assert.equal(pane.get("data-pane"), name, `${name} pane keeps data-pane`);
    assert.equal(pane.get("aria-labelledby"), `cv-lower-tab-${name}`,
      `${name} pane is labelled by its tab`);

    // Initial visibility still matches the initial selection.
    assert.equal(pane.has("hidden"), name !== "events",
      `${name} pane starts ${name === "events" ? "visible" : "hidden"}`);
  }
});

test("the roving tabindex starts on the selected tab only", () => {
  for (const name of NAMES) {
    const tab = attrs(tagFor(new RegExp(`<button[^>]*id="cv-lower-tab-${name}"[^>]*>`)));
    assert.equal(tab.get("aria-selected"), String(name === "events"));
    assert.equal(tab.has("tabindex") ? tab.get("tabindex") : "0",
      name === "events" ? "0" : "-1");
  }
});

// ---------------------------------------------------------------- behaviour

function matches(node, selector) {
  if (!node) return false;
  if (selector.startsWith("[") && selector.endsWith("]")) {
    const [name, value] = selector.slice(1, -1).split("=");
    return value === undefined
      ? node.getAttribute(name) !== null
      : node.getAttribute(name) === value.replace(/^"|"$/g, "");
  }
  return false;
}

function makeElement(id, props) {
  return Object.assign({
    id,
    hidden: false,
    tabIndex: 0,
    dataset: {},
    attributes: new Map(),
    listeners: new Map(),
    parent: null,
    textContent: "",
    innerHTML: "",
    value: "",
    checked: false,
    classList: { contains() { return false; }, add() {}, remove() {} },
    setAttribute(name, value) { this.attributes.set(name, String(value)); },
    getAttribute(name) { return this.attributes.has(name) ? this.attributes.get(name) : null; },
    addEventListener(type, handler) {
      if (!this.listeners.has(type)) this.listeners.set(type, []);
      this.listeners.get(type).push(handler);
    },
    focus() { activeDocument.activeElement = this; },
    click() { fire(this, "click"); },
    querySelectorAll() { return []; },
    closest() { return null; },
    contains(node) { return node === this; },
  }, props);
}

let activeDocument = null;

function fire(target, type, props) {
  const event = Object.assign({
    type,
    target,
    defaultPrevented: false,
    preventDefault() { this.defaultPrevented = true; },
    stopPropagation() {},
  }, props);
  for (const handler of target.listeners.get(type) || []) handler(event);
  return event;
}

function load() {
  const tabs = NAMES.map(name => makeElement(`cv-lower-tab-${name}`, {
    dataset: { lower: name },
    role: "tab",
  }));
  tabs[0].setAttribute("aria-selected", "true");
  for (const tab of tabs.slice(1)) {
    tab.setAttribute("aria-selected", "false");
    tab.tabIndex = -1;
  }
  const panes = NAMES.map((name, i) => makeElement(`cv-lower-pane-${name}`, {
    dataset: { pane: name },
    hidden: i !== 0,
  }));

  const elements = new Map();
  const document = {
    activeElement: null,
    listeners: new Map(),
    body: makeElement("body"),
    documentElement: makeElement("html"),
    getElementById(id) {
      if (!elements.has(id)) elements.set(id, makeElement(id));
      return elements.get(id);
    },
    querySelectorAll(selector) {
      if (selector === "[data-lower]") return tabs;
      if (selector === "[data-pane]") return panes;
      return [];
    },
    createElement(tag) { return makeElement(tag, { tagName: tag.toUpperCase() }); },
    addEventListener(type, handler) {
      if (!this.listeners.has(type)) this.listeners.set(type, []);
      this.listeners.get(type).push(handler);
    },
  };
  document.activeElement = document.body;
  activeDocument = document;

  const window = {
    LMCurveRender: { fmtValue: String, render() {}, canvasPainter() { return {}; }, svgPainter() { return {}; } },
    addEventListener() {},
    devicePixelRatio: 1,
  };
  class Observer { observe() {} disconnect() {} }

  vm.runInContext(source, vm.createContext({
    window,
    document,
    location: { href: "http://localhost/lanmatrix/projects/1/tasks/x?cv=1" },
    navigator: { clipboard: { writeText: async () => {} } },
    console,
    setTimeout,
    requestAnimationFrame: callback => setTimeout(callback, 0),
    getComputedStyle: () => ({ getPropertyValue: () => "" }),
    ResizeObserver: Observer,
    MutationObserver: Observer,
  }), { filename: "task_curves.js" });

  return { document, tabs, panes };
}

const selected = tabs => tabs.find(tab => tab.getAttribute("aria-selected") === "true");
const byName = (tabs, name) => tabs.find(tab => tab.dataset.lower === name);
const paneFor = (panes, name) => panes.find(pane => pane.dataset.pane === name);

function assertSelection({ tabs, panes }, name, message) {
  for (const tab of tabs) {
    const on = tab.dataset.lower === name;
    assert.equal(tab.getAttribute("aria-selected"), String(on), `${message}: aria-selected on ${tab.dataset.lower}`);
    assert.equal(tab.tabIndex, on ? 0 : -1, `${message}: tabindex on ${tab.dataset.lower}`);
  }
  for (const pane of panes) {
    assert.equal(pane.hidden, pane.dataset.pane !== name, `${message}: hidden on ${pane.dataset.pane}`);
  }
}

test("ArrowRight moves to the next tab and focuses it", () => {
  const dom = load();
  const event = fire(byName(dom.tabs, "events"), "keydown", { key: "ArrowRight" });

  assert.equal(event.defaultPrevented, true, "the tab key is consumed");
  assertSelection(dom, "checks", "ArrowRight");
  assert.equal(dom.document.activeElement, byName(dom.tabs, "checks"));
  assert.equal(paneFor(dom.panes, "checks").hidden, false);
  assert.equal(paneFor(dom.panes, "events").hidden, true);
});

test("ArrowLeft wraps from the first tab to the last", () => {
  const dom = load();
  fire(byName(dom.tabs, "events"), "keydown", { key: "ArrowLeft" });
  assertSelection(dom, "raw", "ArrowLeft wrap");
  assert.equal(dom.document.activeElement, byName(dom.tabs, "raw"));
});

test("ArrowRight wraps from the last tab to the first", () => {
  const dom = load();
  fire(byName(dom.tabs, "raw"), "keydown", { key: "ArrowRight" });
  assertSelection(dom, "events", "ArrowRight wrap");
  assert.equal(dom.document.activeElement, byName(dom.tabs, "events"));
});

test("Home and End jump to the ends of the tablist", () => {
  const dom = load();
  fire(byName(dom.tabs, "checks"), "keydown", { key: "End" });
  assertSelection(dom, "raw", "End");
  assert.equal(dom.document.activeElement, byName(dom.tabs, "raw"));

  fire(byName(dom.tabs, "raw"), "keydown", { key: "Home" });
  assertSelection(dom, "events", "Home");
  assert.equal(dom.document.activeElement, byName(dom.tabs, "events"));
});

test("click activation still moves selection, focus path unchanged", () => {
  const dom = load();
  byName(dom.tabs, "checks").click();
  assertSelection(dom, "checks", "click");
  assert.equal(selected(dom.tabs), byName(dom.tabs, "checks"));
});

test("modified arrow keys are ignored", () => {
  for (const modifier of ["ctrlKey", "altKey", "metaKey"]) {
    const dom = load();
    const event = fire(byName(dom.tabs, "events"), "keydown", {
      key: "ArrowRight", [modifier]: true,
    });
    assert.equal(event.defaultPrevented, false, `${modifier}+ArrowRight is not consumed`);
    assertSelection(dom, "events", `${modifier}+ArrowRight`);
  }
});

test("keys during IME composition are ignored", () => {
  const dom = load();
  const composing = fire(byName(dom.tabs, "events"), "keydown", { key: "ArrowRight", isComposing: true });
  assert.equal(composing.defaultPrevented, false);
  assertSelection(dom, "events", "isComposing");

  const legacy = fire(byName(dom.tabs, "events"), "keydown", { key: "ArrowRight", keyCode: 229 });
  assert.equal(legacy.defaultPrevented, false);
  assertSelection(dom, "events", "keyCode 229");
});

test("other keys are not handled", () => {
  const dom = load();
  const event = fire(byName(dom.tabs, "events"), "keydown", { key: "Tab" });
  assert.equal(event.defaultPrevented, false);
  assertSelection(dom, "events", "Tab");
});