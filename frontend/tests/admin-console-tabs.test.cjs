const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const app = path.resolve(__dirname, "../../app");
const template = fs.readFileSync(path.join(app, "templates/lanmatrix/admin.html"), "utf8");
const scripts = path.join(app, "static/js/lanmatrix");
const tabNames = ["users", "license", "tasks"];

function attributes(selector) {
  const tag = template.match(new RegExp(`<[^<>]*\\s${selector}(?=[\\s>])[^<>]*>`));
  assert.ok(tag, `Expected a template element with ${selector}`);
  const entries = [...tag[0].matchAll(/\s([\w-]+)(?:="([^"]*)")?/g)];
  return new Map(entries.map(([, name, value]) => [name, value ?? ""]));
}

function loadPage(query = "") {
  const document = {};
  const nodes = new Map();
  const tabList = new EventTarget();
  const tabs = tabNames.map((name) => {
    const attrs = attributes(`data-tab="${name}"`);
    const tab = new EventTarget();
    const classes = new Set(attrs.get("class").split(" "));
    return Object.assign(tab, {
      dataset: { tab: name },
      tabIndex: Number(attrs.get("tabindex")),
      getAttribute: (key) => attrs.get(key),
      setAttribute: (key, value) => attrs.set(key, value),
      classList: {
        contains: (value) => classes.has(value),
        toggle: (value, on) => on ? classes.add(value) : classes.delete(value),
      },
      focus() {
        focus(tab);
      },
    });
  });
  const panes = tabNames.map((name) => {
    const classes = new Set(name === "users" ? ["on"] : []);
    return {
      dataset: { pane: name },
      hidden: attributes(`data-pane="${name}"`).has("hidden"),
      classList: {
        contains: (value) => classes.has(value),
        toggle: (value, on) => on ? classes.add(value) : classes.delete(value),
      },
    };
  });
  tabList.contains = (element) => tabs.includes(element);
  tabList.querySelectorAll = (selector) => selector === ".tab" ? tabs : [];

  function getElementById(id) {
    if (!nodes.has(id)) {
      nodes.set(id, Object.assign(new EventTarget(), { querySelectorAll: () => [] }));
    }
    return nodes.get(id);
  }

  function focus(element) {
    const previous = document.activeElement;
    document.activeElement = element;
    if (previous !== element && tabList.contains(previous)) {
      const event = Object.assign(new Event("focusout"), { relatedTarget: element });
      tabList.dispatchEvent(event);
    }
  }

  const root = {
    querySelector: (selector) => selector === '[role="tablist"]' ? tabList : null,
    querySelectorAll: (selector) => {
      if (selector === ".tab") return tabs;
      if (selector === ".pane") return panes;
      return [];
    },
  };
  Object.assign(document, {
    activeElement: getElementById("lm-user-new"),
    getElementById,
    querySelector: (selector) => selector === ".lm-admin" ? root : null,
  });

  const window = new EventTarget();
  const location = new URL(`/lanmatrix/admin${query}`, "http://localhost");
  const entries = [location.href];
  let position = 0;
  const history = {
    pushState(state, title, url) {
      location.href = new URL(url, location).href;
      entries.splice(position + 1, entries.length, location.href);
      position += 1;
    },
    go(delta) {
      position += delta;
      location.href = entries[position];
      window.dispatchEvent(new Event("popstate"));
    },
  };
  const calls = [];
  // Every admin API call stays pending: the loaders must not get past their
  // first await, keeping the test focused on tab semantics rather than tables.
  const LMApi = new Proxy({}, {
    get: (_target, name) => typeof name !== "string" ? undefined : (...args) => {
      calls.push({ name, args });
      return new Promise(() => {});
    },
  });
  Object.assign(window, { location, history, LMReady: Promise.resolve() });
  const context = vm.createContext({
    window, document, URLSearchParams, LMApi,
    LMPill: { TASK_ZH: {}, apply() {} },
  });
  vm.runInContext(fs.readFileSync(path.join(scripts, "urlstate.js"), "utf8"), context);
  context.LMUrl = window.LMUrl;
  vm.runInContext(fs.readFileSync(path.join(scripts, "admin_console.js"), "utf8"), context);

  return { document, tabs, panes, window, history, entries, calls, focus };
}

function assertSelected(page, index, tabStop = index) {
  page.tabs.forEach((tab, i) => {
    assert.equal(tab.getAttribute("aria-selected"), String(i === index),
      `tab ${tabNames[i]} aria-selected`);
    assert.equal(tab.classList.contains("on"), i === index, `tab ${tabNames[i]} .on`);
    assert.equal(tab.tabIndex, i === tabStop ? 0 : -1, `tab ${tabNames[i]} tabindex`);
    assert.equal(page.panes[i].hidden, i !== index, `pane ${tabNames[i]} hidden`);
    assert.equal(page.panes[i].classList.contains("on"), i === index, `pane ${tabNames[i]} .on`);
  });
}

function press(element, key, modifiers = {}) {
  const event = Object.assign(new Event("keydown", { cancelable: true }), { key }, modifiers);
  return !element.dispatchEvent(event);
}

test("the template exposes a real tablist with one selected tab", () => {
  const tabList = attributes('role="tablist"');
  assert.ok(tabList.get("aria-label"), "the tablist has an accessible name");

  // A tablist may contain tabs only.
  const start = template.indexOf('<div class="tabs" role="tablist"');
  const openEnd = template.indexOf(">", start) + 1;
  const inner = template.slice(openEnd, template.indexOf("</div>", openEnd));
  const children = inner.match(/<[a-zA-Z][^>]*>/g) || [];
  assert.equal(children.length, tabNames.length);
  children.forEach((tag) => assert.match(tag, /^<button\b/));

  tabNames.forEach((name) => {
    const attrs = attributes(`data-tab="${name}"`);
    assert.equal(attrs.get("type"), "button", `${name} type`);
    assert.equal(attrs.get("role"), "tab", `${name} role`);
    assert.equal(attrs.get("id"), `lm-admin-tab-${name}`, `${name} id`);
    assert.equal(attrs.get("aria-selected"), name === "users" ? "true" : "false",
      `${name} aria-selected`);
    assert.equal(attrs.get("tabindex"), name === "users" ? "0" : "-1", `${name} tabindex`);
    assert.ok(!attrs.has("aria-controls"), `${name} has no aria-controls`);

    const pane = attributes(`data-pane="${name}"`);
    assert.equal(pane.get("role"), "tabpanel", `${name} pane role`);
    assert.equal(pane.get("aria-labelledby"), `lm-admin-tab-${name}`,
      `${name} pane aria-labelledby`);
    assert.equal(pane.has("hidden"), name !== "users", `${name} pane hidden`);
  });
});

test("navigation keys move focus and the roving tab stop without selecting", async () => {
  const page = loadPage();
  await page.window.LMReady;
  const [users] = page.tabs;

  users.focus();
  const moves = [
    ["ArrowRight", 1], ["ArrowRight", 2], ["ArrowRight", 0], // wraps forward
    ["ArrowLeft", 2], ["Home", 0], ["End", 2], ["ArrowLeft", 1], ["Home", 0],
  ];
  for (const [key, target] of moves) {
    assert.equal(press(page.document.activeElement, key), true, `${key} is handled`);
    assert.equal(page.document.activeElement, page.tabs[target],
      `${key} focuses ${tabNames[target]}`);
    assertSelected(page, 0, target); // selection unchanged, tab stop follows focus
    assert.equal(page.tabs.filter((tab) => tab.tabIndex === 0).length, 1);
  }

  users.focus();
  for (const key of ["Enter", " ", "Tab", "ArrowUp", "ArrowDown"]) {
    assert.equal(press(users, key), false, `${key} is not handled`);
  }
  for (const modifier of ["altKey", "ctrlKey", "metaKey", "shiftKey"]) {
    assert.equal(press(users, "ArrowRight", { [modifier]: true }), false);
    assert.equal(page.document.activeElement, users);
  }
  assertSelected(page, 0);

  // Leaving the tablist puts the tab stop back on the selected tab.
  press(users, "End");
  assertSelected(page, 0, 2);
  page.focus(page.document.getElementById("lm-user-new"));
  assertSelected(page, 0);

  assert.equal(page.entries.length, 1, "focus movement pushes no history entry");
  assert.equal(page.window.location.search, "");
});

test("clicks select tabs and keep the ?tab= deep-link contract", async () => {
  const page = loadPage("?tab=license&keep=1");
  await page.window.LMReady;
  const [users, license] = page.tabs;

  const query = () => new URLSearchParams(page.window.location.search);
  assertSelected(page, 1); // restored from the URL
  assert.equal(query().get("tab"), "license");
  assert.equal(query().get("keep"), "1");

  users.dispatchEvent(new Event("click"));
  assertSelected(page, 0);
  assert.equal(page.window.location.search, "?keep=1");

  users.dispatchEvent(new Event("click"));
  assert.equal(page.entries.length, 2, "re-selecting the same tab pushes nothing");

  license.dispatchEvent(new Event("click"));
  assertSelected(page, 1);
  assert.equal(query().get("tab"), "license");
  assert.equal(query().get("keep"), "1");
  assert.equal(page.entries.length, 3);

  page.history.go(-1);
  assertSelected(page, 0);
  assert.equal(page.window.location.search, "?keep=1");

  page.history.go(1);
  assertSelected(page, 1);
  assert.equal(query().get("tab"), "license");
  assert.equal(query().get("keep"), "1");

  const unknown = loadPage("?tab=bogus");
  await unknown.window.LMReady;
  assertSelected(unknown, 0); // unknown values fall back to the default tab
});