// Guards the import-preview error table built by previewImport() in
// app/static/js/lanmatrix/editor.js: every column header (行 / 列 / 问题) must
// carry scope="col" in both rendering paths — the DOM construction used by
// modern browsers and the innerHTML fallback used when createElement is
// unavailable. Without it a screen-reader user cannot tell which column a
// cell describes.
//
// The real previewImport() is sliced out of the controller IIFE and executed
// in a node:vm sandbox with a minimal DOM shim, so the assertions cover what
// the browser would receive rather than only the source text.
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const root = path.resolve(__dirname, "../..");
const editorPath = path.join(root, "app/static/js/lanmatrix/editor.js");
const source = fs.readFileSync(editorPath, "utf8");

// Slice one top-level function out of the controller IIFE. Functions are
// indented by two spaces and closed by a line holding exactly "  }".
function extractFunction(name) {
  const marker = `function ${name}(`;
  const at = source.indexOf(marker);
  assert.ok(at >= 0, `editor.js must still define ${name}()`);
  const start = source.slice(at - 6, at) === "async " ? at - 6 : at;
  const end = source.indexOf("\n  }", at);
  assert.ok(end > at, `${name}() must end on its own top-level line`);
  return source.slice(start, end + 4);
}

// Same escaping contract as editor.js's esc() helper.
const realEsc = (s) => String(s == null ? "" : s).replace(/[&<>"]/g, (c) =>
  ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));

function element(tag) {
  return {
    tagName: String(tag).toUpperCase(),
    className: "",
    textContent: "",
    scope: undefined,
    children: [],
    appendChild(child) { this.children.push(child); return child; },
    replaceChildren(...children) { this.children = children; },
  };
}

// Build a sandbox around the real previewImport(). `createElement: true`
// exercises the DOM path; false removes document.createElement so the
// innerHTML fallback runs. `response` is the job payload or a thunk that
// produces/rejects it, letting tests cover the error slot too.
function makeSandbox({ createElement, response, mode = "upsert", format = "generic" }) {
  const created = [];
  const summary = element("div");
  const calls = [];
  const commitButton = { disabled: false };
  const errorEl = { textContent: "" };
  const specCall = (projectId, file, callMode) => {
    calls.push({ projectId, file, mode: callMode });
    return Promise.resolve(typeof response === "function" ? response() : response);
  };
  const documentShim = createElement ? {
    createElement(tag) { const node = element(tag); created.push(node); return node; },
    createTextNode(text) { return { tagName: "#TEXT", textContent: String(text) }; },
  } : {};
  const context = vm.createContext({
    document: documentShim,
    importFormatSel: { value: format },
    importModeSel: { value: mode },
    importCommitButton: commitButton,
    importError: errorEl,
    importSummary: summary,
    IMPORT_FORMATS: { [format]: { label: "通用模板", call: specCall } },
    IMPORT_SHEETS: { [format]: "test" },
    pendingFile: null,
    pendingJob: null,
    previewRequest: 0,
    importCommitActive: false,
    pid: 7,
    esc: realEsc,
    updateImportCommitButton() {},
  });
  vm.runInContext(extractFunction("previewImport"), context, { filename: editorPath });
  return { context, created, summary, commitButton, errorEl, calls };
}

function previewJob(errors, extra = {}) {
  return {
    job: {
      id: 11,
      status: "previewed",
      preview: {
        total: 3, insert: 2, update: 1, invalid: 0, errors, ...extra,
      },
    },
  };
}

async function runPreview(sandbox, file = { name: "cases.xlsx" }, notice = "") {
  await sandbox.context.previewImport(file, notice);
  return sandbox;
}

function scopedHeaderTexts(table) {
  const thead = table.children[0];
  assert.equal(thead.tagName, "THEAD");
  const headRow = thead.children[0];
  assert.equal(headRow.tagName, "TR");
  return headRow.children.map((th) => ({
    text: th.textContent,
    scope: th.scope,
    tag: th.tagName,
  }));
}

// ------------------------------------------------------- primary DOM path

test("DOM path: all three preview headers carry explicit column scope", async () => {
  const sandbox = makeSandbox({
    createElement: true,
    response: previewJob([
      { row: 2, column: "A", message: "重复值" },
      { row: 3, field: "B" },
    ]),
  });
  const file = { name: "cases.xlsx" };
  await runPreview(sandbox, file);

  const table = sandbox.created.find((node) => node.tagName === "TABLE");
  assert.ok(table, "the error preview must build a table");
  assert.equal(table.className, "lm-table lm-preview");

  assert.deepEqual(scopedHeaderTexts(table), [
    { text: "行", scope: "col", tag: "TH" },
    { text: "列", scope: "col", tag: "TH" },
    { text: "问题", scope: "col", tag: "TH" },
  ]);

  // Cells keep their order and fallbacks.
  const tbody = table.children[1];
  assert.equal(tbody.tagName, "TBODY");
  assert.equal(tbody.children.length, 2);
  assert.deepEqual(tbody.children[0].children.map((td) => td.textContent),
    ["2", "A", "重复值"]);
  assert.deepEqual(tbody.children[1].children.map((td) => td.textContent),
    ["3", "B", "未知错误"]);

  // Summary counts and existing import state survive the change.
  const summaryP = sandbox.summary.children[0];
  assert.equal(summaryP.textContent,
    "《cases.xlsx》按通用模板预览：共 3 行，新增 2，更新 1，错误 0。");
  assert.equal(sandbox.calls.length, 1);
  assert.deepEqual({ projectId: sandbox.calls[0].projectId, mode: sandbox.calls[0].mode },
    { projectId: 7, mode: "upsert" });
  assert.equal(sandbox.calls[0].file, file);
  assert.equal(sandbox.context.pendingJob.id, 11);
  assert.equal(sandbox.context.pendingFile, file);
  assert.equal(sandbox.commitButton.disabled, true);
});

test("DOM path: replace-all warning and the 20-row cap are preserved", async () => {
  const errors = Array.from({ length: 25 }, (_, index) =>
    ({ row: index + 1, column: "A", message: "bad" }));
  const sandbox = makeSandbox({
    createElement: true,
    response: previewJob(errors, { invalid: 25 }),
    mode: "replace_all",
  });
  await runPreview(sandbox);

  const [summaryP, warnP, table, moreP] = sandbox.summary.children;
  assert.equal(summaryP.tagName, "P");
  assert.equal(warnP.className, "lm-error");
  assert.equal(warnP.children[0].textContent, "整表替换：");
  assert.equal(warnP.children[1].textContent,
    "确认后将替换该格式对应工作表的现有数据；被替换行将移入回收站。");

  assert.equal(table.tagName, "TABLE");
  assert.deepEqual(scopedHeaderTexts(table).map((th) => th.scope),
    ["col", "col", "col"]);
  const tbody = table.children[1];
  assert.equal(tbody.children.length, 20);

  assert.equal(moreP.tagName, "P");
  assert.equal(moreP.className, "lm-muted");
  assert.equal(moreP.textContent, "仅显示前 20 项错误。");
});

test("DOM path: a failed preview reports through the error slot", async () => {
  const sandbox = makeSandbox({
    createElement: true,
    response: () => Promise.reject(new Error("导入失败")),
  });
  await runPreview(sandbox, { name: "x.xlsx" }, "先前的提示");

  assert.equal(sandbox.context.pendingJob, null);
  assert.equal(sandbox.errorEl.textContent, "导入失败");
  assert.deepEqual(sandbox.summary.children, []);
  assert.equal(sandbox.created.find((node) => node.tagName === "TABLE"), undefined);
});

// ------------------------------------------------------ innerHTML fallback

test("fallback path: header markup carries scope=col and preserves escaping", async () => {
  const sandbox = makeSandbox({
    createElement: false,
    response: previewJob([
      { row: 2, column: "<A>", message: "非法值 <x>" },
    ]),
    mode: "replace_all",
  });
  await runPreview(sandbox, { name: "a&b.xlsx" });

  const html = sandbox.summary.innerHTML;
  assert.match(html,
    /<table class="lm-table lm-preview"><thead><tr><th scope="col">行<\/th><th scope="col">列<\/th><th scope="col">问题<\/th><\/tr><\/thead><tbody>/);
  assert.match(html, /<td>&lt;A&gt;<\/td>/);
  assert.match(html, /<td>非法值 &lt;x&gt;<\/td>/);
  assert.match(html, /《a&amp;b\.xlsx》按通用模板预览：共 3 行，新增 2，更新 1，错误 0。/);
  assert.match(html,
    /<p class="lm-error"><strong>整表替换：<\/strong>确认后将替换该格式对应工作表的现有数据；被替换行将移入回收站。<\/p>/);
  assert.equal(sandbox.context.pendingJob.id, 11);
});

test("fallback path: no error table without errors and the 20-row cap holds", async () => {
  const empty = makeSandbox({ createElement: false, response: previewJob([]) });
  await runPreview(empty);
  assert.doesNotMatch(empty.summary.innerHTML, /<table/);
  assert.doesNotMatch(empty.summary.innerHTML, /仅显示前/);

  const errors = Array.from({ length: 21 }, (_, index) =>
    ({ row: index, column: "C", message: "m" }));
  const capped = makeSandbox({
    createElement: false,
    response: previewJob(errors, { invalid: 21 }),
  });
  await runPreview(capped);
  const html = capped.summary.innerHTML;
  assert.equal((html.match(/<tr><td>/g) || []).length, 20);
  assert.match(html, /<p class="lm-muted">仅显示前 20 项错误。<\/p>/);
});