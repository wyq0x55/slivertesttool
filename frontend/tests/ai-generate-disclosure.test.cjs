const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const root = path.resolve(__dirname, "../..");
const read = (file) => fs.readFileSync(path.join(root, file), "utf8");
const settle = () => new Promise((resolve) => setImmediate(resolve));

const TEMPLATE = "app/templates/lanmatrix/ai_drafts.html";
const SCRIPT = "app/static/js/lanmatrix/ai_drafts.js";

function deferred() {
  let resolve;
  let reject;
  const promise = new Promise((accept, fail) => { resolve = accept; reject = fail; });
  return { promise, resolve, reject };
}

// Same shim shape as ai-workflow.test.cjs: a node carries the template
// attributes, and setAttribute() keeps both the attribute map and the
// reflected property in sync so aria-expanded can be asserted either way.
function dom(template) {
  const nodes = new Map();
  const attributes = (source) => {
    const result = {};
    for (const match of source.matchAll(/([\w-]+)="([^"]*)"/g)) result[match[1]] = match[2];
    return result;
  };
  function element(id = "", attrs = {}) {
    const classes = new Set((attrs.class || "").split(/\s+/));
    const node = {
      id, value: "", textContent: "", hidden: false, disabled: false,
      checked: false, open: false, dataset: {}, listeners: {}, children: [],
      className: attrs.class || "", href: "", selectedOptions: [], style: {}, tagName: "",
      classList: {
        contains: (name) => classes.has(name),
        add: (name) => classes.add(name),
        remove: (name) => classes.delete(name),
        toggle(name, on) { if (on) classes.add(name); else classes.delete(name); },
      },
      addEventListener(type, callback) { this.listeners[type] = callback; },
      getAttribute(name) { return attrs[name] ?? null; },
      setAttribute(name, value) { attrs[name] = String(value); this[name] = value; },
      appendChild(child) { this.children.push(child); return child; },
      querySelector() { return null; },
      querySelectorAll(selector) {
        if (selector.includes("data-view")) return this.children.filter((child) => child.dataset.view);
        if (selector.includes("data-ref")) return this.children.filter((child) =>
          child.dataset.ref !== undefined && (!selector.includes(":checked") || child.checked));
        return [];
      },
      replaceWith(replacement) { nodes.delete(this.id); nodes.set(replacement.id, replacement); },
      showModal() { this.open = true; },
      close() { this.open = false; if (this.listeners.close) this.listeners.close(); },
    };
    for (const [name, value] of Object.entries(attrs)) {
      if (name.startsWith("data-")) node.dataset[name.slice(5).replace(/-([a-z])/g,
        (_, letter) => letter.toUpperCase())] = value;
    }
    Object.defineProperty(node, "innerHTML", {
      get() { return this.html || ""; },
      set(value) {
        this.html = value;
        this.children = [];
        for (const match of value.matchAll(/<(input|button)\b([^>]*)>/g)) {
          const attrs2 = attributes(match[2]);
          const child = element(attrs2.id || "", attrs2);
          child.tagName = match[1].toUpperCase();
          child.checked = /\bchecked\b/.test(match[2]);
          this.children.push(child);
        }
      },
    });
    return node;
  }
  let groupCount = 0;
  for (const match of template.matchAll(/<(\w+)\b([^>]*)>/g)) {
    const attrs = attributes(match[2]);
    if (!attrs.id && !attrs["data-ai-scenarios"]) continue;
    const node = element(attrs.id || `group-${++groupCount}`, attrs);
    node.tagName = match[1].toUpperCase();
    node.hidden = /\bhidden\b/.test(match[2]);
    node.disabled = /\bdisabled\b/.test(match[2]);
    nodes.set(node.id, node);
  }
  const statusGroup = nodes.get("lm-ai-f-seg");
  if (statusGroup) statusGroup.innerHTML = template.match(/<div\b[^>]* id="lm-ai-f-seg"[^>]*>([\s\S]*?)<\/div>/)[1];
  const get = (id) => {
    if (id === "lm-ai-settings") return null;
    if (id === "lm-ai-d-output-edit" && !nodes.has(id)) return null;
    if (!nodes.has(id)) nodes.set(id, element(id));
    return nodes.get(id);
  };
  const list = element("list", { "data-project": "1" });
  list.dataset.projectId = "1";
  list.dataset.collab = "0";
  const document = {
    hidden: false, activeElement: null, readyState: "complete",
    getElementById: get,
    querySelector(selector) {
      if (selector === ".lm-ai-drafts") return list;
      return null;
    },
    querySelectorAll(selector) {
      if (selector === "[data-ai-scenarios]") return [...nodes.values()].filter((node) => node.dataset.aiScenarios);
      if (selector === "#lm-ai-f-seg button") return statusGroup?.children.filter((node) => node.tagName === "BUTTON") || [];
      if (selector.includes("#lm-ai-gen-panel")) return [...nodes.values()].filter((node) =>
        node.id.startsWith("lm-ai-gen-") && ["INPUT", "SELECT", "TEXTAREA"].includes(node.tagName));
      return [];
    },
    addEventListener() {},
    createElement: () => element(), createTextNode: (text) => ({ textContent: text }),
  };
  get("lm-ai-d-refs").querySelectorAll = (selector) => get("lm-ai-refs-list").querySelectorAll(selector);
  return { get, document, list, nodes };
}

function disclosureHarness({ role = "editor", createAiDraft } = {}) {
  const environment = dom(read(TEMPLATE));
  const timers = new Map();
  let timerId = 0;
  const api = {
    getProject: async () => ({ project: { is_editable: true }, role }),
    listAiDrafts: async () => [], aiUsage: async () => { throw new Error("no usage"); },
    aiGetSignals: async () => [], aiGetSettings: async () => ({}),
    getAiDraft: async (id) => ({ id, project_id: 1, scenario: "procedure", status: "pending",
      input: { _context: { items: [], provenance: [] } }, output: {}, meta: {} }),
    createAiDraft: createAiDraft || (async () => ({ id: 50, project_id: 1, scenario: "procedure",
      status: "pending", input: { _context: { items: [] } }, output: {}, meta: {} })),
    cancelAiDraft: async (id) => ({ id, status: "cancelled" }),
    retryAiDraft: async (id) => ({ id, status: "running", meta: { job: { state: "queued" } } }),
    updateAiDraft: async (id, output) => ({ id, status: "pending", output }),
    approveAiDraft: async () => ({}), rejectAiDraft: async () => ({}),
  };
  const window = { location: { search: "", hash: "" }, listeners: {},
    addEventListener(type, callback) { this.listeners[type] = callback; } };
  const context = vm.createContext({ window, document: environment.document,
    LM: { user: {} }, LMApi: api, LMReady: Promise.resolve({}), URLSearchParams,
    LMUI: { toast() {}, confirm: async () => true, prompt: async () => "reason" },
    setTimeout(callback) { timers.set(++timerId, callback); return timerId; },
    clearTimeout(id) { timers.delete(id); },
  });
  vm.runInContext(read(SCRIPT), context, { filename: path.join(root, SCRIPT) });

  const button = environment.get("lm-ai-open-gen");
  const panel = environment.get("lm-ai-gen-panel");
  const click = (node) => node.listeners.click({
    currentTarget: node, target: node, preventDefault() {}, stopPropagation() {},
  });
  return {
    environment, button, panel,
    openToggle() { return click(button); },
    submit() {
      environment.get("lm-ai-gen-scenario").value = "procedure";
      environment.get("lm-ai-gen-item-ids").value = "12, 13";
      return click(environment.get("lm-ai-gen-submit"));
    },
  };
}

test("the generate toggle advertises the panel it controls", () => {
  const template = read(TEMPLATE);
  const tag = template.match(/<button\b[^>]*\bid="lm-ai-open-gen"[^>]*>/);
  assert.ok(tag, "ai_drafts.html must define #lm-ai-open-gen");
  assert.match(tag[0], /aria-expanded="false"/);
  assert.match(tag[0], /aria-controls="lm-ai-gen-panel"/);
  assert.match(tag[0], /type="button"/);
  assert.match(tag[0], /\bdisabled\b/);
  assert.ok(/<div\b[^>]*\bid="lm-ai-gen-panel"[^>]*\bhidden\b/.test(template),
    "the controlled panel starts hidden, so aria-expanded=false is the initial state");
});

test("clicking the toggle opens the panel and reports aria-expanded=true", async () => {
  const harness = disclosureHarness();
  await settle();
  assert.equal(harness.panel.hidden, true);
  assert.equal(harness.button.getAttribute("aria-expanded"), "false");

  harness.openToggle();
  assert.equal(harness.panel.hidden, false);
  assert.equal(harness.button.getAttribute("aria-expanded"), "true");
});

test("clicking the toggle again closes the panel and reports aria-expanded=false", async () => {
  const harness = disclosureHarness();
  await settle();
  harness.openToggle();
  harness.openToggle();

  assert.equal(harness.panel.hidden, true);
  assert.equal(harness.button.getAttribute("aria-expanded"), "false");
});

test("read-only roles cannot open the panel or change aria-expanded", async () => {
  const harness = disclosureHarness({ role: "reader" });
  await settle();

  harness.openToggle();
  assert.equal(harness.panel.hidden, true);
  assert.equal(harness.button.getAttribute("aria-expanded"), "false");
});

test("a running generation leaves the panel and aria-expanded untouched", async () => {
  const pending = deferred();
  const harness = disclosureHarness({ createAiDraft: () => pending.promise });
  await settle();
  harness.submit();

  harness.openToggle();
  assert.equal(harness.panel.hidden, true);
  assert.equal(harness.button.getAttribute("aria-expanded"), "false");

  pending.resolve({ id: 50, project_id: 1, scenario: "procedure", status: "pending",
    input: { _context: { items: [] } }, output: {}, meta: {} });
  await settle();
});