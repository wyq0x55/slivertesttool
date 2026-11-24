const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");
const Y = require("yjs");

const root = path.resolve(__dirname, "../..");

function readRepoFile(relPath) {
  return fs.readFileSync(path.join(root, relPath), "utf8");
}

function loadCollabController() {
  const source = readRepoFile("app/static/js/lanmatrix/collab.js");
  const window = {
    location: { protocol: "http:", hostname: "localhost", href: "http://localhost/lanmatrix" },
    LMCollab: { Y, WebsocketProvider: function () {}, Awareness: function () {} },
  };
  window.window = window;
  const context = vm.createContext({
    window,
    console,
    URL,
    setTimeout,
    clearTimeout,
  });
  vm.runInContext(source, context);
  const ctrl = window.LMCollabController.create({
    pid: 1,
    sheets: ["test", "const", "lib"],
    rowPrefix: "rows:",
  });
  const doc = new Y.Doc();
  ctrl.doc = doc;
  ctrl._active = true;
  ["test", "const", "lib"].forEach((s) => {
    ctrl.arrays[s] = doc.getArray("rows:" + s);
  });
  return { ctrl, doc };
}

function seedRows(ctrl, doc, sheet, rows) {
  const arr = ctrl.arrays[sheet];
  doc.transact(() => {
    const maps = rows.map(() => new Y.Map());
    arr.insert(arr.length, maps);
    rows.forEach((row, idx) => {
      const m = maps[idx];
      Object.keys(row).forEach((k) => m.set(k, row[k]));
    });
  });
}

async function loadApi() {
  const source = readRepoFile("app/static/js/lanmatrix/api.js");
  const calls = [];
  const document = {
    readyState: "loading",
    documentElement: { getAttribute: () => null, setAttribute() {}, removeAttribute() {} },
    addEventListener() {},
    getElementById: () => null,
    querySelector: () => null,
    querySelectorAll: () => [],
  };
  const window = {
    document,
    location: { pathname: "/lanmatrix/projects/1" },
    localStorage: { getItem: () => null, setItem() {} },
  };
  window.window = window;
  const fetch = async (url, opts) => {
    calls.push({
      url,
      method: opts.method,
      headers: Object.assign({}, opts.headers),
      body: opts.body ? JSON.parse(opts.body) : undefined,
    });
    return {
      ok: true,
      status: 200,
      text: async () => JSON.stringify({
        success: true,
        data: { csrf_token: "boot-tok", user: { id: 1 }, moved: true },
      }),
    };
  };
  const context = vm.createContext({
    window,
    document,
    localStorage: window.localStorage,
    fetch,
    FormData: class FormData {},
    URLSearchParams,
    setTimeout,
    clearTimeout,
  });
  vm.runInContext(source, context);
  if (window.LMReady) await window.LMReady;
  calls.length = 0;
  window.LMApi.setToken("csrf-tok");
  return { LMApi: window.LMApi, calls };
}

test("editor.html keeps the run controls in the toolbar and task templates prefer ESM lazy-load", () => {
  const editorHtml = readRepoFile("app/templates/lanmatrix/editor.html");
  assert.doesNotMatch(editorHtml, /\{%\s*block\s+topbar_actions\s*%\}/,
    "the editor page header is gone; execution settings live on 执行配置");

  // Ribbon/fallback order: row tools, 导入, 导出, 队列测试, 帮助.
  const toolbarStart = editorHtml.indexOf('id="lm-toolbar-right"');
  assert.ok(toolbarStart >= 0, "editor.html must keep #lm-toolbar-right");
  const toolbarEnd = editorHtml.indexOf('id="lm-sheet-tabs"', toolbarStart);
  assert.ok(toolbarEnd > toolbarStart, "#lm-sheet-tabs follows the toolbar");
  const toolbar = editorHtml.slice(toolbarStart, toolbarEnd);
  const order = ["lm-rowtools", "lm-import", "lm-export", "lm-run-selected", "lm-help"]
    .map((id) => ({ id, at: toolbar.indexOf(`id="${id}"`) }));
  for (const entry of order) assert.ok(entry.at >= 0, `#lm-toolbar-right must include #${entry.id}`);
  assert.deepEqual([...order].sort((a, b) => a.at - b.at).map((entry) => entry.id),
    ["lm-rowtools", "lm-import", "lm-export", "lm-run-selected", "lm-help"],
    "队列测试 must sit between 导出 and 帮助");

  for (const movedId of ["lm-ai-generate", "lm-ai-model-id", "lm-ai-draft-link", "lm-topbar-ai", "lm-topbar-exec"]) {
    assert.doesNotMatch(editorHtml, new RegExp(`id="${movedId}"`), `#${movedId} moved to the 执行配置 page`);
  }
  const execHtml = readRepoFile("app/templates/lanmatrix/exec_config.html");
  for (const id of ["lm-ai-generate", "lm-ai-model-id", "lm-ai-row-ids", "lm-ai-draft-link", "lm-signal-list-btn", "lm-clean-room-toggle"]) {
    assert.match(execHtml, new RegExp(`id="${id}"`), `执行配置 must include #${id}`);
  }

  assert.match(editorHtml, /class="[^"]*lm-fallback-only[^"]*"[^>]*id="lm-proj-title"|id="lm-proj-title"[^>]*class="[^"]*lm-fallback-only[^"]*"/);
  assert.match(editorHtml, /class="[^"]*lm-fallback-only[^"]*"[^>]*id="lm-grid-engine"|id="lm-grid-engine"[^>]*class="[^"]*lm-fallback-only[^"]*"/);

  for (const tpl of [
    "app/templates/lanmatrix/home.html",
    "app/templates/lanmatrix/project_tasks.html",
    "app/templates/lanmatrix/ut_tasks.html",
  ]) {
    const html = readRepoFile(tpl);
    assert.match(html, /\{%\s*if\s+univer_esm_available\s*%\}/, `${tpl} must branch on univer_esm_available`);
    assert.match(html, /vendor\/univer\/esm\/univer-core\.js/, `${tpl} must prefetch ESM univer-core.js`);
    assert.match(html, /data-esm="1"/, `${tpl} must mark ESM prefetch link with data-esm="1"`);
    assert.match(html, /vendor\/univer\/univer\.full\.umd\.js/, `${tpl} must retain UMD fallback`);
  }

  const pagesPy = readRepoFile("app/routes/lanmatrix_pages.py");
  const esmMatches = pagesPy.match(/univer_esm_available\s*=/g) || [];
  assert.ok(esmMatches.length >= 4, "editor, workspace, ut_tasks, and project_tasks must pass univer_esm_available");
});

test("editor.js passes toolbarRight: null and wires Univer native menu callbacks", () => {
  const editorJs = readRepoFile("app/static/js/lanmatrix/editor.js");
  assert.match(editorJs, /toolbarRight:\s*null/);
  assert.match(editorJs, /onInsert:\s*\(item,\s*where,\s*count\)\s*=>\s*insertRowAt\(item,\s*where,\s*count\)/);
  assert.match(editorJs, /onMoveRows:\s*\(ids,\s*targetId,\s*place\)\s*=>\s*moveRowsTo\(ids,\s*targetId,\s*place\)/);
  assert.match(editorJs, /onImport:\s*\(\)\s*=>\s*openImportDialog\(\)/);
  assert.match(editorJs, /onExport:\s*\(\)\s*=>\s*openExportDialog\(\)/);
  assert.match(editorJs, /onHelp:\s*\(\)\s*=>\s*toggleHelp\(\)/);
  assert.doesNotMatch(editorJs, /function\s+univerCtxFor\b/);
  assert.doesNotMatch(editorJs, /function\s+applyUniverRowFormats\b/);
  assert.ok(editorJs.includes("  // --- Import dialog ---"));
  assert.ok(editorJs.includes("  const extractIoBtn"));
});

test("collab.insertRow inserts multiple rows in one transaction with distinct UUIDs and inherited category", () => {
  const { ctrl, doc } = loadCollabController();
  seedRows(ctrl, doc, "test", [
    { id: 10, uuid: "u-10", category: "2", category_name: "異常系", test_id: "TC-01" },
    { id: 20, uuid: "u-20", category: "3", category_name: "境界値", test_id: "TC-02" },
  ]);

  let txCount = 0;
  doc.on("afterTransaction", () => { txCount += 1; });

  const firstInserted = ctrl.insertRow("test", {
    anchorId: 10,
    place: "below",
    count: 3,
    values: { draft: true },
  });

  assert.equal(txCount, 1, "all 3 rows must be inserted in one Y.Doc transaction");
  const items = ctrl.getItems("test");
  assert.equal(items.length, 5);
  assert.equal(items[0].id, 10);
  assert.equal(items[4].id, 20);

  const inserted = items.slice(1, 4);
  assert.equal(firstInserted.uuid, inserted[0].uuid);
  const uuids = new Set(inserted.map((r) => r.uuid));
  assert.equal(uuids.size, 3, "each inserted row must receive a distinct UUID");
  for (const row of inserted) {
    assert.ok(row.uuid && row.uuid.length >= 8);
    assert.equal(row.category, "2");
    assert.equal(row.category_name, "異常系");
    assert.equal(row.draft, true);
  }
});

test("collab.moveRows moves single and multi-row blocks before/after target while preserving uuid and skipping no-ops", () => {
  const { ctrl, doc } = loadCollabController();
  seedRows(ctrl, doc, "test", [
    { id: 1, uuid: "uuid-1", title: "Row 1" },
    { id: 2, uuid: "uuid-2", title: "Row 2" },
    { id: 3, uuid: "uuid-3", title: "Row 3" },
    { id: 4, uuid: "uuid-4", title: "Row 4" },
    { id: 5, uuid: "uuid-5", title: "Row 5" },
  ]);

  let txCount = 0;
  doc.on("afterTransaction", () => { txCount += 1; });

  // Self-drop is a no-op without mutating Y.Doc
  assert.equal(ctrl.moveRows("test", [2, 3], { targetId: 2, place: "before" }), 0);
  assert.equal(txCount, 0);

  // Unchanged order (moving row 2 "after" row 1 or "before" row 3) is a no-op
  assert.equal(ctrl.moveRows("test", [2], { targetId: 1, place: "after" }), 0);
  assert.equal(ctrl.moveRows("test", [2], { targetId: 3, place: "before" }), 0);
  assert.equal(txCount, 0);

  // Single-row downward move ("after" row 4): [1, 3, 4, 2, 5]
  assert.equal(ctrl.moveRows("test", [2], { targetId: 4, place: "after" }), 1);
  assert.equal(txCount, 1);
  assert.deepEqual(Array.from(ctrl.getItems("test"), (r) => r.id), [1, 3, 4, 2, 5]);
  assert.deepEqual(
    Array.from(ctrl.getItems("test"), (r) => r.uuid),
    ["uuid-1", "uuid-3", "uuid-4", "uuid-2", "uuid-5"],
  );

  // Multi-row upward move (move [2, 5] "before" row 3): [1, 2, 5, 3, 4]
  assert.equal(ctrl.moveRows("test", [2, 5], { targetId: 3, place: "before" }), 2);
  assert.equal(txCount, 2);
  assert.deepEqual(Array.from(ctrl.getItems("test"), (r) => r.id), [1, 2, 5, 3, 4]);
  assert.deepEqual(
    Array.from(ctrl.getItems("test"), (r) => r.uuid),
    ["uuid-1", "uuid-2", "uuid-5", "uuid-3", "uuid-4"],
  );

  // Multi-row downward move (move [1, 2] "after" row 4): [5, 3, 4, 1, 2]
  assert.equal(ctrl.moveRows("test", [1, 2], { target_id: 4, place: "after" }), 2);
  assert.equal(txCount, 3);
  assert.deepEqual(Array.from(ctrl.getItems("test"), (r) => r.id), [5, 3, 4, 1, 2]);
  assert.deepEqual(
    Array.from(ctrl.getItems("test"), (r) => r.uuid),
    ["uuid-5", "uuid-3", "uuid-4", "uuid-1", "uuid-2"],
  );
});

test("LMApi.moveItems sends target_id/place for object arg and direction for string arg", async () => {
  const { LMApi, calls } = await loadApi();

  await LMApi.moveItems(42, [11, 12], { target_id: 99, place: "after" });
  assert.deepEqual(calls[0], {
    url: "/api/v1/projects/42/items/move",
    method: "POST",
    headers: { "X-CSRF-Token": "csrf-tok", "Content-Type": "application/json" },
    body: { ids: [11, 12], target_id: 99, place: "after" },
  });

  await LMApi.moveItems(42, [11], { targetId: 88 });
  assert.deepEqual(calls[1].body, { ids: [11], target_id: 88, place: "before" });

  await LMApi.moveItems(42, [11, 12], "up");
  assert.deepEqual(calls[2].body, { ids: [11, 12], direction: "up" });
});

test("grid.js attachUniverDark disconnects MutationObserver when adapter.dispose runs", () => {
  const source = readRepoFile("app/static/js/lanmatrix/grid.js");
  let disconnected = 0;
  let origDisposed = 0;
  class FakeMutationObserver {
    observe() {}
    disconnect() { disconnected += 1; }
  }
  const document = {
    documentElement: { getAttribute: () => "dark" },
    addEventListener() {},
  };
  const window = { document, MutationObserver: FakeMutationObserver };
  window.window = window;
  const context = vm.createContext({
    window,
    document,
    MutationObserver: FakeMutationObserver,
    WeakSet,
    setTimeout: () => 1,
    clearTimeout() {},
    console,
  });
  vm.runInContext(source, context);

  const toggled = [];
  const adapter = {
    univerAPI: { toggleDarkMode: (dark) => toggled.push(dark) },
    dispose() { origDisposed += 1; },
  };
  window.LMUniverDark(adapter);
  assert.deepEqual(toggled, [true]);
  assert.ok(adapter._darkObserver);

  adapter.dispose();
  assert.equal(disconnected, 1);
  assert.equal(origDisposed, 1);
  assert.equal(adapter._darkObserver, null);
});
