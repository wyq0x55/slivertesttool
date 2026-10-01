const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");
const typescript = require("typescript");

function selectionHarness() {
  const filename = path.resolve(__dirname, "../src/adapter.ts");
  const compiled = typescript.transpileModule(fs.readFileSync(filename, "utf8"), {
    compilerOptions: { module: typescript.ModuleKind.CommonJS, target: typescript.ScriptTarget.ES2020 },
  });
  const exports = {};
  vm.runInNewContext(compiled.outputText, { exports, require: () => ({}), console, setTimeout, clearTimeout }, { filename });
  const Adapter = exports.UniverGridAdapter;
  assert.equal(typeof Adapter, "function");
  const adapter = Object.create(Adapter.prototype);
  let consume;
  const changes = [];
  adapter.univerAPI = {
    Event: { SelectionChanged: "SelectionChanged" }, addEvent() {},
    onCommandExecuted(callback) { consume = callback; },
  };
  adapter.fWorkbook = { getId: () => "workbook-test" };
  adapter.active = { key: "test", items: [{ id: 12 }, { id: 13 }], selected: new Set(),
    fSheet: { getSheetId: () => "sheet-test" } };
  adapter.opts = { onSelectionChange: ids => changes.push(Array.from(ids)) };
  adapter._maybeHandleStructural = () => false;
  adapter._maybeOpenSteps = () => {};
  adapter._bindEvents();
  return { adapter, changes, consume };
}

function command(overrides = {}) {
  return { id: "sheet.operation.set-selections", params: {
    unitId: "workbook-test", subUnitId: "sheet-test", type: 2,
    selections: [{ range: { startRow: 1, endRow: 2, startColumn: 0, endColumn: 1 } }],
    ...overrides,
  } };
}

test("native Univer selection commands reach persisted-row selection without facade emission", () => {
  const harness = selectionHarness();
  harness.consume(command());
  assert.deepEqual(Array.from(harness.adapter.getSelectedIds()), [12, 13]);
  assert.deepEqual(harness.changes.at(-1), [12, 13]);
});

test("native Univer empty selection clears selected row IDs", () => {
  const harness = selectionHarness();
  harness.adapter.active.selected.add(12);
  harness.consume(command({ selections: [] }));
  assert.deepEqual(Array.from(harness.adapter.getSelectedIds()), []);
});

for (const target of [{ unitId: "other-workbook" }, { subUnitId: "other-sheet" }]) {
  test(`selection commands cannot borrow another sheet's rows ${JSON.stringify(target)}`, () => {
    const harness = selectionHarness();
    harness.adapter.active.selected.add(12);
    harness.consume(command(target));
    assert.deepEqual(Array.from(harness.adapter.getSelectedIds()), [12]);
    assert.deepEqual(harness.changes, []);
  });
}
