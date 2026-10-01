const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const root = path.resolve(__dirname, "../..");
const read = (file) => fs.readFileSync(path.join(root, file), "utf8");
const plain = (value) => JSON.parse(JSON.stringify(value));
const settle = () => new Promise((resolve) => setImmediate(resolve));

function deferred() {
  let resolve;
  let reject;
  const promise = new Promise((accept, fail) => { resolve = accept; reject = fail; });
  return { promise, resolve, reject };
}

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
      className: attrs.class || "", href: "", selectedOptions: [], style: {},
      classList: {
        contains: (name) => classes.has(name),
        add: (name) => classes.add(name),
        remove: (name) => classes.delete(name),
        toggle(name, on) { if (on) classes.add(name); else classes.delete(name); },
      },
      addEventListener(type, callback) { this.listeners[type] = callback; },
      setAttribute(name, value) { this[name] = value; },
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
          child.checked = /\bchecked\b/.test(match[2]);
          this.children.push(child);
        }
      },
    });
    return node;
  }
  for (const match of template.matchAll(/<\w+\b([^>]*\bid="[^"]+"[^>]*)>/g)) {
    const attrs = attributes(match[1]);
    const node = element(attrs.id, attrs);
    node.hidden = /\bhidden\b/.test(match[1]);
    node.disabled = /\bdisabled\b/.test(match[1]);
    nodes.set(node.id, node);
  }
  const get = (id) => {
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
      if (selector === ".lm-ai-drafts" || selector === ".lm-editor") return list;
      return null;
    },
    querySelectorAll() { return []; },
    addEventListener() {},
    createElement: () => element(), createTextNode: (text) => ({ textContent: text }),
  };
  get("lm-ai-d-refs").querySelectorAll = (selector) => get("lm-ai-refs-list").querySelectorAll(selector);
  return { get, document, list, nodes };
}

function draft(id, status = "pending", extra = {}) {
  return { id, project_id: 1, scenario: "procedure", status,
    input: { _context: { items: [{ id: 12, version: 3 }], provenance: [] } },
    output: { procedures: [{ ref: "12", steps_doc: {} }] }, meta: {}, ...extra };
}

function draftHarness(overrides = {}, role = "editor") {
  const environment = dom(read("app/templates/lanmatrix/ai_drafts.html"));
  const timers = new Map();
  let timerId = 0;
  const calls = [];
  const toasts = [];
  const api = {
    getProject: async () => ({ project: { is_editable: true }, role }),
    listAiDrafts: async () => [], aiUsage: async () => { throw new Error("no usage"); },
    aiGetSignals: async () => [], aiGetSettings: async () => ({}),
    getAiDraft: async (id) => draft(id),
    createAiDraft(scenario, projectId, payload) {
      calls.push({ scenario, projectId, payload: plain(payload) });
      return Promise.resolve(draft(50));
    },
    cancelAiDraft: async (id) => draft(id, "cancelled"),
    retryAiDraft: async (id) => draft(id, "running", { meta: { job: { state: "queued" } } }),
    updateAiDraft: async (id, output) => draft(id, "pending", { output }),
    approveAiDraft: async () => ({}), rejectAiDraft: async () => ({}),
    ...overrides,
  };
  const window = { location: { search: "", hash: "" }, addEventListener() {} };
  const context = vm.createContext({ window, document: environment.document,
    LM: { user: {} }, LMApi: api, LMReady: Promise.resolve({}), URLSearchParams,
    LMUI: { toast: (message, ok) => toasts.push({ message, ok }),
      confirm: async () => true, prompt: async () => "reason" },
    setTimeout(callback) { timers.set(++timerId, callback); return timerId; },
    clearTimeout(id) { timers.delete(id); },
  });
  const source = read("app/static/js/lanmatrix/ai_drafts.js").replace("  bindGenerate();",
    "  window.testOpenDetail = openDetail;\n  bindGenerate();");
  vm.runInContext(source, context, { filename: "ai_drafts.js" });
  environment.get("lm-ai-gen-scenario").value = "procedure";
  environment.get("lm-ai-gen-payload").value = "";
  environment.get("lm-ai-gen-item-ids").value = "12, 13";
  const click = async (id) => {
    const node = environment.get(id);
    const callback = node.listeners.click || node.onclick;
    assert.equal(typeof callback, "function", `${id} must have an executable click handler`);
    return callback({ preventDefault() {} });
  };
  return { ...environment, context, window, api, calls, toasts, timers, click,
    open: (id) => window.testOpenDetail(id),
    async tick() {
      const pending = [...timers.values()];
      timers.clear();
      for (const callback of pending) await callback();
      await settle();
    },
  };
}

function editorHarness(options = {}) {
  const environment = dom(read("app/templates/lanmatrix/editor.html"));
  const calls = [];
  const toasts = [];
  const persisted = options.persisted || [{ id: 12, uuid: "row-12", version: 3, title: "Saved" }];
  const api = {
    getProject: async () => ({ project: { code: "P", name: "Project", is_editable: options.editable !== false },
      role: options.role || "editor" }),
    listItems: async () => ({ items: persisted, total: persisted.length }),
    createAiDraft(scenario, projectId, payload) {
      calls.push({ scenario, projectId, payload: plain(payload) });
      return options.create ? options.create() : Promise.resolve(draft(50, "running"));
    },
    patchItem: options.patch || (async () => ({ item: persisted[0] })),
  };
  const grid = { engine: "builtin", getSelectedIds: () => options.ids || [12],
    isEditing: () => false, setData() {}, setFields() {}, ...options.grid };
  const collab = options.collab ? { isActive: () => true, getItems: () => options.live || persisted } : null;
  const window = { location: { href: "" }, addEventListener() {} };
  const context = vm.createContext({ document: environment.document, window, URLSearchParams,
    LMApi: api, LM: { user: {}, urls: {} }, LMReady: Promise.resolve({}),
    LMPill: { apply() {}, PROJECT_ZH: {} }, LMUI: { toast: (message, ok) => toasts.push({ message, ok }) },
    setTimeout: (callback) => setImmediate(callback), clearTimeout: clearImmediate,
    setInterval: () => 1, clearInterval() {}, console,
    testGrid: grid, testCollab: collab, testRows: persisted, testSheet: options.sheet || "test",
    testConn: options.disconnected ? "disconnected" : "connected",
  });
  const source = read("app/static/js/lanmatrix/editor.js").replace("  init();", `
    grid = testGrid; collab = testCollab; currentSheet = testSheet;
    sheetItems.test = testRows; sheetFields.test = [{ field_key: "title" }];
    fields = sheetFields.test; collabConn = testConn;
    window.testLoadProject = loadProject; window.testSaveCell = saveCell;
    window.testSelection = updateSelectionUI;
  `);
  vm.runInContext(source, context, { filename: "editor.js" });
  return { ...environment, api, grid, window, calls, toasts,
    async ready() { await window.testLoadProject(); window.testSelection(grid.getSelectedIds()); },
    async click() {
      const callback = environment.get("lm-ai-generate").listeners.click;
      assert.equal(typeof callback, "function", "matrix generation entry must be wired");
      return callback({ preventDefault() {} });
    },
  };
}

test("cancel and retry API use CSRF POST envelopes and return the same draft", async () => {
  const requests = [];
  const environment = dom("");
  const window = {};
  const context = vm.createContext({ window, document: environment.document, URLSearchParams, FormData,
    fetch: async (url, options) => {
      requests.push({ url, options });
      return { ok: true, status: 200, json: async () => ({ success: true,
        data: url.endsWith("/me") ? { user: {}, csrf_token: "csrf-test" } : draft(7, "cancelled") }) };
    },
  });
  vm.runInContext(read("app/static/js/lanmatrix/api.js"), context);
  await window.LMReady;
  assert.equal((await window.LMApi.cancelAiDraft(7)).id, 7);
  assert.equal((await window.LMApi.retryAiDraft(7)).id, 7);
  assert.deepEqual(requests.slice(1).map((entry) => entry.url),
    ["/api/v1/ai/drafts/7/cancel", "/api/v1/ai/drafts/7/retry"]);
  for (const entry of requests.slice(1)) {
    assert.equal(entry.options.method, "POST");
    assert.equal(entry.options.headers["X-CSRF-Token"], "csrf-test");
    assert.equal(entry.options.credentials, "same-origin");
    assert.deepEqual(JSON.parse(entry.options.body), {});
  }
});

test("matrix creates a procedure from persisted selected IDs and links its draft", async () => {
  const harness = editorHarness();
  await harness.ready();
  await harness.click();
  assert.deepEqual(harness.calls, [{ scenario: "procedure", projectId: 1, payload: { item_ids: [12] } }]);
  assert.equal(harness.get("lm-ai-draft-link").href, "/lanmatrix/projects/1/ai?draft=50");
  assert.equal(harness.get("lm-ai-draft-link").hidden, false);
});

test("matrix waits for a save and ignores duplicate submission clicks", async () => {
  const saving = deferred();
  const submitting = deferred();
  const harness = editorHarness({ patch: () => saving.promise, create: () => submitting.promise });
  await harness.ready();
  const save = harness.window.testSaveCell({ id: 12, version: 3 }, { title: "New" });
  const first = harness.click();
  await harness.click();
  await settle();
  assert.equal(harness.calls.length, 0);
  saving.resolve({ item: { id: 12, version: 4 } });
  await save;
  await settle();
  await settle();
  assert.equal(harness.calls.length, 1);
  assert.equal(harness.get("lm-ai-generate").disabled, true);
  submitting.resolve(draft(50));
  await first;
  assert.equal(harness.get("lm-ai-generate").disabled, false);
});

for (const options of [{ ids: [-1] }, { ids: [] }, { role: "reader" },
  { editable: false }, { sheet: "lib" }, { ids: [999] }]) {
  test(`matrix refuses unsafe selection ${JSON.stringify(options)}`, async () => {
    const harness = editorHarness(options);
    await harness.ready();
    await harness.click();
    assert.equal(harness.calls.length, 0);
  });
}

test("matrix refuses dirty collaborative rows even when ID and version match", async () => {
  const harness = editorHarness({ collab: true,
    live: [{ id: 12, uuid: "row-12", version: 3, title: "CRDT unsaved" }] });
  await harness.ready();
  await harness.click();
  assert.equal(harness.calls.length, 0);
  assert.match(harness.get("lm-ai-status").textContent, /同步|保存/);
});

test("matrix allows materialized collaboration and refuses a disconnected room", async () => {
  for (const disconnected of [false, true]) {
    const harness = editorHarness({ collab: true, disconnected });
    await harness.ready();
    await harness.click();
    assert.equal(harness.calls.length, disconnected ? 0 : 1);
  }
});

test("matrix save failure during submission blocks generation with visible feedback", async () => {
  const saving = deferred();
  const harness = editorHarness({ patch: () => saving.promise });
  await harness.ready();
  const save = harness.window.testSaveCell({ id: 12, version: 3 }, {}).catch(() => {});
  const submit = harness.click();
  saving.reject(new Error("save rejected"));
  await save;
  await submit;
  assert.equal(harness.calls.length, 0);
  assert.match(harness.get("lm-ai-status").textContent, /保存/);
});

test("procedure form sends ordinary item IDs with optional saved model", async () => {
  const harness = draftHarness();
  await settle();
  harness.get("lm-ai-gen-model-id").value = "4";
  await harness.click("lm-ai-gen-submit");
  assert.deepEqual(harness.calls, [{ scenario: "procedure", projectId: 1,
    payload: { item_ids: [12, 13], model_id: 4 } }]);
});

test("viewpoint form retains document name and revision as submitted provenance", async () => {
  const harness = draftHarness();
  await settle();
  harness.get("lm-ai-gen-scenario").value = "viewpoint";
  harness.get("lm-ai-gen-doc-text").value = "Design excerpt";
  harness.get("lm-ai-gen-source-name").value = "spec.md";
  harness.get("lm-ai-gen-source-revision").value = "r42";
  await harness.click("lm-ai-gen-submit");
  assert.deepEqual(harness.calls[0].payload,
    { doc_text: "Design excerpt", source_name: "spec.md", source_revision: "r42" });
});

test("library proposal uses persisted rows and a human proposal", async () => {
  const harness = draftHarness();
  await settle();
  harness.get("lm-ai-gen-scenario").value = "lib";
  harness.get("lm-ai-gen-proposal").value = "Reuse reset steps";
  await harness.click("lm-ai-gen-submit");
  assert.deepEqual(harness.calls[0].payload, { item_ids: [12, 13], proposal: "Reuse reset steps" });
});

test("SBS form submits saved model identity and explicit source excerpts", async () => {
  const harness = draftHarness();
  await settle();
  harness.get("lm-ai-gen-scenario").value = "sbs";
  harness.get("lm-ai-gen-model-id").value = "4";
  harness.get("lm-ai-gen-payload").value = '{"source_files":{"engine.c":"int speed;"}}';
  harness.get("lm-ai-gen-source-revision").value = "rev1";
  await harness.click("lm-ai-gen-submit");
  assert.deepEqual(harness.calls[0].payload,
    { model_id: 4, source_files: { "engine.c": "int speed;" }, source_revision: "rev1" });
});

test("failure analysis selects an archived task and one persisted row", async () => {
  const harness = draftHarness();
  await settle();
  harness.get("lm-ai-gen-scenario").value = "failure";
  harness.get("lm-ai-gen-item-ids").value = "12";
  harness.get("lm-ai-gen-task-key").value = "archived-run-42";
  await harness.click("lm-ai-gen-submit");
  assert.deepEqual(harness.calls[0].payload, { item_id: 12, task_key: "archived-run-42" });
});

for (const value of ["", "0", "12,12", "12.5", "1e3", "12,other"]) {
  test(`procedure rejects invalid ID input ${JSON.stringify(value)}`, async () => {
    const harness = draftHarness();
    await settle();
    harness.get("lm-ai-gen-item-ids").value = value;
    await harness.click("lm-ai-gen-submit");
    assert.equal(harness.calls.length, 0);
    assert.ok(harness.get("lm-ai-gen-status").textContent);
  });
}

test("advanced JSON cannot replace server-owned context or ordinary selection", async () => {
  for (const payload of [{ _context: {} }, { viewpoints: [{ item_id: 99 }] }, { item_ids: [99] },
    { log_text: "legacy log" }, { source_files: { "a.c": 5 } }]) {
    const harness = draftHarness();
    await settle();
    harness.get("lm-ai-gen-payload").value = JSON.stringify(payload);
    await harness.click("lm-ai-gen-submit");
    assert.equal(harness.calls.length, 0);
    assert.ok(harness.get("lm-ai-gen-status").textContent);
  }
});

test("generation disables repeated clicks and leaves server errors visible", async () => {
  const response = deferred();
  let requests = 0;
  const harness = draftHarness({ createAiDraft: () => { requests++; return response.promise; } });
  await settle();
  const first = harness.click("lm-ai-gen-submit");
  await harness.click("lm-ai-gen-submit");
  assert.equal(requests, 1);
  assert.equal(harness.get("lm-ai-gen-submit").disabled, true);
  response.reject(new Error("queue unavailable"));
  await first;
  assert.match(harness.get("lm-ai-gen-status").textContent, /queue unavailable/);
  assert.equal(harness.get("lm-ai-gen-submit").disabled, false);
});

test("running draft distinguishes queue and generation and supports cooperative cancel", async () => {
  const response = deferred();
  let requests = 0;
  const harness = draftHarness({ getAiDraft: async (id) => draft(id, "running",
    { meta: { job: { state: "queued" } } }), cancelAiDraft: () => { requests++; return response.promise; } });
  await settle();
  await harness.open(1);
  assert.match(harness.get("lm-ai-d-status").textContent, /排队/);
  assert.equal(harness.get("lm-ai-d-approve").disabled, true);
  const cancel = harness.click("lm-ai-d-cancel");
  await harness.click("lm-ai-d-cancel");
  assert.equal(requests, 1);
  assert.match(harness.get("lm-ai-d-action-status").textContent, /取消/);
  response.resolve(draft(1, "running", { meta: { job: { state: "generating", cancel_requested: true } } }));
  await cancel;
  assert.match(harness.get("lm-ai-d-status").textContent, /生成|取消/);
  assert.equal(harness.get("lm-ai-d-cancel").disabled, true);
  assert.equal(harness.get("lm-ai-d-retry").disabled, true);
});

test("retry is restricted to failed or cancelled drafts and preserves original draft identity", async () => {
  const retries = [];
  const harness = draftHarness({ getAiDraft: async (id) => draft(id, "cancelled"),
    retryAiDraft: async (id) => { retries.push(id); return draft(id, "running", { meta: { job: { state: "queued" } } }); } });
  await settle();
  await harness.open(1);
  await harness.click("lm-ai-d-retry");
  assert.deepEqual(retries, [1]);
  assert.equal(harness.calls.length, 0);
  assert.match(harness.get("lm-ai-d-title").textContent, /#1/);
  assert.equal(harness.get("lm-ai-d-retry").disabled, true);
});

test("old detail fetch cannot replace the most recently opened draft", async () => {
  const first = deferred();
  const harness = draftHarness({ getAiDraft: (id) => id === 1 ? first.promise : Promise.resolve(draft(id)) });
  const old = harness.open(1);
  await harness.open(2);
  first.resolve(draft(1));
  await old;
  assert.match(harness.get("lm-ai-d-title").textContent, /#2/);
});

test("running draft poll cannot reopen or replace another detail after navigation", async () => {
  const harness = draftHarness({ getAiDraft: async (id) => draft(id, id === 1 ? "running" : "pending") });
  await harness.open(1);
  await harness.open(2);
  await harness.tick();
  assert.match(harness.get("lm-ai-d-title").textContent, /#2/);
  await harness.click("lm-ai-d-close");
  await harness.tick();
  assert.equal(harness.get("lm-ai-detail").hidden, true);
});

test("inline save completion cannot replace a different detail", async () => {
  const update = deferred();
  const harness = draftHarness({ updateAiDraft: () => update.promise });
  await settle();
  await harness.open(1);
  await harness.click("lm-ai-d-edit");
  assert.equal(harness.get("lm-ai-d-approve").disabled, true);
  const saving = harness.click("lm-ai-d-edit");
  await harness.open(2);
  update.resolve(draft(1));
  await saving;
  await settle();
  assert.match(harness.get("lm-ai-d-title").textContent, /#2/);
  assert.ok(harness.nodes.has("lm-ai-d-output"));
});

test("stale approval confirmation never decides a draft after switching details", async () => {
  const confirmation = deferred();
  const approvals = [];
  const harness = draftHarness({ approveAiDraft: async (id) => approvals.push(id) });
  harness.context.LMUI.confirm = () => confirmation.promise;
  await settle();
  await harness.open(1);
  const deciding = harness.click("lm-ai-d-approve");
  await harness.open(2);
  confirmation.resolve(true);
  await deciding;
  assert.deepEqual(approvals, []);
  assert.match(harness.get("lm-ai-d-title").textContent, /#2/);
});

test("readers can inspect drafts but cannot generate, edit, cancel or retry", async () => {
  const harness = draftHarness({}, "reader");
  await settle();
  await harness.open(1);
  for (const id of ["lm-ai-open-gen", "lm-ai-gen-submit", "lm-ai-d-edit", "lm-ai-d-approve", "lm-ai-d-reject"]) {
    assert.equal(harness.get(id).disabled, true, id);
  }
  await harness.click("lm-ai-gen-submit");
  assert.equal(harness.calls.length, 0);
});
