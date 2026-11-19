// Steps editor reference panel (参考 / JSON) must be a real ARIA tablist:
// the tablist holds the two tabs only, each pane is a labelled tabpanel, and the
// tabs carry aria-selected + a roving tabindex driven by arrow keys.
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const root = path.resolve(__dirname, "../..");
const template = fs.readFileSync(
  path.join(root, "app/templates/lanmatrix/_steps_dialog.html"), "utf8");
const source = fs.readFileSync(
  path.join(root, "app/static/js/lanmatrix/steps_editor.js"), "utf8");

function section(start, end) {
  const first = source.indexOf(start);
  const last = source.indexOf(end, first);
  assert.ok(first >= 0 && last > first, "production section must exist");
  return source.slice(first, last);
}

// Attributes of one tag, e.g. attrs('<button id="x" tabindex="0">').
function attrs(tag) {
  const map = new Map();
  tag.replace(/<[a-z]+/i, "").replace(/\/?>$/, "").replace(
    /([^\s=/]+)(?:\s*=\s*"([^"]*)")?/g,
    (_, name, value) => { map.set(name, value === undefined ? "" : value); return ""; });
  return map;
}

function tagFor(pattern) {
  const match = template.match(pattern);
  assert.ok(match, `template must contain ${pattern}`);
  return match[0];
}

function tabbar() {
  const start = template.indexOf('<div class="lm-refpanel-tabbar">');
  assert.ok(start >= 0, "tabbar must exist");
  const end = template.indexOf('<div class="lm-refpanel-panes">', start);
  assert.ok(end > start, "panes must follow the tabbar");
  return template.slice(start, end);
}

function tablist() {
  const block = tabbar();
  const open = block.indexOf('<div class="lm-refpanel-tabs" role="tablist"');
  assert.ok(open >= 0, "the tablist must be an inner .lm-refpanel-tabs element");
  const close = block.indexOf("</div>", open);
  assert.ok(close > open, "the tablist must close inside the tabbar");
  return { html: block.slice(open, close + "</div>".length), rest: block.slice(close) };
}

test("the tablist contains the two tabs and nothing else", () => {
  const { html, rest } = tablist();
  const buttons = html.match(/<button\b/g) || [];
  assert.equal(buttons.length, 2, "a tablist may only contain tabs");
  assert.doesNotMatch(html, /lm-refpanel-sp/, "the spacer stays outside the tablist");
  assert.doesNotMatch(html, /lm-refpanel-collapse/, "the ✕ stays outside the tablist");
  // Siblings of the tablist inside the tabbar: spacer then collapse button.
  assert.match(rest, /<span class="lm-refpanel-sp"><\/span>/);
  assert.match(rest, /class="lm-refpanel-collapse" id="lm-refpanel-collapse"/);
  assert.doesNotMatch(tagFor(/<div class="lm-refpanel-tabbar"[^>]*>/), /role=/,
    "the visual tabbar wrapper is not the tablist");
});

test("each tab carries id, role, aria-selected and a roving tabindex", () => {
  const { html } = tablist();
  const [ref, json] = html.match(/<button[^>]*>/g).map(attrs);
  assert.equal(ref.get("id"), "lm-refpanel-tab-ref");
  assert.equal(ref.get("type"), "button");
  assert.equal(ref.get("role"), "tab");
  assert.equal(ref.get("data-tab"), "ref");
  assert.equal(ref.get("aria-selected"), "true", "参考 is the default active tab");
  assert.equal(ref.get("tabindex"), "0");
  assert.equal(json.get("id"), "lm-refpanel-tab-json");
  assert.equal(json.get("type"), "button");
  assert.equal(json.get("role"), "tab");
  assert.equal(json.get("data-tab"), "json");
  assert.equal(json.get("aria-selected"), "false");
  assert.equal(json.get("tabindex"), "-1");
  assert.match(html, /role="tablist"[^>]*aria-label="[^"]+"/, "the tablist needs a name");
});

test("each pane is a tabpanel labelled by its tab", () => {
  const refPane = attrs(tagFor(/<aside id="lm-steps-ref"[^>]*>/));
  assert.equal(refPane.get("role"), "tabpanel");
  assert.equal(refPane.get("aria-labelledby"), "lm-refpanel-tab-ref");
  assert.equal(refPane.get("data-pane"), "ref");
  const jsonPane = attrs(tagFor(/<section id="lm-steps-json"[^>]*>/));
  assert.equal(jsonPane.get("role"), "tabpanel");
  assert.equal(jsonPane.get("aria-labelledby"), "lm-refpanel-tab-json");
  assert.equal(jsonPane.get("data-pane"), "json");
  assert.equal(jsonPane.has("tabindex"), false,
    "the JSON pane hosts a textarea, so the panel itself must not be a tab stop");
  assert.ok(jsonPane.has("hidden"), "the JSON pane starts hidden");
  const { html } = tablist();
  [refPane, jsonPane].forEach((pane) => {
    assert.match(html, new RegExp(`id="${pane.get("aria-labelledby")}"`),
      "aria-labelledby must point at a real tab id");
  });
  assert.doesNotMatch(template, /aria-controls/, "the tabs guide says not to add aria-controls");
});

// ---- Behaviour: the real methods, sliced out of steps_editor.js ------------ //

const INIT_PANEL = section("_initPanel() {", "_readPanelWidth() {");
const TAB_HELPERS = section("_setTabStop(target) {", "_openPanel(tab) {");
const SET_TAB = section("_setTab(tab) {", "// Reset to the default-collapsed state");
const RESET_PANEL = section("_resetPanel() {", "_syncPanelButtons() {");

const context = vm.createContext({});
vm.runInContext(
  `class Editor {${INIT_PANEL}\n${TAB_HELPERS}\n${SET_TAB}\n${RESET_PANEL}}\n` +
  "globalThis.Editor = Editor;",
  context);
const Editor = context.Editor;

const doc = { activeElement: null };

class FakeTab extends EventTarget {
  constructor(name) {
    super();
    this.dataset = { tab: name };
    this.attrs = new Map();
    this.classes = new Set();
    this.tabIndex = -1;
    this.classList = {
      toggle: (cls, on) => { if (on) this.classes.add(cls); else this.classes.delete(cls); },
      contains: (cls) => this.classes.has(cls),
    };
  }
  setAttribute(name, value) { this.attrs.set(name, value); }
  getAttribute(name) { return this.attrs.has(name) ? this.attrs.get(name) : null; }
  focus() { doc.activeElement = this; }
}

class FakeTabList extends EventTarget {
  constructor(tabs) { super(); this._tabs = tabs; }
  contains(node) { return this._tabs.indexOf(node) !== -1; }
}

function makeEditor() {
  const panes = [
    { dataset: { pane: "ref" }, hidden: false },
    { dataset: { pane: "json" }, hidden: true },
  ];
  const ed = new Editor();
  ed.activeTab = "ref";
  ed.panelEl = { hidden: false, querySelectorAll: () => panes };
  ed.panelTabs = [new FakeTab("ref"), new FakeTab("json")];
  ed.panelTabList = new FakeTabList(ed.panelTabs);
  ed.panelCollapseBtn = null;
  ed.refPanel = null;
  ed.widths = {};
  ed._initPanelResize = () => {};
  ed._stopJsonSync = () => {};
  ed._startJsonSync = () => {};
  ed._ensureRefPanel = () => false;
  ed._loadRefData = () => {};
  ed._syncPanelButtons = () => {};
  ed._nudgeView = () => {};
  // Record activation so a focus-only key press can be told apart from a select.
  const activations = [];
  const setTab = ed._setTab.bind(ed);
  ed._setTab = (tab) => { activations.push(tab); return setTab(tab); };
  ed._initPanel();
  return { ed, panes, tabs: ed.panelTabs, activations };
}

function press(tab, key, mods) {
  const event = Object.assign(new Event("keydown", { cancelable: true }), { key }, mods || {});
  return !tab.dispatchEvent(event);
}

test("_initPanel applies the default tab state and keeps the panel collapsed", () => {
  const { ed, tabs, panes } = makeEditor();
  const [ref, json] = tabs;
  assert.equal(ref.getAttribute("aria-selected"), "true");
  assert.equal(ref.tabIndex, 0);
  assert.equal(json.getAttribute("aria-selected"), "false");
  assert.equal(json.tabIndex, -1);
  assert.equal(ed.panelEl.hidden, true, "the panel still starts collapsed");
  assert.equal(panes[0].hidden, false);
  assert.equal(panes[1].hidden, true);
});

test("activating a tab syncs aria-selected, the tab stop and the panes", () => {
  const { ed, tabs, panes, activations } = makeEditor();
  const [ref, json] = tabs;
  json.dispatchEvent(new Event("click", { cancelable: true }));
  assert.deepEqual(activations, ["json"], "click activates the tab");
  assert.equal(json.getAttribute("aria-selected"), "true");
  assert.equal(json.tabIndex, 0);
  assert.equal(ref.getAttribute("aria-selected"), "false");
  assert.equal(ref.tabIndex, -1);
  assert.ok(json.classList.contains("is-on"), "the is-on class still tracks the tab");
  assert.ok(!ref.classList.contains("is-on"));
  assert.equal(panes[0].hidden, true);
  assert.equal(panes[1].hidden, false);
});

test("leaving the tablist returns the tab stop to the selected tab", () => {
  const { ed, tabs } = makeEditor();
  const [ref, json] = tabs;
  press(ref, "ArrowRight");
  assert.equal(json.tabIndex, 0, "arrowing moves the tab stop");
  assert.equal(ref.getAttribute("aria-selected"), "true", "arrowing does not select");
  ed.panelTabList.dispatchEvent(Object.assign(new Event("focusout"), { relatedTarget: json }));
  assert.equal(json.tabIndex, 0, "moving between tabs keeps the arrowed tab stop");
  ed.panelTabList.dispatchEvent(Object.assign(new Event("focusout"), { relatedTarget: null }));
  assert.equal(ref.tabIndex, 0, "the selected tab is the tab stop again");
  assert.equal(json.tabIndex, -1);
});

test("arrow keys move focus and the tab stop without activating", () => {
  const { ed, tabs, panes, activations } = makeEditor();
  const [ref, json] = tabs;
  assert.equal(press(ref, "ArrowRight"), true, "the handled key is default-prevented");
  assert.equal(doc.activeElement, json);
  assert.equal(json.tabIndex, 0);
  assert.equal(ref.tabIndex, -1);
  assert.deepEqual(activations, [], "focus movement must not select the tab");
  assert.equal(ed.activeTab, "ref");
  assert.equal(ref.getAttribute("aria-selected"), "true");
  assert.equal(json.getAttribute("aria-selected"), "false");
  assert.equal(panes[1].hidden, true, "the JSON pane stays hidden");

  press(json, "ArrowLeft");
  assert.equal(doc.activeElement, ref);
  assert.equal(press(ref, "ArrowLeft"), true, "ArrowLeft wraps to the last tab");
  assert.equal(doc.activeElement, json);
  press(json, "ArrowRight");
  assert.equal(doc.activeElement, ref, "ArrowRight wraps to the first tab");
});

test("Home and End jump to the first and last tab", () => {
  const { tabs } = makeEditor();
  const [ref, json] = tabs;
  doc.activeElement = json;
  assert.equal(press(json, "Home"), true);
  assert.equal(doc.activeElement, ref);
  assert.equal(press(ref, "End"), true);
  assert.equal(doc.activeElement, json);
});

test("modified arrows and other keys are left to the browser", () => {
  const { tabs, activations } = makeEditor();
  const [ref, json] = tabs;
  doc.activeElement = ref;
  [{ ctrlKey: true }, { altKey: true }, { metaKey: true }, { shiftKey: true }].forEach((mods) => {
    assert.equal(press(ref, "ArrowRight", mods), false, "no preventDefault with a modifier");
    assert.equal(doc.activeElement, ref);
  });
  assert.equal(press(ref, "ArrowDown"), false);
  assert.equal(press(ref, "Tab"), false);
  assert.equal(doc.activeElement, ref);
  assert.equal(json.tabIndex, -1, "the tab stop does not move");
  assert.deepEqual(activations, []);
});

test("_resetPanel returns the panel to the default 参考 state", () => {
  const { ed, tabs } = makeEditor();
  const [ref, json] = tabs;
  json.dispatchEvent(new Event("click", { cancelable: true }));
  ed._resetPanel();
  assert.equal(ed.activeTab, "ref");
  assert.equal(ref.getAttribute("aria-selected"), "true");
  assert.equal(ref.tabIndex, 0);
  assert.equal(json.getAttribute("aria-selected"), "false");
  assert.equal(json.tabIndex, -1);
  assert.equal(ed.panelEl.hidden, true);
});