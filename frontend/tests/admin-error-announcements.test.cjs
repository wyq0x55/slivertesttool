const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const root = path.resolve(__dirname, "../..");
const read = (file) => fs.readFileSync(path.join(root, file), "utf8");
const settle = () => new Promise((resolve) => setImmediate(resolve));

const ALERTS = [
  { file: "app/templates/lanmatrix/admin_db.html", id: "lm-db-tv-error" },
  { file: "app/templates/lanmatrix/admin_db.html", id: "lm-db-error" },
  { file: "app/templates/lanmatrix/admin_db.html", id: "lm-db-row-error" },
  { file: "app/templates/lanmatrix/editor.html", id: "lm-queue-error" },
  { file: "app/templates/lanmatrix/editor.html", id: "lm-export-error" },
  { file: "app/templates/lanmatrix/editor.html", id: "lm-import-error" },
];

function deferred() {
  let resolve;
  let reject;
  const promise = new Promise((accept, fail) => { resolve = accept; reject = fail; });
  return { promise, resolve, reject };
}

function section(source, start, end) {
  const first = source.indexOf(start);
  const last = source.indexOf(end, first);
  assert.ok(first >= 0 && last > first, `production section ${start}..${end} must exist`);
  return source.slice(first, last);
}

// Minimal element shim. `hidden` is a tracked accessor so a test can prove the
// production code never toggles it; the region is mounted (hidden === false).
function makeNode(id) {
  const node = {
    id,
    textContent: "",
    innerHTML: "",
    value: "",
    checked: false,
    disabled: false,
    open: false,
    className: "",
    dataset: {},
    selectedOptions: [],
    listeners: {},
    hiddenWrites: [],
    _hidden: false,
    addEventListener(type, callback) { this.listeners[type] = callback; },
    dispatch(type, event = {}) { return this.listeners[type]?.(event); },
    replaceChildren(...nodes) { this.children = nodes; },
    appendChild(node) { this.children.push(node); },
    querySelectorAll() { return []; },
    contains() { return false; },
    setAttribute() {},
    getAttribute() { return null; },
    removeAttribute() {},
    showModal() { this.open = true; },
    close() { this.open = false; if (this.listeners.close) this.listeners.close(); },
  };
  Object.defineProperty(node, "hidden", {
    configurable: true,
    get() { return node._hidden; },
    set(value) { node.hiddenWrites.push(value); node._hidden = value; },
  });
  return node;
}

function assertNeverHides(nodes) {
  for (const [id, element] of Object.entries(nodes)) {
    assert.deepEqual(element.hiddenWrites, [], `${id} must stay mounted instead of toggling hidden`);
  }
}

test("the six error divs are mounted alert regions", () => {
  const templates = new Map();
  for (const { file, id } of ALERTS) {
    if (!templates.has(file)) templates.set(file, read(file));
    const html = templates.get(file);
    const tag = html.match(new RegExp(`<div\\b[^>]*\\bid="${id}"[^>]*>`));
    assert.ok(tag, `expected a div with id=${id} in ${file}`);
    assert.match(tag[0], /\bclass="lm-error"/, `${id} keeps the lm-error styling hook`);
    assert.match(tag[0], /\brole="alert"/, `${id} must be an alert live region`);
    assert.doesNotMatch(tag[0], /\shidden(?=[\s>])/, `${id} must not start hidden`);
  }
});

function adminDbHarness() {
  const nodes = new Map();
  const get = (id) => {
    if (!nodes.has(id)) nodes.set(id, makeNode(id));
    return nodes.get(id);
  };
  const tabList = { addEventListener() {}, querySelectorAll() { return []; }, contains() { return false; } };
  const rootElement = {
    querySelector: (selector) => (selector === '[role="tablist"]' ? tabList : null),
    querySelectorAll: () => [],
  };
  const ready = deferred();
  const navButton = makeNode("t1-nav");
  navButton.dataset = { t: "t1" };
  navButton.classList = { toggle() {} };
  navButton.querySelectorAll = () => [];
  get("lm-db-tablenav").querySelectorAll = (selector) =>
    (selector === ".lm-db-tnav-btn" ? [navButton] : []);
  get("lm-db-readonly").checked = true;

  const api = {
    dbOverview: async () => ({
      backend: "postgres", database: "silver", db_user: "postgres",
      size_pretty: "8 MB", now: "now", version: "16",
      tables: [{ schema: "public", name: "t1", est_rows: 1, size_pretty: "8 kB" }],
    }),
    dbTables: async () => ({ tables: [{ name: "t1", est_rows: 1 }] }),
    dbTableSchema: async () => ({ primary_key: [], columns: [{ name: "id" }] }),
    dbTableRows: async () => ({
      primary_key: [], columns: ["id"], rows: [], pages: 1, page: 1, total: 0,
    }),
    dbQuery: async () => ({
      returns_rows: false, rowcount: 0, command: "UPDATE", elapsed_ms: 1, read_only: true,
    }),
    dbInsertRow: async () => ({}),
    dbUpdateRow: async () => ({}),
    dbDeleteRow: async () => ({}),
  };

  const context = vm.createContext({
    document: {
      activeElement: null,
      getElementById: get,
      querySelector: (selector) => (selector === ".lm-dbadmin" ? rootElement : null),
      querySelectorAll: () => [],
      createElement: () => makeNode("generic"),
    },
    window: { LMReady: ready.promise },
    LMApi: api,
    LMUI: { confirm: async () => true },
    URL: { createObjectURL: () => "blob:", revokeObjectURL() {} },
    setTimeout: () => 0,
  });
  vm.runInContext(read("app/static/js/lanmatrix/admin_db.js"), context, {
    filename: path.join(root, "app/static/js/lanmatrix/admin_db.js"),
  });
  ready.resolve();
  return { get, api, navButton, settle };
}

test("admin DB console clears and fills the mounted alerts on real failure paths", async () => {
  const harness = adminDbHarness();
  await settle();
  await settle();

  const sqlError = harness.get("lm-db-error");
  const tvError = harness.get("lm-db-tv-error");
  const rowError = harness.get("lm-db-row-error");
  const run = () => harness.get("lm-db-run").dispatch("click");

  harness.get("lm-db-sql").value = "   ";
  await run();
  assert.equal(sqlError.textContent, "请输入 SQL 语句");

  harness.get("lm-db-sql").value = "select 1";
  harness.api.dbQuery = async () => { throw new Error("db boom"); };
  await run();
  assert.equal(sqlError.textContent, "db boom");

  harness.api.dbQuery = async () => ({
    returns_rows: false, rowcount: 0, command: "UPDATE", elapsed_ms: 1, read_only: true,
  });
  await run();
  assert.equal(sqlError.textContent, "");

  harness.api.dbTableSchema = async () => { throw new Error("schema down"); };
  await harness.navButton.dispatch("click");
  await settle();
  assert.equal(tvError.textContent, "schema down");

  harness.api.dbTableSchema = async () => ({ primary_key: [], columns: [{ name: "id" }] });
  await harness.navButton.dispatch("click");
  await settle();
  assert.equal(tvError.textContent, "");

  rowError.textContent = "stale failure";
  harness.api.dbInsertRow = async () => { throw new Error("row boom"); };
  await harness.get("lm-db-row-save").dispatch("click", { preventDefault() {} });
  assert.equal(rowError.textContent, "row boom");

  harness.api.dbInsertRow = async () => ({});
  await harness.get("lm-db-row-save").dispatch("click", { preventDefault() {} });
  assert.equal(rowError.textContent, "");

  assertNeverHides({ sqlError, tvError, rowError });
});

test("editor queue dialog announces selection and enqueue failures without hiding the region", async () => {
  const source = read("app/static/js/lanmatrix/editor.js");
  const queueError = makeNode("lm-queue-error");
  const queueOk = makeNode("lm-queue-ok");
  const queueChecked = new Set();
  const dialog = makeNode("lm-queue-dialog");
  let enqueue = async () => { throw new Error("queue boom"); };
  const context = vm.createContext({
    queueError,
    queueOk,
    queueChecked,
    queueDialog: dialog,
    enqueueSelectedRuns: (...args) => enqueue(...args),
    window: { LMDefaultModel: "model" },
    toast() {},
    refreshTaskResults() {},
  });
  vm.runInContext(section(source, "  if (queueOk) {", "  // Fallback worksheet tab bar"), context, {
    filename: path.join(root, "app/static/js/lanmatrix/editor.js"),
  });
  const click = () => queueOk.dispatch("click", { preventDefault() {} });

  await click();
  assert.equal(queueError.textContent, "请至少选择一个 test_id。");

  queueChecked.add("case-1");
  await click();
  assert.equal(queueError.textContent, "queue boom");

  enqueue = async () => ({ created: ["case-1"], missing: [], errors: [] });
  await click();
  assert.equal(queueError.textContent, "");
  assert.equal(queueOk.disabled, false);

  assertNeverHides({ queueError });
});

test("editor export dialog clears and fills its mounted alert", async () => {
  const source = read("app/static/js/lanmatrix/editor.js");
  const nodes = new Map();
  const get = (id) => {
    if (!nodes.has(id)) nodes.set(id, makeNode(id));
    return nodes.get(id);
  };
  const api = {
    exportProject: async () => { throw new Error("export boom"); },
    testMatrixExportUrl: () => "/export/matrix",
    libFuncExportUrl: () => "/export/lib",
    constExportUrl: () => "/export/const",
    ioExportUrl: () => "/export/io",
  };
  const context = vm.createContext({
    document: {
      getElementById: get,
      createElement: () => ({ href: "", download: "", click() {} }),
    },
    LMApi: api,
    pid: 7,
    setTimeout: (callback) => callback(),
    URL: { createObjectURL: () => "blob:", revokeObjectURL() {} },
  });
  vm.runInContext(section(source,
    "  // --- Export dialog (unifies", "  // --- Import dialog ---"), context, {
    filename: path.join(root, "app/static/js/lanmatrix/editor.js"),
  });
  const exportError = get("lm-export-error");

  exportError.textContent = "stale failure";
  get("lm-export").dispatch("click");
  assert.equal(exportError.textContent, "");

  await get("lm-export-ok").dispatch("click", { preventDefault() {} });
  assert.equal(exportError.textContent, "export boom");

  api.exportProject = async () => ({});
  await get("lm-export-ok").dispatch("click", { preventDefault() {} });
  assert.equal(exportError.textContent, "");
  assert.equal(get("lm-export-dialog").open, false);

  get("lm-export-format").value = "legacy_judge";
  get("lm-export").dispatch("click");
  api.exportSilverTestcases = async (projectId, format) => {
    assert.equal(projectId, 7);
    assert.equal(format, "legacy_judge");
    throw new Error("invalid testcase step");
  };
  await get("lm-export-ok").dispatch("click", { preventDefault() {} });
  assert.equal(exportError.textContent, "invalid testcase step");
  assert.equal(get("lm-export-dialog").open, true);
  assert.equal(get("lm-export-ok").disabled, false);

  assertNeverHides({ exportError });
});

function importHarness() {
  const nodes = new Map();
  const get = (id) => {
    if (!nodes.has(id)) nodes.set(id, makeNode(id));
    return nodes.get(id);
  };
  get("lm-import-format").value = "test_matrix";
  get("lm-import-mode").value = "upsert";
  get("lm-import-dialog").open = true;
  const previews = [];
  const commits = [];
  const preview = (projectId, file, mode) => {
    const response = deferred();
    previews.push({ ...response, file, mode });
    return response.promise;
  };
  const context = vm.createContext({
    document: { getElementById: get, querySelectorAll: () => [] },
    pid: 1,
    currentSheet: "test",
    esc: (value) => String(value),
    isUniver: () => false,
    collabActive: () => false,
    applySheetContext: async () => {},
    loadFields: async () => {},
    loadItems: async () => {},
    toast() {},
    LMApi: {
      createImport: preview,
      importTestMatrix: preview,
      importLibFunc: preview,
      importConst: preview,
      importIo: preview,
      commitImport(jobId) {
        const response = deferred();
        commits.push({ ...response, jobId });
        return response.promise;
      },
    },
  });
  vm.runInContext(section(read("app/static/js/lanmatrix/editor.js"),
    "  // --- Import dialog ---", "  const extractIoBtn"), context, {
    filename: path.join(root, "app/static/js/lanmatrix/editor.js"),
  });
  const upload = (name = "cases.xlsx") =>
    get("lm-import-dialog-file").dispatch("change", { target: { files: [{ name }] } });
  const commit = () => get("lm-import-commit").dispatch("click", { preventDefault() {} });
  return { get, previews, commits, upload, commit };
}

const job = (id) => ({
  job: {
    id, status: "previewed", parameters: { format: "test_matrix" },
    preview: { total: 1, insert: 1, update: 0, invalid: 0 },
  },
});

test("editor import dialog clears and fills its mounted alert", async () => {
  const harness = importHarness();
  const importError = harness.get("lm-import-error");

  importError.textContent = "stale failure";
  harness.get("lm-import").dispatch("click");
  assert.equal(importError.textContent, "");

  harness.upload();
  harness.previews[0].reject(new Error("preview boom"));
  await settle();
  assert.equal(importError.textContent, "preview boom");

  importError.textContent = "stale failure";
  harness.get("lm-import").dispatch("click");
  harness.upload();
  harness.previews[1].resolve(job(2));
  await settle();
  assert.equal(importError.textContent, "");

  const committing = harness.commit();
  harness.commits[0].reject(new Error("permission denied"));
  await committing;
  assert.equal(importError.textContent, "permission denied");

  assertNeverHides({ importError });
});
