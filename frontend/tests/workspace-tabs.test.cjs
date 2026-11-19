const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const root = path.resolve(__dirname, "../..");
const template = fs.readFileSync(path.join(root, "app/templates/lanmatrix/home.html"), "utf8");
const tabMarkup = [...template.matchAll(/<button\b([^>]*\bdata-view="[^"]+"[^>]*)>/g)];
const panelMarkup = [...template.matchAll(/<div\b([^>]*\bid="lm-pane-[^"]+"[^>]*)>/g)];
const settle = () => new Promise((resolve) => setImmediate(resolve));

function workspace(search = "") {
  function node(markup = "") {
    const attrs = new Map(Array.from(markup.matchAll(/([\w-]+)(?:="([^"]*)")?/g),
      ([, name, value]) => [name, value ?? ""]));
    const classes = new Set((attrs.get("class") || "").split(" "));
    const target = new EventTarget();
    Object.defineProperty(target, "tabIndex", {
      get: () => Number(attrs.get("tabindex") ?? -1),
      set: (value) => attrs.set("tabindex", String(value)),
    });
    return Object.assign(target, {
      dataset: { view: attrs.get("data-view") },
      hidden: attrs.has("hidden"),
      value: "",
      classList: {
        contains: (name) => classes.has(name),
        toggle: (name, on) => on ? classes.add(name) : classes.delete(name),
      },
      getAttribute: (name) => attrs.get(name) ?? null,
      setAttribute: (name, value) => attrs.set(name, value),
      removeAttribute: (name) => attrs.delete(name),
      querySelectorAll: () => [],
    });
  }

  function fire(target, type, props) {
    return target.dispatchEvent(Object.assign(new Event(type, { cancelable: true }), props));
  }

  const tabs = tabMarkup.map(([, markup]) => node(markup));
  const panels = panelMarkup.map(([, markup]) => node(markup));
  const tablist = node(template.match(/<\w+\b([^>]*\bid="lm-home-tabs"[^>]*)>/)[1]);
  const nodes = new Map(tabs.concat(panels).map((el) => [el.getAttribute("id"), el]));
  nodes.set("lm-home-tabs", tablist);
  tablist.contains = (el) => tabs.includes(el);
  const document = Object.assign(new EventTarget(), {
    activeElement: null,
    getElementById(id) {
      if (!nodes.has(id)) nodes.set(id, node());
      return nodes.get(id);
    },
    querySelectorAll: (selector) => selector.startsWith("#lm-home-tabs ") ? tabs : [],
    querySelector: () => null,
  });
  const reviewCheckbox = document.getElementById("lm-h-rv-all");
  panels.forEach((panel, index) => {
    panel.contains = (element) => element === panel || (index === 1 && element === reviewCheckbox);
  });
  function focus(el) {
    const before = document.activeElement;
    if (before === el) return;
    document.activeElement = el;
    if (tabs.includes(before)) fire(tablist, "focusout", { relatedTarget: el });
    fire(el, "focus");
  }
  for (const el of tabs.concat(panels)) el.focus = () => focus(el);

  const pathname = "/lanmatrix/home";
  const entries = [pathname + search];
  const location = { pathname, search };
  const polling = new Set();
  let timer = 0;
  const browser = new EventTarget();
  const window = {
    document, location, URLSearchParams,
    addEventListener: browser.addEventListener.bind(browser),
    LMTaskRow: { FINAL: ["passed"], rowHtml: () => "" },
    LMPill: { TASK_ZH: {}, PROJECT_ZH: {}, html: () => "" },
    LMApi: {
      meOverview: async () => ({ projects: [{ id: 1 }] }),
      meTasks: async () => ({ tasks: [{ task_id: "T1", status: "running" }] }),
    },
    setInterval() {
      polling.add(++timer);
      return timer;
    },
    clearInterval: (id) => polling.delete(id),
    history: {
      pushState(_state, _title, url) {
        entries.push(url);
        location.search = url.slice(pathname.length);
      },
      replaceState(_state, _title, url) {
        entries[entries.length - 1] = url;
        location.search = url.slice(pathname.length);
      },
      back() {
        entries.pop();
        location.search = entries.at(-1).slice(pathname.length);
        fire(browser, "popstate");
      },
    },
  };
  const context = vm.createContext(Object.assign(window, { window }));
  for (const script of ["urlstate.js", "home.js"]) {
    const source = fs.readFileSync(path.join(root, "app/static/js/lanmatrix", script), "utf8");
    vm.runInContext(source, context, { filename: script });
  }

  function assertView(selected, tabStop = selected) {
    assert.equal(window.LMHome.view(), selected);
    for (const [index, tab] of tabs.entries()) {
      const view = tab.dataset.view;
      const panel = panels[index];
      assert.equal(tab.tabIndex, view === tabStop ? 0 : -1);
      assert.equal(tab.getAttribute("aria-selected"), String(view === selected));
      assert.equal(tab.classList.contains("on"), view === selected);
      assert.equal(panel.hidden, view !== selected);
      if (!panel.hidden) assert.equal(panel.tabIndex, 0);
      else assert.equal(panel.getAttribute("tabindex"), null);
    }
  }

  return { document, tablist, tabs, panels, entries, polling, location, assertView,
    focus,
    start: () => fire(document, "DOMContentLoaded"),
    key: (key, modifiers) => fire(document.activeElement, "keydown", { key, ...modifiers }),
    click: (tab) => fire(tab, "click"),
    back: window.history.back,
  };
}

test("the workspace template names all three panels and renders one tab stop", () => {
  const { tablist, tabs, panels } = workspace();
  assert.equal(tablist.getAttribute("role"), "tablist");
  assert.ok(tablist.getAttribute("aria-label"));
  assert.deepEqual(tabs.map((tab) => tab.dataset.view), ["tasks", "reviews", "projects"]);
  assert.equal(panels.length, tabs.length);
  for (const [index, tab] of tabs.entries()) {
    const panel = panels[index];
    assert.equal(tab.getAttribute("type"), "button");
    assert.equal(tab.getAttribute("role"), "tab");
    assert.equal(panel.getAttribute("role"), "tabpanel");
    assert.ok(tab.getAttribute("id"));
    assert.equal(panel.getAttribute("aria-labelledby"), tab.getAttribute("id"));
    assert.equal(tab.getAttribute("aria-controls"), panel.getAttribute("id"));
    assert.equal(tab.getAttribute("aria-selected"), String(index === 0));
    assert.equal(tab.getAttribute("tabindex"), index === 0 ? "0" : "-1");
    assert.equal(panel.getAttribute("tabindex"), index === 0 ? "0" : null);
    assert.equal(panel.hidden, index !== 0);
  }
});

test("arrows only move focus and leaving the tablist restores the selected stop", async () => {
  const page = workspace();
  page.start();
  await settle();
  assert.equal(page.polling.size, 1);
  const [tasks, reviews, projects] = page.tabs;
  tasks.focus();
  for (const [key, target] of [
    ["ArrowLeft", projects], ["ArrowRight", tasks], ["End", projects],
    ["Home", tasks], ["ArrowRight", reviews],
  ]) {
    assert.equal(page.key(key), false, `${key} prevents its default action`);
    assert.equal(page.document.activeElement, target);
    page.assertView("tasks", target.dataset.view);
    assert.deepEqual(page.entries, ["/lanmatrix/home"]);
    assert.equal(page.polling.size, 1);
  }
  assert.equal(page.key("ArrowLeft", { altKey: true }), true);
  assert.equal(page.key("Tab"), true);
  assert.equal(page.key("Tab", { shiftKey: true }), true);
  assert.equal(page.document.activeElement, reviews);
  page.panels[0].focus();
  page.assertView("tasks");

  tasks.focus();
  page.key("ArrowRight");
  assert.equal(page.key("Enter"), true);
  page.click(reviews);
  page.assertView("reviews");
  assert.equal(page.polling.size, 0);
  assert.deepEqual(page.entries, ["/lanmatrix/home", "/lanmatrix/home?view=reviews"]);
  page.click(reviews);
  assert.equal(page.entries.length, 2);

  page.key("End");
  assert.equal(page.key(" "), true);
  page.click(projects);
  page.assertView("projects");
  assert.equal(page.entries.length, 3);
  page.panels[2].focus();
  page.back();
  page.assertView("reviews");
  assert.equal(page.document.activeElement, reviews);
  assert.equal(page.entries.length, 2);
  page.focus(page.document.getElementById("lm-h-rv-all"));
  page.back();
  page.assertView("tasks");
  assert.equal(page.document.activeElement, tasks);
  assert.equal(page.polling.size, 1);
  assert.deepEqual(page.entries, ["/lanmatrix/home"]);
});

test("deep links select a tab without pushing history and invalid views use the default", () => {
  for (const [search, view, expected] of [
    ["?view=reviews&q=example", "reviews", "?view=reviews&q=example"],
    ["?view=unknown&q=example", "tasks", "?q=example"],
  ]) {
    const page = workspace(search);
    page.start();
    page.assertView(view);
    assert.equal(page.document.activeElement, null);
    assert.equal(page.location.search, expected);
    assert.deepEqual(page.entries, ["/lanmatrix/home" + expected]);
    page.click(page.tabs[2]);
    page.assertView("projects");
    assert.equal(new URLSearchParams(page.location.search).get("q"), "example");
    page.back();
    page.assertView(view);
    assert.deepEqual(page.entries, ["/lanmatrix/home" + expected]);
  }
});
