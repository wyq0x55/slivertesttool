const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const root = path.resolve(__dirname, "../..");
const editorSource = fs.readFileSync(path.join(root, "app/static/js/lanmatrix/editor.js"), "utf8");
const stepsSource = fs.readFileSync(path.join(root, "app/static/js/lanmatrix/steps_editor.js"), "utf8");
const tasksSource = fs.readFileSync(path.join(root, "app/static/js/lanmatrix/project_tasks.js"), "utf8");

function sliceBetween(source, startMarker, endMarker) {
  const start = source.indexOf(startMarker);
  const end = source.indexOf(endMarker, start);
  assert.ok(start >= 0 && end > start, `missing section ${startMarker} .. ${endMarker}`);
  return source.slice(start, end);
}

test("executionSnapshot fetches unfiltered sheets even when quick search is active", async () => {
  const fetchSection = sliceBetween(
    editorSource,
    "  async function fetchDbItems(sheet, opts)",
    "  // After a server-side import"
  );
  const rowIdSection = sliceBetween(
    editorSource,
    "  function rowTestId(item)",
    "  // Server-authoritative run results"
  );
  const snapSection = sliceBetween(
    editorSource,
    "  async function waitForCommittedEdits()",
    "  function queueCandidates()"
  );

  const queries = [];
  const context = vm.createContext({
    FETCH_CHUNK: 500,
    quick: "TC-1",
    currentSheet: "test",
    pid: 9,
    IS_BOOKS: false,
    savingCount: 0,
    grid: null,
    collabConn: "disconnected",
    remoteRowErrors: {},
    document: { activeElement: null },
    window: { __LM_COLLAB_MATERIALIZE_WAIT_MS: 50, __LM_COLLAB_MATERIALIZE_POLL_MS: 5 },
    setTimeout: (fn) => { fn(); return 1; },
    Promise,
    Set,
    Date,
    Error,
    Array,
    String,
    collabActive: () => false,
    collab: null,
    LMApi: {
      async listItems(_pid, params) {
        queries.push({ ...params });
        if (params.sheet === "test") {
          return { total: 2, items: [
            { uuid: "u1", test_id: "TC-1", steps: "{}" },
            { uuid: "u2", test_id: "TC-2", steps: "{}" },
          ] };
        }
        if (params.sheet === "lib") {
          return { total: 1, items: [{ uuid: "l1", lib_func: "Init", lib_stb: "{}", lib_para: "", isinit: "1" }] };
        }
        return { total: 1, items: [{ uuid: "c1", const_name: "K", const_value: "10" }] };
      },
    },
  });

  vm.runInContext(`${fetchSection}
${rowIdSection}
${snapSection}`, context);
  const snap = await context.executionSnapshot(["TC-1"]);
  assert.equal(queries.some((q) => "q" in q), false, "snapshot queries must not carry quick filter q");
  assert.equal(snap.tests.length, 1);
  assert.equal(snap.tests[0].test_id, "TC-1");
  assert.equal(snap.lib.length, 1);
  assert.equal(snap.const.length, 1);
});

test("enqueueSelectedRuns retries COLLAB_INPUTS_NOT_MATERIALIZED during collab debounce and stops on row error", async () => {
  const rowIdSection = sliceBetween(
    editorSource,
    "  function rowTestId(item)",
    "  // Server-authoritative run results"
  );
  const snapSection = sliceBetween(
    editorSource,
    "  async function waitForCommittedEdits()",
    "  function queueCandidates()"
  );

  let attempts = 0;
  let stepText = '{"steps":[{"no":1}]}';
  let rowErrors = {};
  const context = vm.createContext({
    pid: 9,
    IS_BOOKS: false,
    savingCount: 0,
    grid: null,
    collabConn: "connected",
    get remoteRowErrors() { return rowErrors; },
    document: { activeElement: null },
    window: { __LM_COLLAB_MATERIALIZE_WAIT_MS: 200, __LM_COLLAB_MATERIALIZE_POLL_MS: 5 },
    setTimeout,
    Promise,
    Set,
    Date,
    Error,
    Array,
    String,
    collabActive: () => true,
    fetchAllItems: async (sheet) => {
      if (sheet === "test") return [{ uuid: "u1", test_id: "TC-1", steps: stepText }];
      if (sheet === "lib") return [];
      return [];
    },
    LMApi: {
      async runSelectedTasks(_pid, payload) {
        attempts += 1;
        if (attempts < 3) {
          stepText = '{"steps":[{"no":2}]}';
          const err = new Error("协同编辑尚未写入数据库，请等待同步后重新运行");
          err.code = "COLLAB_INPUTS_NOT_MATERIALIZED";
          err.details = { conflicts: [{ sheet: "test", uuid: "u1" }] };
          throw err;
        }
        return { created: [{ test_id: "TC-1", task_id: "t-1" }], snapshot: payload.expected_inputs };
      },
    },
  });

  vm.runInContext(`${rowIdSection}
${snapSection}`, context);
  const res = await context.enqueueSelectedRuns(["TC-1"], "plant@v1");
  assert.equal(attempts, 3);
  assert.equal(res.created.length, 1);
  assert.equal(res.snapshot.tests[0].steps, '{"steps":[{"no":2}]}');

  // When Materializer reported a validation error for the conflicted row, fail fast without polling.
  attempts = 0;
  rowErrors = { u1: { message: "bad row" } };
  await assert.rejects(
    () => context.enqueueSelectedRuns(["TC-1"], "plant@v1"),
    (err) => err && err.code === "COLLAB_INPUTS_NOT_MATERIALIZED"
  );
  assert.equal(attempts, 1);
});

test("steps_editor open() binds onPrepare and awaits it before onEnqueue", async () => {
  const enqueueMethod = sliceBetween(
    stepsSource,
    "    async _enqueue() {",
    "    // Toggle between the bottom drawer"
  );
  const openAssign = sliceBetween(
    stepsSource,
    "    open(item, opts) {",
    "      // The Reference Panel always starts collapsed"
  );
  assert.match(openAssign, /this\.onPrepare\s*=\s*\(opts\s*&&\s*opts\.onPrepare\)\s*\|\|\s*null/);

  const order = [];
  let suppressedDuringPrepare = false;
  const context = vm.createContext({ JSON, clearTimeout, setTimeout: (fn) => { fn(); return 1; } });
  vm.runInContext(`this.Host = class { ${enqueueMethod} };`, context);
  const inst = new context.Host();
  Object.assign(inst, {
    testId: "TC-1",
    enqueueBtn: { disabled: false },
    errEl: { hidden: true, textContent: "" },
    _flushTimer: 42,
    _suppressRemote: false,
    async _flushEdit() { order.push("flushEdit"); },
    _pull() { order.push("pull"); },
    _serialize() { return { steps: [{ no: 1 }] }; },
    async onPrepare(json) {
      suppressedDuringPrepare = this._suppressRemote === true;
      order.push("prepare:" + json);
    },
    async onEnqueue(tid) { order.push("enqueue:" + tid); },
    _setStatus(s) { order.push("status:" + s); },
    _refreshStatus() { order.push("refresh"); },
  });
  await inst._enqueue();
  assert.equal(inst._flushTimer, null);
  assert.equal(suppressedDuringPrepare, true);
  assert.equal(inst._suppressRemote, false);
  assert.deepEqual(order, [
    "flushEdit",
    "pull",
    'prepare:{"steps":[{"no":1}]}',
    "enqueue:TC-1",
    "status:queued",
    "refresh",
  ]);
});

test("project_tasks onEnqueue resolves case_id fallback via logicalId", () => {
  const openStepsSection = sliceBetween(
    tasksSource, "  async function openSteps(", "  function closeDetail("
  );
  assert.match(openStepsSection, /items\.filter\(\(item\)\s*=>\s*logicalId\(item\)\s*===\s*targetId\)/);
});

test("project_tasks keeps the steps drawer in the task's book and row", async () => {
  const rowInOtherBook = { uuid: "other-row", test_id: "TC-1", book_id: 6 };
  const replacement = { uuid: "replacement", case_id: "TC-1", book_id: 7 };
  const opened = [];
  const messages = [];
  const stepsEditor = { open: (item) => opened.push(item) };
  const context = vm.createContext({
    window: { LMStepsEditor: stepsEditor },
    LMStepsEditor: stepsEditor,
    fetchTestItems: async () => [rowInOtherBook, replacement],
    toast: (message) => messages.push(message),
  });
  vm.runInContext(sliceBetween(
    tasksSource, "  async function openSteps(", "  function closeDetail("
  ), context);

  await context.openSteps("TC-1", "deleted-row", 7);
  assert.deepEqual(opened, []);
  assert.match(messages[0], /未找到/);

  await context.openSteps("TC-1", "", 7);
  assert.deepEqual(opened, [replacement]);
});
