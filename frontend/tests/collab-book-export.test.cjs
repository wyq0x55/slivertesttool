const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const source = (name) => fs.readFileSync(path.resolve(
  __dirname, `../../app/static/js/lanmatrix/${name}.js`
), "utf8");

test("book export retries with one live receipt and releases it when the request ends", async () => {
  const editor = source("editor");
  const start = editor.indexOf("  async function downloadBookExport(");
  const end = editor.indexOf("  const exportButton", start);
  assert.ok(start >= 0 && end > start);

  const fences = new Map();
  const events = [];
  const requestFences = [];
  let errors = ["COLLAB_BOOK_EXPORT_PENDING"];
  const context = vm.createContext({
    window: {},
    collabActive: () => true,
    collabConn: "connected",
    async waitForEditorCommit(action) { events.push(action); },
    setTimeout(callback) { callback(); },
    document: {
      createElement() {
        return { click() { events.push(this.download); } };
      },
    },
    URL: {
      createObjectURL: () => "blob:workbook",
      revokeObjectURL() {},
    },
    async fetch(_url, { headers }) {
      const fence = headers["X-LM-Export-Fence"];
      assert.equal(fences.get(fence), true);
      requestFences.push(fence);
      const code = errors.shift();
      if (code) return { ok: false, json: async () => ({ error: { code, message: "Fix the row" } }) };
      return {
        ok: true,
        headers: { get: () => "attachment; filename*=UTF-8''%E5%BC%8F.xlsx" },
        blob: async () => ({}),
      };
    },
  });
  vm.runInContext(source("collab"), context);
  const collab = context.window.LMCollabController.create({ pid: 1 });
  collab.doc = { getMap: () => fences };
  collab.provider = { synced: true };
  context.collab = collab;
  vm.runInContext(editor.slice(start, end), context);

  await context.downloadBookExport("/first", "fallback.xlsx");
  assert.deepEqual(events, ["导出", "式.xlsx"]);
  assert.equal(requestFences.length, 2);
  assert.equal(requestFences[0], requestFences[1]);
  assert.equal(fences.size, 0);

  errors = ["COLLAB_BOOK_EXPORT_INVALID"];
  await assert.rejects(context.downloadBookExport("/all", "fallback.zip"), /Fix the row/);
  assert.equal(requestFences.length, 3);
  assert.equal(fences.size, 0);

  collab.provider.synced = false;
  await assert.rejects(context.downloadBookExport("/all", "fallback.zip"), /未同步/);
  assert.equal(requestFences.length, 3);
});
