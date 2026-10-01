const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const root = path.resolve(__dirname, "../..");
const read = (file) => fs.readFileSync(path.join(root, file), "utf8");
const settle = () => new Promise((resolve) => setImmediate(resolve));

function deferred() {
  let resolve;
  let reject;
  const promise = new Promise((accept, fail) => { resolve = accept; reject = fail; });
  return { promise, resolve, reject };
}

function elements() {
  const nodes = new Map();
  const get = (id) => {
    if (!nodes.has(id)) {
      nodes.set(id, {
        value: "", textContent: "", innerHTML: "", hidden: true,
        disabled: false, open: false, selectedOptions: [], listeners: {},
        addEventListener(type, callback) { this.listeners[type] = callback; },
        querySelectorAll() { return []; },
        showModal() { this.open = true; },
        close() {
          this.open = false;
          if (this.listeners.close) this.listeners.close();
        },
      });
    }
    return nodes.get(id);
  };
  return get;
}

function section(source, start, end) {
  const first = source.indexOf(start);
  const last = source.indexOf(end, first);
  assert.ok(first >= 0 && last > first, "production section must exist");
  return source.slice(first, last);
}

function importHarness() {
  const get = elements();
  get("lm-import-format").value = "test_matrix";
  get("lm-import-mode").value = "upsert";
  get("lm-import-dialog").open = true;
  const previews = [];
  const commits = [];
  const toasts = [];
  const preview = (projectId, file, mode) => {
    const response = deferred();
    previews.push({ ...response, file, mode });
    return response.promise;
  };
  const context = vm.createContext({
    document: { getElementById: get, querySelectorAll: () => [] },
    pid: 1, currentSheet: "test", esc: (value) => String(value),
    isUniver: () => false, collabActive: () => false,
    applySheetContext: async () => {}, loadFields: async () => {},
    loadItems: async () => {}, toast: (message, ok) => toasts.push({ message, ok }),
    LMApi: {
      createImport: preview, importTestMatrix: preview, importLibFunc: preview,
      importConst: preview, importIo: preview,
      commitImport(jobId) {
        const response = deferred();
        commits.push({ ...response, jobId });
        return response.promise;
      },
    },
  });
  vm.runInContext(section(read("app/static/js/lanmatrix/editor.js"),
    "  // --- Import dialog ---", "  const extractIoBtn"), context);
  const upload = (name = "cases.xlsx") => get("lm-import-dialog-file").listeners.change({
    target: { files: [{ name }] },
  });
  const commit = () => get("lm-import-commit").listeners.click({ preventDefault() {} });
  return { get, previews, commits, toasts, upload, commit };
}

function job(id, invalid = 0) {
  return { job: { id, status: "previewed", parameters: { format: "test_matrix" },
    preview: { total: 1, insert: 1, update: 0, invalid } } };
}

async function readyImport(harness) {
  const uploading = harness.upload();
  harness.previews[0].resolve(job(1));
  await uploading;
}

test("import locks format, mode and file until commit settles", async () => {
  const harness = importHarness();
  await readyImport(harness);
  const committing = harness.commit();
  for (const id of ["lm-import-format", "lm-import-mode", "lm-import-dialog-file"]) {
    assert.equal(harness.get(id).disabled, true, id);
  }
  harness.commits[0].resolve({ inserted: 1, updated: 0 });
  await committing;
  for (const id of ["lm-import-format", "lm-import-mode", "lm-import-dialog-file"]) {
    assert.equal(harness.get(id).disabled, false, id);
  }
  assert.equal(harness.toasts[0].ok, true);
});

test("import ignores change events and duplicate commit while submitting", async () => {
  const harness = importHarness();
  await readyImport(harness);
  const committing = harness.commit();
  harness.get("lm-import-mode").listeners.change();
  harness.get("lm-import-format").listeners.change();
  const replacing = harness.upload("replacement.xlsx");
  const duplicate = harness.commit();
  await settle();
  assert.equal(harness.previews.length, 1);
  assert.equal(harness.commits.length, 1);
  harness.commits[0].resolve({ inserted: 1, updated: 0 });
  await committing;
  await replacing;
  await duplicate;
});

test("closed import dialog ignores old commit success and can preview a new job", async () => {
  const harness = importHarness();
  await readyImport(harness);
  const committing = harness.commit();
  harness.get("lm-import-dialog").close();
  harness.commits[0].resolve({ inserted: 1, updated: 0 });
  await committing;
  assert.equal(harness.toasts.length, 0);
  harness.get("lm-import").listeners.click();
  const uploading = harness.upload("new.xlsx");
  harness.previews[1].resolve(job(2));
  await uploading;
  const newCommit = harness.commit();
  assert.equal(harness.commits[1].jobId, 2);
  harness.commits[1].resolve({ inserted: 1, updated: 0 });
  await newCommit;
});

test("closed import dialog ignores old commit failure", async () => {
  const harness = importHarness();
  await readyImport(harness);
  const committing = harness.commit();
  harness.get("lm-import-dialog").close();
  harness.commits[0].reject(new Error("old failure"));
  await committing;
  assert.equal(harness.get("lm-import-error").hidden, true);
  assert.equal(harness.get("lm-import-format").disabled, false);
});

test("failed commit restores controls and keeps the preview for retry", async () => {
  const harness = importHarness();
  await readyImport(harness);
  const committing = harness.commit();
  harness.commits[0].reject(new Error("permission denied"));
  await committing;
  assert.equal(harness.get("lm-import-error").textContent, "permission denied");
  assert.equal(harness.get("lm-import-error").hidden, false);
  assert.equal(harness.get("lm-import-mode").disabled, false);
  assert.equal(harness.get("lm-import-commit").disabled, false);
  assert.equal(harness.toasts.length, 0);
});

for (const code of ["IMPORT_PREVIEW_STALE", "IMPORT_PREVIEW_EXPIRED"]) {
  test(`${code} regenerates preview and requires a second confirmation`, async () => {
    const harness = importHarness();
    await readyImport(harness);
    const committing = harness.commit();
    harness.commits[0].reject(Object.assign(new Error(code), { code }));
    await settle();
    assert.equal(harness.previews.length, 2);
    assert.equal(harness.get("lm-import-commit").disabled, true);
    harness.previews[1].resolve(job(2));
    await committing;
    assert.equal(harness.get("lm-import-commit").disabled, false);
    assert.equal(harness.commits.length, 1);
    assert.equal(harness.toasts.length, 0);
  });
}

test("late preview does not overwrite a newer preview", async () => {
  const harness = importHarness();
  const older = harness.upload("old.xlsx");
  const newer = harness.upload("new.xlsx");
  harness.previews[1].resolve(job(2));
  await newer;
  harness.previews[0].resolve(job(1, 1));
  await older;
  assert.match(harness.get("lm-import-summary").innerHTML, /new.xlsx/);
  assert.equal(harness.get("lm-import-commit").disabled, false);
});

function compareHarness() {
  const get = elements();
  get("lm-vc-left").value = "model@v1";
  get("lm-vc-right").value = "model@v2";
  const requests = [];
  const context = vm.createContext({
    URLSearchParams, projectId: 1, $: get, esc: (value) => String(value),
    fetch(url) {
      const response = deferred();
      requests.push({ ...response, url });
      return response.promise;
    },
  });
  const source = section(read("app/static/js/lanmatrix/dashboard.js"),
    "const COMPARE_LABELS", '  document.addEventListener("DOMContentLoaded"');
  vm.runInContext(source + "\nglobalThis.compare = loadVersionCompare; bindVersionCompare();", context);
  return { get, requests, compare: context.compare };
}

function response(label, { status = 200, total = 1 } = {}) {
  return { status, ok: status >= 200 && status < 300,
    json: async () => ({ success: true, data: { total, page_size: 50,
      items: [{ test_id: label, left_outcome: "pass", right_outcome: "fail",
        change: "pass_to_fail" }] } }) };
}

async function readyCompare(harness) {
  const comparing = harness.compare(1);
  harness.requests[0].resolve(response("baseline", { total: 100 }));
  await comparing;
}

test("compare 401 clears prior results and reports session expiry", async () => {
  const harness = compareHarness();
  await readyCompare(harness);
  const comparing = harness.compare(1);
  harness.requests[1].resolve({ status: 401, ok: false, json: async () => ({
    success: false, error: { message: "expired" },
  }) });
  await comparing;
  assert.equal(harness.get("lm-vc-err").hidden, false);
  assert.ok(harness.get("lm-vc-err").textContent.length > 0);
  assert.equal(harness.get("lm-vc-rows").innerHTML, "");
  assert.equal(harness.get("lm-vc-summary").textContent, "");
  assert.equal(harness.get("lm-vc-next").hidden, true);
});

test("late compare response cannot replace the newer model pair", async () => {
  const harness = compareHarness();
  const older = harness.compare(1);
  harness.get("lm-vc-left").value = "model@v3";
  harness.get("lm-vc-right").value = "model@v4";
  const newer = harness.compare(1);
  harness.requests[1].resolve(response("new-pair"));
  await newer;
  harness.requests[0].resolve(response("old-pair"));
  await older;
  assert.match(harness.get("lm-vc-rows").innerHTML, /new-pair/);
  assert.doesNotMatch(harness.get("lm-vc-rows").innerHTML, /old-pair/);
});

test("late compare failure cannot fail or unlock the current request", async () => {
  const harness = compareHarness();
  const older = harness.compare(1);
  const newer = harness.compare(2);
  harness.requests[0].reject(new Error("old network failure"));
  await older;
  assert.equal(harness.get("lm-vc-err").hidden, true);
  assert.equal(harness.get("lm-vc-run").disabled, true);
  harness.requests[1].resolve(response("page-two"));
  await newer;
  assert.equal(harness.get("lm-vc-run").disabled, false);
});

test("editing the model pair invalidates the pending response and rendered results", async () => {
  const harness = compareHarness();
  await readyCompare(harness);
  const comparing = harness.compare(1);
  harness.get("lm-vc-left").value = "model@v3";
  if (harness.get("lm-vc-left").listeners.input) {
    harness.get("lm-vc-left").listeners.input();
  }
  harness.requests[1].resolve(response("old-pair"));
  await comparing;
  assert.equal(harness.get("lm-vc-rows").innerHTML, "");
  assert.equal(harness.get("lm-vc-next").hidden, true);
  assert.equal(harness.get("lm-vc-run").disabled, false);
});

test("non-2xx compare response is rejected even with a success envelope", async () => {
  const harness = compareHarness();
  await readyCompare(harness);
  const comparing = harness.compare(1);
  harness.requests[1].resolve(response("false-success", { status: 500 }));
  await comparing;
  assert.equal(harness.get("lm-vc-err").hidden, false);
  assert.equal(harness.get("lm-vc-rows").innerHTML, "");
});

test("dashboard exposes the project-wide history CSV download", () => {
  const html = read("app/templates/lanmatrix/dashboard.html");
  const link = html.match(/<a\b[^>]*id="lm-history-csv"[^>]*>/);
  assert.ok(link, "history CSV action must be reachable from the dashboard");
  assert.match(link[0], /lanmatrix_projects\.test_run_history_csv/);
  assert.match(link[0], /project_id=project_id/);
});
