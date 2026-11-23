/* The workspace status filter is one group of mutually exclusive filters, not
 * a tab strip: clicking a status does not swap a panel, it narrows the same
 * task list. The template therefore names it `role="group"` and each native
 * button carries `aria-pressed`, with exactly one `true`.
 *
 * Two things can drift if that state is only painted with `.on`:
 *   1. Assistive technology has nothing to announce (the old role="tablist"
 *      also promised an arrow-key tab model these buttons never implemented).
 *   2. The three entry paths -- URL/deep link, a segmented click, and a KPI
 *      shortcut -- each used to write the visual class on their own.
 *
 * This test loads the real urlstate.js + home.js through the same lightweight
 * DOM shim workspace-tabs.test.cjs uses, then drives those entry paths and
 * asserts aria-pressed and .on agree on every button. It also checks the
 * unknown-?status= case keeps its old behaviour: no button pressed and the
 * value still forwarded to the request.
 */
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const root = path.resolve(__dirname, "../..");
const template = fs.readFileSync(path.join(root, "app/templates/lanmatrix/home.html"), "utf8");
// Only #lm-h-seg's buttons: the review queue further down happens to reuse
// data-status for its own (unrelated) role switch.
const segBlock = template.match(/<div\b[^>]*\bid="lm-h-seg"[^>]*>([\s\S]*?)<\/div>/);
const segMarkup = [...segBlock[1].matchAll(/<button\b([^>]*)>/g)];
const kpiMarkup = [...template.matchAll(/<button\b([^>]*\bdata-filter="[^"]+"[^>]*)>/g)];
const tabMarkup = [...template.matchAll(/<button\b([^>]*\bdata-view="[^"]+"[^>]*)>/g)];
const settle = () => new Promise((resolve) => setImmediate(resolve));

function attrsOf(markup) {
  return new Map(Array.from(markup.matchAll(/([\w-]+)(?:="([^"]*)")?/g),
    ([, name, value]) => [name, value ?? ""]));
}

test("the template renders a named group of native toggle buttons", () => {
  const group = template.match(/<div\b([^>]*\bid="lm-h-seg"[^>]*)>([\s\S]*?)<\/div>/);
  assert.ok(group, "home.html still has #lm-h-seg");
  const groupAttrs = attrsOf(group[1]);
  assert.equal(groupAttrs.get("role"), "group", "a filter group is not a tablist");
  assert.ok(groupAttrs.get("aria-label"), "the group has an accessible name");

  const buttons = [...group[2].matchAll(/<button\b([^>]*)>([\s\S]*?)<\/button>/g)];
  assert.equal(buttons.length, 5, "every status has exactly one button");
  assert.deepEqual(buttons.map(([, markup]) => attrsOf(markup).get("data-status")),
    ["", "running", "queued", "failed", "passed"]);
  for (const [, markup] of buttons) {
    const attrs = attrsOf(markup);
    assert.equal(attrs.get("type"), "button", "a native button, not an ARIA widget");
    assert.ok(attrs.has("aria-pressed"), "each toggle exposes pressed state");
  }
  const pressed = buttons
    .filter(([, markup]) => attrsOf(markup).get("aria-pressed") === "true")
    .map(([, markup]) => attrsOf(markup).get("data-status") || "");
  assert.deepEqual(pressed, [""], "全部 is the only initial selection");
});

function workspace(search = "") {
  const pathname = "/lanmatrix/home";
  const location = { pathname, search };
  const entries = [pathname + (search || "")];
  const polling = new Set();
  const taskRequests = [];
  let timer = 0;
  const browser = new EventTarget();

  function node(markup = "") {
    const attrs = attrsOf(markup);
    const classes = new Set((attrs.get("class") || "").split(" ").filter(Boolean));
    const target = new EventTarget();
    Object.defineProperty(target, "tabIndex", {
      get: () => Number(attrs.get("tabindex") ?? -1),
      set: (value) => attrs.set("tabindex", String(value)),
    });
    return Object.assign(target, {
      dataset: {
        view: attrs.get("data-view"),
        status: attrs.get("data-status"),
        filter: attrs.get("data-filter"),
      },
      hidden: attrs.has("hidden"),
      value: "",
      textContent: "",
      innerHTML: "",
      options: [],
      classList: {
        contains: (name) => classes.has(name),
        toggle: (name, on) => on ? classes.add(name) : classes.delete(name),
      },
      getAttribute: (name) => attrs.get(name) ?? null,
      setAttribute: (name, value) => attrs.set(name, String(value)),
      removeAttribute: (name) => attrs.delete(name),
      querySelectorAll: () => [],
      querySelector: () => null,
      replaceChildren: () => {},
      contains: () => false,
    });
  }

  function fire(target, type, props) {
    return target.dispatchEvent(Object.assign(new Event(type, { cancelable: true }), props));
  }

  const seg = segMarkup.map(([, markup] = []) => node(markup));
  const tiles = kpiMarkup.map(([, markup]) => node(markup));
  const tabs = tabMarkup.map(([, markup]) => node(markup));
  const nodes = new Map([...seg, ...tiles, ...tabs]
    .filter((el) => el.getAttribute("id"))
    .map((el) => [el.getAttribute("id"), el]));
  seg.forEach((button) => nodes.set(`seg-${button.dataset.status}`, button));

  const document = Object.assign(new EventTarget(), {
    activeElement: null,
    getElementById(id) {
      if (!nodes.has(id)) nodes.set(id, node());
      return nodes.get(id);
    },
    querySelectorAll(selector) {
      if (selector === "#lm-home-tabs .tab[data-view]") return tabs;
      if (selector === "#lm-h-seg button") return seg;
      if (selector === "#lm-kpi .kpi-tile[data-filter]") return tiles;
      return [];
    },
    querySelector: () => null,
  });

  const window = {
    document, location, URLSearchParams,
    addEventListener: browser.addEventListener.bind(browser),
    LMTaskRow: { FINAL: ["passed"], rowHtml: () => "", cmp: () => 0 },
    LMPill: { TASK_ZH: {}, PROJECT_ZH: {}, html: () => "" },
    LMApi: {
      meOverview: async () => ({ projects: [{ id: 1, code: "P1" }] }),
      meTasks: async (params) => {
        taskRequests.push(params);
        return { tasks: [] };
      },
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
    },
  };
  const context = vm.createContext(Object.assign(window, { window }));
  for (const script of ["urlstate.js", "home.js"]) {
    const source = fs.readFileSync(path.join(root, "app/static/js/lanmatrix", script), "utf8");
    vm.runInContext(source, context, { filename: script });
  }

  const statusInput = () => document.getElementById("lm-h-status");
  const mineBox = () => document.getElementById("lm-h-mine");

  function assertPressed(expected, effective = expected[0] || "") {
    const actual = seg
      .filter((button) => button.getAttribute("aria-pressed") === "true")
      .map((button) => button.dataset.status || "");
    assert.deepEqual(actual, expected, "one aria-pressed=true matching the effective status");
    for (const button of seg) {
      assert.equal(button.classList.contains("on"),
        button.getAttribute("aria-pressed") === "true",
        "the painted class and the announced state never disagree");
    }
    assert.equal(statusInput().value, effective);
  }

  return {
    document, location, browser, seg, tiles, entries, taskRequests,
    statusInput, mineBox, assertPressed,
    start: () => fire(document, "DOMContentLoaded"),
    click: (el) => fire(el, "click"),
    pop: (nextSearch) => {
      location.search = nextSearch;
      fire(browser, "popstate");
    },
  };
}

test("a deep-linked status is pressed on first paint", async () => {
  const page = workspace("?status=running");
  page.start();
  await settle();
  page.assertPressed(["running"]);
  assert.equal(page.statusInput().value, "running");
});

test("segmented clicks move the pressed state and republish the URL", async () => {
  const page = workspace();
  page.start();
  await settle();
  page.assertPressed([""]);
  page.document.activeElement = page.seg[0];

  page.click(page.seg[3]);
  await settle();
  page.assertPressed(["failed"]);
  assert.equal(page.entries.at(-1), "/lanmatrix/home?status=failed");
  assert.deepEqual(page.taskRequests.at(-1).status, "failed");
  assert.equal(page.document.activeElement, page.seg[0], "clicking does not move focus");
  assert.equal(page.seg.length, 5, "the options are preserved");

  page.click(page.seg[0]);
  await settle();
  page.assertPressed([""]);
  assert.equal(page.entries.at(-1), "/lanmatrix/home", "全部 removes the empty status param");
});

test("Back/forward restoration reuses the same pressed-state path", async () => {
  const page = workspace("?status=running");
  page.start();
  await settle();
  page.assertPressed(["running"]);

  page.pop("?status=queued");
  await settle();
  page.assertPressed(["queued"]);

  page.pop("?status=");
  await settle();
  page.assertPressed([""]);
});

test("a KPI status shortcut presses the matching filter and toggles off", async () => {
  const page = workspace();
  page.start();
  await settle();
  const running = page.tiles.find((tile) => tile.dataset.filter === "running");
  assert.ok(running, "the 运行中 tile is a status shortcut");

  page.click(running);
  await settle();
  page.assertPressed(["running"]);
  assert.equal(running.getAttribute("aria-pressed"), "true");

  page.click(running);
  await settle();
  page.assertPressed([""]);
  assert.equal(running.getAttribute("aria-pressed"), "false");
});

test("the mine shortcut clears a status and leaves 全部 pressed", async () => {
  const page = workspace("?status=failed");
  page.start();
  await settle();
  page.assertPressed(["failed"]);
  assert.equal(page.mineBox().checked, false);

  const mine = page.tiles.find((tile) => tile.dataset.filter === "mine");
  assert.ok(mine, "the 我的进行中 tile is a status shortcut");
  page.click(mine);
  await settle();
  assert.equal(page.mineBox().checked, true);
  page.assertPressed([""]);
  assert.equal(mine.getAttribute("aria-pressed"), "true");
});

test("an unknown URL status presses nothing and is still sent to the server", async () => {
  const page = workspace("?status=bogus");
  page.start();
  await settle();
  page.assertPressed([], "bogus");
  assert.equal(page.statusInput().value, "bogus", "the value itself is preserved");
  assert.equal(page.location.search, "?status=bogus");
  assert.equal(page.taskRequests.at(-1).status, "bogus");
});