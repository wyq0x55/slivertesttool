// Guards the curve panel's toolbar semantics (frontend modernization C4).
//
// The export dropdown advertises role="menu", so task_curves.js must implement the
// ARIA menu button keyboard model (open → first item, arrows with wraparound,
// Home/End, Escape restores focus, Tab leaves), and the variable filter strip is a
// group of toggle buttons, not a radiogroup. The menu logic is executed through a
// minimal DOM shim so the assertions cover real behaviour, not only source text.
const vm = require("node:vm"),
  fs = require("node:fs"),
  path = require("node:path"),
  assert = require("node:assert/strict"),
  { test } = require("node:test");

const root = path.resolve(__dirname, "../..");
const template = fs.readFileSync(path.join(root, "app/templates/lanmatrix/project_tasks.html"), "utf8");
const source = fs.readFileSync(path.join(root, "app/static/js/lanmatrix/task_curves.js"), "utf8");

let activeDocument = null;

function matches(node, selector) {
  if (!node) return false;
  if (selector.startsWith("[") && selector.endsWith("]")) {
    const body = selector.slice(1, -1);
    if (body.includes("=")) {
      const [name, value] = body.split("=");
      return node.getAttribute(name) === value.replace(/^"|"$/g, "");
    }
    return node.getAttribute(body) !== null;
  }
  if (selector.startsWith(".")) return node.classList.contains(selector.slice(1));
  return false;
}

function makeElement(id, props) {
  const element = {
    id,
    hidden: false,
    textContent: "",
    innerHTML: "",
    value: "",
    checked: false,
    tabIndex: 0,
    dataset: {},
    attributes: new Map(),
    listeners: new Map(),
    children: [],
    parent: null,
    classList: {
      set: new Set(),
      add(name) { this.set.add(name); },
      remove(name) { this.set.delete(name); },
      contains(name) { return this.set.has(name); },
      toggle(name, on) {
        if (on === undefined) this.set.has(name) ? this.set.delete(name) : this.set.add(name);
        else if (on) this.set.add(name); else this.set.delete(name);
      },
    },
    setAttribute(name, value) { this.attributes.set(name, String(value)); },
    getAttribute(name) {
      if (name.startsWith("data-")) {
        const prop = name.slice(5).replace(/-([a-z])/g, (_, c) => c.toUpperCase());
        if (prop in this.dataset) return String(this.dataset[prop]);
      }
      return this.attributes.has(name) ? this.attributes.get(name) : null;
    },
    removeAttribute(name) { this.attributes.delete(name); },
    addEventListener(type, handler) {
      if (!this.listeners.has(type)) this.listeners.set(type, []);
      this.listeners.get(type).push(handler);
    },
    focus() {
      // A display:none element cannot take focus: mimic the browser instead of
      // recording focus on an item the user could never reach, or a broken
      // arrow sequence would still look green here.
      for (let node = this; node; node = node.parent) if (node.hidden) return;
      activeDocument.activeElement = this;
    },
    click() { fire(this, "click"); },
    contains(node) {
      for (let cur = node; cur; cur = cur.parent) if (cur === this) return true;
      return false;
    },
    closest(selector) {
      for (let node = this; node; node = node.parent) if (matches(node, selector)) return node;
      return null;
    },
    querySelectorAll(selector) { return this.children.filter(child => matches(child, selector)); },
  };
  return Object.assign(element, props);
}

function fire(target, type, props) {
  const event = Object.assign({
    type,
    target,
    defaultPrevented: false,
    preventDefault() { this.defaultPrevented = true; },
    stopPropagation() {},
  }, props);
  // Real events bubble, and the menu listens on its container rather than on each
  // item, so walk the parent chain the way the browser would.
  for (let node = target; node; node = node.parent) {
    for (const handler of node.listeners.get(type) || []) handler(event);
  }
  return event;
}

function load() {
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
    querySelectorAll() { return []; },
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
  const location = { href: "http://localhost/lanmatrix/projects/1/tasks/x?cv=1" };
  const navigator = { clipboard: { writeText: async () => {} } };
  class Observer { observe() {} disconnect() {} }

  vm.runInContext(source, vm.createContext({
    window,
    document,
    location,
    navigator,
    console,
    setTimeout,
    requestAnimationFrame: callback => setTimeout(callback, 0),
    getComputedStyle: () => ({ getPropertyValue: () => "" }),
    ResizeObserver: Observer,
    MutationObserver: Observer,
  }), { filename: "task_curves.js" });

  const menu = elements.get("cv-export-menu");
  const trigger = elements.get("cv-export");
  assert.ok(menu, "cv-export-menu is present in the curve panel markup");
  assert.ok(trigger, "cv-export is present in the curve panel markup");
  const wrapper = makeElement("cv-menu", { parent: makeElement("cv-toolbar") });
  wrapper.classList.add("cv-menu");
  trigger.parent = wrapper;
  // Mirror the server-rendered initial state: the menu is closed and the trigger
  // reports it, exactly as the `hidden` and `aria-expanded="false"` attributes do.
  menu.hidden = true;
  trigger.setAttribute("aria-expanded", "false");
  // Items are queried lazily, so attaching them after the script runs is enough.
  menu.children = [
    makeElement("mi-png", { tagName: "BUTTON", dataset: { export: "png" }, parent: menu, tabIndex: -1 }),
    makeElement("mi-svg", { tagName: "BUTTON", dataset: { export: "svg" }, parent: menu, tabIndex: -1 }),
    makeElement("mi-csv", { tagName: "BUTTON", dataset: { export: "csv" }, parent: menu, tabIndex: -1 }),
    makeElement("cv-download", { tagName: "A", href: "#", hidden: true, parent: menu, tabIndex: -1 }),
  ];
  for (const item of menu.children) item.attributes.set("role", "menuitem");

  return { document, elements, trigger, menu, items: menu.children };
}

const expanded = element => element.getAttribute("aria-expanded");

test("the variable filter strip is a group of toggle buttons, not a radiogroup", () => {
  assert.match(template, /<div class="cv-filters" role="group" aria-label="变量筛选">/);
  assert.ok(!template.includes('role="radiogroup"'), "no radiogroup remains in the template");
  const strip = template.slice(template.indexOf('class="cv-filters"'));
  assert.match(strip, /class="cv-chip"[^>]*aria-pressed=/, "filter chips keep their aria-pressed state");
});

test("the export dropdown declares a proper menu button", () => {
  assert.match(template, /id="cv-export"[^>]*aria-haspopup="menu"[^>]*aria-expanded="false"/);
  assert.match(template, /id="cv-export-menu" role="menu" aria-labelledby="cv-export" hidden/);
  const menuMarkup = template.slice(template.indexOf('id="cv-export-menu"'), template.indexOf("</div>", template.indexOf('id="cv-export-menu"')));
  const items = menuMarkup.match(/role="menuitem"[^>]*/g) || [];
  assert.equal(items.length, 4, "four menuitems stay in the export menu");
  for (const item of items) assert.match(item, /tabindex="-1"/, "menuitems leave the Tab order to the trigger");
  // Until load() resolves the curve data, the download link still points at "#";
  // shipping it hidden keeps Enter/Space from jumping the page to the top.
  assert.match(template, /<a role="menuitem" tabindex="-1" id="cv-download" href="#" hidden>/);
});

test("source keeps the menu keyboard handling and mirrors aria-expanded", () => {
  for (const token of ['"ArrowDown"', '"ArrowUp"', '"Home"', '"End"', '"Escape"', '"Tab"']) {
    assert.ok(source.includes(token), `${token} is handled`);
  }
  assert.match(source, /menuBtn\.setAttribute\("aria-expanded", "true"\)/);
  assert.match(source, /menuBtn\.setAttribute\("aria-expanded", "false"\)/);
});

test("clicking the trigger opens the menu and focuses the first item", () => {
  const { document, trigger, menu, items } = load();
  assert.equal(menu.hidden, true);
  assert.equal(expanded(trigger), "false");

  fire(trigger, "click");

  assert.equal(menu.hidden, false);
  assert.equal(expanded(trigger), "true");
  assert.equal(document.activeElement, items[0], "focus moves into the menu on open");

  fire(trigger, "click");
  assert.equal(menu.hidden, true, "a second click closes it");
  assert.equal(expanded(trigger), "false");
  assert.equal(document.activeElement, trigger, "closing returns focus to the trigger");
});

test("items that are hidden or disabled stay out of the arrow sequence", () => {
  const { document, trigger, items } = load();
  const press = key => fire(document.activeElement, "keydown", { key });
  fire(trigger, "click");

  press("End");
  assert.equal(document.activeElement, items[2], "the hidden download link is not a target");
  press("ArrowDown");
  assert.equal(document.activeElement, items[0], "wrapping skips the hidden link");

  items[2].disabled = true;
  press("End");
  assert.equal(document.activeElement, items[1], "a disabled item is skipped too");
});

test("Space on a menu link opens it instead of scrolling the page", () => {
  const { document, trigger, menu, items } = load();
  items[3].hidden = false;
  fire(trigger, "click");
  fire(document.activeElement, "keydown", { key: "End" });
  assert.equal(document.activeElement, items[3]);

  const space = fire(items[3], "keydown", { key: " " });
  assert.equal(space.defaultPrevented, true, "Space does not scroll the page");
  assert.equal(menu.hidden, true, "activating an item closes the menu");
  assert.equal(document.activeElement, trigger, "focus returns to the trigger");
});

test("ArrowDown/ArrowUp cycle with wraparound and Home/End jump", () => {
  const { document, trigger, items } = load();
  items[3].hidden = false;
  const press = key => fire(document.activeElement, "keydown", { key });
  fire(trigger, "click");

  press("ArrowDown");
  assert.equal(document.activeElement, items[1]);
  press("ArrowDown"); press("ArrowDown");
  assert.equal(document.activeElement, items[3]);
  press("ArrowDown");
  assert.equal(document.activeElement, items[0], "ArrowDown wraps to the first item");
  press("ArrowUp");
  assert.equal(document.activeElement, items[3], "ArrowUp wraps to the last item");
  press("Home");
  assert.equal(document.activeElement, items[0]);
  press("End");
  assert.equal(document.activeElement, items[3]);

  const event = press("ArrowDown");
  assert.equal(event.defaultPrevented, true, "arrow keys do not scroll the page");
});

test("ArrowDown on the trigger opens the menu at the first item", () => {
  const { document, trigger, menu, items } = load();
  const event = fire(trigger, "keydown", { key: "ArrowDown" });
  assert.equal(event.defaultPrevented, true);
  assert.equal(menu.hidden, false);
  assert.equal(expanded(trigger), "true");
  assert.equal(document.activeElement, items[0]);
});

test("Escape closes the menu and restores focus to the trigger", () => {
  const { document, trigger, menu, items } = load();
  fire(trigger, "click");
  fire(items[1], "keydown", { key: "ArrowDown" });

  const event = fire(items[2], "keydown", { key: "Escape" });

  assert.equal(event.defaultPrevented, true);
  assert.equal(menu.hidden, true);
  assert.equal(expanded(trigger), "false");
  assert.equal(document.activeElement, trigger);
});

test("Tab leaves the menu instead of trapping focus", () => {
  const { document, trigger, menu, items } = load();
  fire(trigger, "click");

  const event = fire(items[0], "keydown", { key: "Tab" });

  assert.equal(event.defaultPrevented, false, "Tab is not swallowed");
  assert.equal(menu.hidden, true);
  assert.equal(expanded(trigger), "false");
  assert.equal(document.activeElement, trigger, "focus is not left inside hidden content");
});

test("selecting a menuitem closes the menu and still runs the download action", () => {
  const { document, elements, trigger, menu, items } = load();
  fire(trigger, "click");
  fire(items[2], "click");
  assert.equal(menu.hidden, true);
  assert.equal(expanded(trigger), "false");
  assert.equal(document.activeElement, trigger);
  // exportCsv with no lanes selected reports instead of downloading.
  assert.equal(elements.get("cv-status").textContent, "先选择变量。");

  fire(trigger, "click");
  fire(items[0], "click");
  assert.equal(menu.hidden, true, "PNG export closes the menu too");
  assert.equal(elements.get("cv-status").textContent, "没有可导出的泳道。");
});

test("an outside click on plain page area returns focus to the trigger", () => {
  const { document, trigger, menu } = load();
  fire(trigger, "click");
  // Clicking a non-focusable area blurs the menuitem before the document
  // listener runs; focus must not stay in the menu that is about to hide.
  document.activeElement = document.body;

  fire(document, "click", { target: makeElement("outside") });

  assert.equal(menu.hidden, true);
  assert.equal(expanded(trigger), "false");
  assert.equal(document.activeElement, trigger, "focus leaves the hidden menu");
});

test("an outside click that lands focus elsewhere is not stolen back", () => {
  const { document, trigger, menu } = load();
  fire(trigger, "click");
  const link = makeElement("other-link");
  document.activeElement = link;

  fire(document, "click", { target: link });

  assert.equal(menu.hidden, true);
  assert.equal(document.activeElement, link, "the user's own focus target wins");
});

test("a click inside the menu container does not close it", () => {
  const { document, trigger, menu } = load();
  fire(trigger, "click");
  // The bubbling document listener also sees clicks on the trigger itself.
  fire(document, "click", { target: trigger });
  assert.equal(menu.hidden, false);
  assert.equal(expanded(trigger), "true");
});