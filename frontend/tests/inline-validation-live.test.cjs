/* Inline validation messages: polite live regions (WCAG 4.1.3 Status Messages).
 *
 * Two validation hints were only ever `textContent` on a plain
 * `<span class="muted">`, and their branches return without calling `toast()`,
 * so a screen-reader user never heard the failure:
 *  - project_tasks.html #lm-submit-status: "请至少勾选一个 test id。",
 *    "无可用模型…" and the upload-failure message;
 *  - ai_drafts.html #lm-ai-signals-status: "每行需要至少 2 列…".
 *
 * Guide `accessible-error-announcement`: "Announce dynamic errors via live
 * regions." Guide `accessibility`, Live Region Urgency Table: standard
 * messages are polite ("Announces at next graceful break"), assertive is for
 * critical messages ("Interrupts immediately, clears speech queue"). So these
 * two carry `role="status"` — the implicit polite region — and deliberately
 * not `role="alert"`, which would interrupt the user mid-sentence.
 *
 * These are static checks: no browser or screen reader is involved, so they
 * lock structure and write behaviour, not real announcement timing.
 */
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const root = path.resolve(__dirname, "../..");
// Normalise CRLF: the checkout uses CRLF, the markers below assume LF.
const read = (file) =>
  fs.readFileSync(path.join(root, file), "utf8").replace(/\r\n/g, "\n");

const projectTasksHtml = read("app/templates/lanmatrix/project_tasks.html");
const aiDraftsHtml = read("app/templates/lanmatrix/ai_drafts.html");
const projectTasksJs = read("app/static/js/lanmatrix/project_tasks.js");
const aiDraftsJs = read("app/static/js/lanmatrix/ai_drafts.js");

const escapeRe = (s) => s.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");

// The full opening tag of the single element carrying this id.
function tagFor(html, id) {
  const found = [
    ...html.matchAll(new RegExp(`<[a-z]+\\b[^>]*\\bid="${escapeRe(id)}"[^>]*>`, "g")),
  ];
  assert.equal(found.length, 1, `expected exactly one element with id ${id}`);
  return found[0][0];
}

// Source of the single production block opening with `opening` (for example
// "async function submit() {"), by brace matching that skips braces inside
// strings, template literals and comments.
function extractBlock(source, opening) {
  const start = source.indexOf(opening);
  assert.ok(start >= 0, `production code "${opening}" must exist`);
  let depth = 0;
  let quote = null;
  for (let i = source.indexOf("{", start); i < source.length; i += 1) {
    const c = source[i];
    const next = source[i + 1];
    if (quote) {
      if (c === "\\") i += 1;
      else if (c === quote) quote = null;
      continue;
    }
    if (c === '"' || c === "'" || c === "`") { quote = c; continue; }
    if (c === "/" && next === "/") { i = source.indexOf("\n", i); continue; }
    if (c === "/" && next === "*") { i = source.indexOf("*/", i) + 1; continue; }
    if (c === "{") depth += 1;
    else if (c === "}") {
      depth -= 1;
      if (depth === 0) return source.slice(start, i + 1);
    }
  }
  return assert.fail(`unterminated block opening with "${opening}"`);
}

/* ------------------------------------------------------------- templates */

for (const [name, html, id] of [
  ["project_tasks.html", projectTasksHtml, "lm-submit-status"],
  ["ai_drafts.html", aiDraftsHtml, "lm-ai-signals-status"],
]) {
  test(`${name} #${id} is a polite status region`, () => {
    const tag = tagFor(html, id);
    assert.match(tag, /\brole="status"/, "role=status is the implicit polite live region");
    assert.doesNotMatch(tag, /role="alert"/, "assertive would interrupt the user");
    // role=status already implies aria-live="polite"; the element stays a
    // plain span, so nothing else about it changed.
    assert.doesNotMatch(tag, /\baria-live\b/, "aria-live is redundant with role=status");
    assert.match(tag, /\bclass="muted"/, "the visual styling is unchanged");
    assert.doesNotMatch(tag, /\bhidden\b|aria-hidden/, "a live region must be in the tree to announce");
  });
}

/* ------------------------------------------------- project_tasks.js submit */

// Minimal `$` shim: the two early returns only need a node to write into.
function submitHarness(overrides) {
  const status = { textContent: "" };
  const model = { value: "" };
  const button = { disabled: false };
  const nodes = { "lm-submit-status": status, "lm-model": model, "lm-submit": button };
  const toastCalls = [];
  const deps = {
    $: (id) => {
      assert.ok(nodes[id], `submit() reached unexpected element #${id}`);
      return nodes[id];
    },
    selectedIds: () => [],
    filesToUpload: () => [],
    entries: [],
    libEntries: [],
    stdlibEntries: [],
    LMApi: { uploadProjectTree: async () => ({ created: [], duplicates: [], errors: [] }) },
    pid: 7,
    renderResult: () => {},
    load: () => {},
    toast: (...args) => toastCalls.push(args),
    FormData: class { append() {} },
    ...overrides,
  };
  const submit = vm.runInNewContext(
    `(function (deps) {
       const { $, selectedIds, filesToUpload, entries, libEntries, stdlibEntries,
               LMApi, pid, renderResult, load, toast, FormData } = deps;
       ${extractBlock(projectTasksJs, "async function submit() {")}
       return submit;
     })`,
    {},
  )(deps);
  return { submit, status, model, button, toastCalls };
}

test("the submit failure paths write into the live region and stay silent", async () => {
  // The whole function must not toast: every message it shows is the span's
  // text, which is exactly why the span had to become a live region.
  const body = extractBlock(projectTasksJs, "async function submit() {");
  assert.doesNotMatch(body, /\btoast\s*\(/, "submit() must not rely on toast");

  const noSelection = submitHarness({ selectedIds: () => [] });
  await noSelection.submit();
  assert.equal(noSelection.status.textContent, "请至少勾选一个 test id。");
  assert.equal(noSelection.toastCalls.length, 0);

  const noModel = submitHarness({ selectedIds: () => ["t1"] });
  await noModel.submit();
  assert.match(noModel.status.textContent, /无可用模型/);
  assert.equal(noModel.toastCalls.length, 0);

  const uploadFails = submitHarness({
    selectedIds: () => ["t1"],
    filesToUpload: () => [],
    LMApi: {
      uploadProjectTree: async () => { throw new Error("网络断了"); },
    },
  });
  uploadFails.model.value = "demo.sil";
  await uploadFails.submit();
  assert.equal(uploadFails.status.textContent, "网络断了");
  assert.equal(uploadFails.toastCalls.length, 0);
  assert.equal(uploadFails.button.disabled, false, "finally still re-enables submit");
});

/* ----------------------------------------------- ai_drafts.js bindSignals */

function signalsHarness({ canEdit = true, putSignals } = {}) {
  const text = { value: "" };
  const status = { textContent: "" };
  const save = { listeners: {}, addEventListener(type, callback) { this.listeners[type] = callback; } };
  const box = { id: "lm-ai-signals" };
  const byId = {
    "lm-ai-signals": box,
    "lm-ai-signals-text": text,
    "lm-ai-signals-save": save,
    "lm-ai-signals-status": status,
  };
  const toastCalls = [];
  const deps = {
    document: { getElementById: (id) => byId[id] ?? null },
    LMApi: {
      aiGetSignals: async () => [],
      aiPutSignals: putSignals || (async () => ({ count: 1 })),
    },
    canEdit,
    pid: 3,
    toast: (...args) => toastCalls.push(args),
  };
  const bindSignals = vm.runInNewContext(
    `(function (deps) {
       const { document, LMApi, canEdit, pid, toast } = deps;
       ${extractBlock(aiDraftsJs, "function bindSignals() {")}
       return bindSignals;
     })`,
    {},
  )(deps);
  bindSignals();
  return { text, status, save, toastCalls };
}

test("the signals parse error is announced through the live region only", async () => {
  const harness = signalsHarness();
  harness.text.value = "单列没有逗号";
  await harness.save.listeners.click();
  assert.equal(
    harness.status.textContent,
    "每行需要至少 2 列（表示名, 路径）：单列没有逗号",
  );
  assert.equal(harness.toastCalls.length, 0, "this branch deliberately has no toast");
});

test("the sibling save branches still toast, so only the parse error was silent", async () => {
  const saved = signalsHarness();
  saved.text.value = "车速, engine.veh_speed";
  await saved.save.listeners.click();
  assert.equal(saved.status.textContent, "已保存 1 条");
  assert.deepEqual(saved.toastCalls, [["信号字典已保存", true]]);

  const failing = signalsHarness({
    putSignals: async () => { throw new Error("保存失败"); },
  });
  failing.text.value = "车速, engine.veh_speed";
  await failing.save.listeners.click();
  assert.deepEqual(failing.toastCalls, [["保存失败", false]]);
});