const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const app = path.resolve(__dirname, "../../app");
const template = fs.readFileSync(path.join(app, "templates/lanmatrix/admin_db.html"), "utf8");
const scripts = path.join(app, "static/js/lanmatrix");
const tabNames = ["tables", "sql"];

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
  const panes = tabNames.map((name) => ({
    dataset: { pane: name },
    hidden: attributes(`data-pane="${name}"`).has("hidden"),
    contains: (element) => name === "sql" && element === getElementById("lm-db-sql"),
  }));
  tabList.contains = (element) => tabs.includes(element);
  tabList.querySelectorAll = (selector) => selector === ".lm-db-tab" ? tabs : [];

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
    querySelectorAll: (selector) => selector === ".lm-db-pane" ? panes : [],
  };
  Object.assign(document, {
    activeElement: getElementById("lm-db-refresh"),
    getElementById,
    querySelector: (selector) => selector === ".lm-dbadmin" ? root : null,
  });

  const window = new EventTarget();
  const location = new URL(`/lanmatrix/admin/db${query}`, "http://localhost");
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
  const requests = [];
  Object.assign(window, { location, history, LMReady: Promise.resolve() });
  const context = vm.createContext({
    window, document, URLSearchParams,
    LMApi: {
      async dbOverview() {
        requests.push("overview");
        return { tables: [] };
      },
      async dbTables() {
        requests.push("tables");
        return { tables: [] };
      },
    },
  });
  vm.runInContext(fs.readFileSync(path.join(scripts, "urlstate.js"), "utf8"), context);
  context.LMUrl = window.LMUrl;
  vm.runInContext(fs.readFileSync(path.join(scripts, "admin_db.js"), "utf8"), context);

  return { document, tabs, panes, window, history, entries, requests, focus };
}

function assertSelected(page, index, tabStop = index) {
  page.tabs.forEach((tab, tabIndex) => {
    assert.equal(tab.getAttribute("aria-selected"), String(tabIndex === index));
    assert.equal(tab.classList.contains("is-active"), tabIndex === index);
    assert.equal(tab.tabIndex, tabIndex === tabStop ? 0 : -1);
    assert.equal(page.panes[tabIndex].hidden, tabIndex !== index);
  });
}

function press(element, key, modifiers) {
  const event = Object.assign(new Event("keydown", { cancelable: true }), { key }, modifiers);
  return !element.dispatchEvent(event);
}

test("the template names the tablist and panels and renders one selected tab", () => {
  const tabList = attributes('role="tablist"');
  assert.match(template, new RegExp(`<h1 id="${tabList.get("aria-labelledby")}">[^<]+</h1>`));
  tabNames.forEach((name, index) => {
    const tab = attributes(`data-tab="${name}"`);
    const pane = attributes(`data-pane="${name}"`);
    assert.equal(tab.get("type"), "button");
    assert.equal(tab.get("role"), "tab");
    assert.equal(tab.get("aria-selected"), String(index === 0));
    assert.equal(tab.get("tabindex"), index === 0 ? "0" : "-1");
    assert.ok(tab.get("id"));
    assert.equal(pane.get("aria-labelledby"), tab.get("id"));
    assert.equal(pane.get("role"), "tabpanel");
    assert.equal(pane.has("hidden"), index !== 0);
    if (name === "tables") assert.equal(pane.get("tabindex"), "0");
  });
});

test("navigation keys only move focus and leaving resets the tab stop to selection", async () => {
  const page = loadPage();
  const [tables, sql] = page.tabs;
  tables.focus();
  for (const [key, next] of [
    ["ArrowLeft", sql], ["ArrowRight", tables], ["End", sql],
    ["Home", tables], ["ArrowRight", sql], ["ArrowLeft", tables], ["End", sql],
  ]) {
    assert.equal(press(page.document.activeElement, key), true);
    assert.equal(page.document.activeElement, next);
    assertSelected(page, 0, page.tabs.indexOf(next));
  }
  for (const key of ["Enter", " ", "Tab", "ArrowUp", "ArrowDown"]) {
    assert.equal(press(sql, key), false);
  }
  assert.equal(press(sql, "ArrowLeft", { altKey: true }), false);
  assert.equal(page.document.activeElement, sql);
  page.focus(page.document.getElementById("lm-db-refresh"));
  assertSelected(page, 0);
  assert.equal(page.entries.length, 1);
  await page.window.LMReady;
  assert.deepEqual(page.requests, ["overview", "tables"]);
});

test("deep links, clicks, Back and Forward keep selection, focus and URL in sync", () => {
  const page = loadPage("?tab=sql&keep=1");
  const [tables, sql] = page.tabs;
  const refresh = page.document.getElementById("lm-db-refresh");
  assertSelected(page, 1);
  assert.equal(page.document.activeElement, refresh);

  sql.focus();
  press(sql, "Home");
  tables.dispatchEvent(new Event("click"));
  assertSelected(page, 0);
  assert.equal(page.window.location.search, "?keep=1");

  tables.dispatchEvent(new Event("click"));
  assert.equal(page.entries.length, 2);
  page.history.go(-1);
  assertSelected(page, 1);
  assert.equal(page.document.activeElement, sql);

  page.focus(page.document.getElementById("lm-db-sql"));
  page.history.go(1);
  assertSelected(page, 0);
  assert.equal(page.document.activeElement, tables);
  assert.equal(page.window.location.search, "?keep=1");
  assert.equal(page.entries.length, 2);
  assertSelected(loadPage("?tab=unknown"), 0);
});
