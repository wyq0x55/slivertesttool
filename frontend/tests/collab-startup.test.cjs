const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

for (const mode of ["missing-bundle", "rejected", "exception"]) {
  test(`collaboration startup ${mode} exposes ordinary editing mode`, async () => {
    const source = fs.readFileSync(path.resolve(__dirname,
      "../../app/static/js/lanmatrix/editor.js"), "utf8");
    const start = source.indexOf("  async function tryStartCollab()");
    const end = source.indexOf("\n  // NOTE:", start);
    let status = "";
    const window = mode === "missing-bundle" ? {} : {
      LMCollab: {},
      LMCollabController: {
        create() {
          if (mode === "exception") throw new Error("unavailable");
          return { async start() { return false; } };
        },
      },
    };
    const context = vm.createContext({
      collabAvailable: true, window, pid: 1, LMApi: {}, SHEET_KEYS: [], ROW_ARRAY_PREFIX: "rows",
      setCollabStatus(value) { status = value; },
    });
    vm.runInContext(source.slice(start, end), context);
    await context.tryStartCollab();
    assert.match(status, /普通编辑/);
  });
}
