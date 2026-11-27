const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");
const typescript = require("typescript");

function toHostRealm(value) {
  return value === undefined ? undefined : JSON.parse(JSON.stringify(value));
}

function loadTsModule(relPath, customMocks = {}, customGlobals = {}) {
  const filename = path.resolve(__dirname, relPath);
  const source = fs.readFileSync(filename, "utf8");
  const compiled = typescript.transpileModule(source, {
    compilerOptions: {
      module: typescript.ModuleKind.CommonJS,
      target: typescript.ScriptTarget.ES2020,
    },
  });
  const exports = {};
  const moduleObj = { exports };
  const sandbox = Object.assign(
    {
      exports,
      module: moduleObj,
      require: (id) => {
        if (id in customMocks) return customMocks[id];
        if (id === "react") {
          return {
            createElement(tag, props, ...children) {
              return { tag, props: props || {}, children };
            },
          };
        }
        return {};
      },
      console,
      Promise,
      setTimeout,
      clearTimeout,
    },
    customGlobals
  );
  sandbox.globalThis = sandbox;
  vm.runInNewContext(compiled.outputText, sandbox, { filename });
  return { exports: moduleObj.exports, sandbox };
}

function createMockSheet(initialMaxRows = 1000, initialMaxCols = 64) {
  let maxRows = initialMaxRows;
  let maxCols = initialMaxCols;
  let activeSelectionRow = 1;
  const calls = {
    setRowCount: [],
    setColumnCount: [],
    clearContent: [],
    setBackgroundColor: [],
    setValues: [],
    getRange: [],
  };
  const sheet = {
    calls,
    setSelectionRow(r) {
      activeSelectionRow = r;
    },
    getSheetId: () => "sheet-test",
    getSheetName: () => "矩阵",
    getMaxRows: () => maxRows,
    getRowCount: () => maxRows,
    setRowCount(n) {
      calls.setRowCount.push(n);
      maxRows = n;
    },
    getMaxColumns: () => maxCols,
    getColumnCount: () => maxCols,
    setColumnCount(n) {
      calls.setColumnCount.push(n);
      maxCols = n;
    },
    getSelection: () => ({
      getActiveRange: () => ({
        getRow: () => activeSelectionRow,
      }),
    }),
    getRange(row, col, numRows = 1, numCols = 1) {
      if (row < 0 || row + numRows > maxRows) {
        throw new Error(`Range out of bounds: row=${row} numRows=${numRows} maxRows=${maxRows}`);
      }
      calls.getRange.push({ row, col, numRows, numCols });
      return {
        clearContent() {
          calls.clearContent.push({ row, col, numRows, numCols });
        },
        setBackgroundColor(color) {
          calls.setBackgroundColor.push({ row, col, numRows, numCols, color });
        },
        setValues(matrix) {
          calls.setValues.push({ row, col, numRows, numCols, rowsWritten: matrix.length });
        },
        setValue() {},
        setFontWeight() {},
        setHorizontalAlignment() {},
        setNote() {},
      };
    },
  };
  return sheet;
}

function createAdapterHarness(optsOverrides = {}, itemsInput = null) {
  const { exports } = loadTsModule("../src/adapter.ts", {
    "./collab_overlay": {
      CollabOverlay: class {
        constructor() { this.disposed = false; }
        setSelections() {}
        refresh() {}
        dispose() { this.disposed = true; }
      },
    },
    "./cell_tooltip": {
      CellTooltip: class {
        constructor() { this.disposed = false; }
        init() {}
        dispose() { this.disposed = true; }
      },
    },
  });
  const Adapter = exports.UniverGridAdapter;
  const handlers = {};
  let onCmdExec = null;
  const createdMenus = [];
  const shortcutCalls = { getShortcut: 0, disable: 0, enable: 0 };
  const editableCalls = [];
  const disposedItems = [];
  let lifecycleStage = 2;

  const fSheet = createMockSheet(1000, 64);
  const fWorkbook = {
    getId: () => "wb-main",
    getActiveSheet: () => fSheet,
    getSheetByName: (name) => (name === "矩阵" ? fSheet : null),
    setActiveSheet: () => {},
    setEditable: (v) => editableCalls.push(v),
  };

  const univerAPI = {
    Enum: {
      LifecycleStages: { Starting: 0, Ready: 1, Rendered: 2, Steady: 3 },
      BuiltInUIPart: { FOOTER: "footer", TOOLBAR: "toolbar" },
    },
    Event: {
      LifeCycleChanged: "LifeCycleChanged",
      BeforeCommandExecute: "BeforeCommandExecute",
      BeforeUndo: "BeforeUndo",
      BeforeRedo: "BeforeRedo",
      SheetEditStarted: "SheetEditStarted",
      SheetEditEnded: "SheetEditEnded",
      SheetValueChanged: "SheetValueChanged",
      SelectionChanged: "SelectionChanged",
    },
    getCurrentLifecycleStage: () => lifecycleStage,
    addEvent(evName, cb) {
      if (!handlers[evName]) handlers[evName] = [];
      handlers[evName].push(cb);
      const disp = {
        disposed: false,
        dispose() {
          this.disposed = true;
          disposedItems.push(`event:${evName}`);
        },
      };
      return disp;
    },
    onCommandExecuted(cb) {
      onCmdExec = cb;
      return {
        dispose() {
          disposedItems.push("onCommandExecuted");
        },
      };
    },
    createWorkbook: () => fWorkbook,
    createMenu(spec) {
      const entry = { spec, targets: [] };
      createdMenus.push(entry);
      return {
        appendTo(target) {
          entry.targets.push(target);
        },
      };
    },
    registerUIPart(part, compFn) {
      return {
        part,
        compFn,
        dispose() {
          disposedItems.push(`uipart:${part}`);
        },
      };
    },
    getShortcut() {
      shortcutCalls.getShortcut++;
      let disabled = false;
      return {
        disableShortcut() {
          disabled = true;
          shortcutCalls.disable++;
        },
        enableShortcut() {
          if (disabled) {
            disabled = false;
            shortcutCalls.enable++;
          }
        },
      };
    },
    dispose() {
      disposedItems.push("univerAPI.dispose");
    },
  };

  const univer = {
    registerPlugin() {},
    __getInjector: () => ({ get: () => ({ load() {} }) }),
  };

  const deps = {
    createUniver: () => ({ univer, univerAPI }),
    defaultTheme: {},
    LocaleType: { ZH_CN: "zhCN" },
    mergeLocales: (...args) => Object.assign({}, ...args),
    UniverSheetsCorePreset: () => ({ plugins: [] }),
    UniverPresetSheetsCoreZhCN: {},
  };

  const host = {
    style: {},
    classList: { add() {} },
  };

  const records = {
    inserts: [],
    deletes: [],
    bulkDeletes: [],
    bulkDuplicates: [],
    moves: [],
    moveRows: [],
    imports: 0,
    exports: 0,
    queues: 0,
    helps: 0,
    disposes: 0,
  };

  const opts = Object.assign(
    {
      host,
      sheets: [{ key: "test", name: "矩阵" }],
      fields: [
        { field_key: "case_no", display_name: "No", data_type: "text" },
        { field_key: "title", display_name: "Title", data_type: "text" },
      ],
      onInsert: (item, where, count) => records.inserts.push({ id: item ? item.id : null, where, count }),
      onDelete: (item) => records.deletes.push(item.id),
      onBulkDelete: (ids) => records.bulkDeletes.push(Array.from(ids)),
      onBulkDuplicate: (ids) => records.bulkDuplicates.push(Array.from(ids)),
      onMove: (ids, dir) => records.moves.push({ ids: Array.from(ids), dir }),
      onMoveRows: (ids, targetId, place) => records.moveRows.push({ ids: Array.from(ids), targetId, place }),
      onImport: () => { records.imports++; },
      onExport: () => { records.exports++; },
      onQueue: () => { records.queues++; },
      onHelp: () => { records.helps++; },
      onDispose: () => { records.disposes++; },
    },
    optsOverrides
  );

  const adapter = new Adapter(deps, opts);
  if (optsOverrides._initialStage !== undefined) {
    lifecycleStage = optsOverrides._initialStage;
  }
  adapter.init();

  const defaultItems =
    itemsInput !== null
      ? itemsInput
      : [
          { id: 101, version: 1, case_no: "TC-1", title: "Alpha" },
          { id: 102, version: 1, case_no: "TC-2", title: "Beta" },
          { id: 103, version: 1, case_no: "TC-3", title: "Gamma" },
        ];
  adapter.setSheetData("test", defaultItems);

  function fireBeforeCommand(id, params = {}) {
    const event = { id, params: Object.assign({ unitId: "wb-main", subUnitId: "sheet-test" }, params) };
    (handlers.BeforeCommandExecute || []).forEach((fn) => fn(event));
    return event;
  }

  return {
    adapter,
    fSheet,
    fWorkbook,
    univerAPI,
    handlers,
    onCmdExec: () => onCmdExec,
    createdMenus,
    shortcutCalls,
    editableCalls,
    disposedItems,
    records,
    fireBeforeCommand,
  };
}

test("BeforeCommandExecute cancels column structural commands while respecting workbook scope and applying flag", () => {
  const h = createAdapterHarness();
  const colCommands = [
    "sheet.command.insert-col",
    "sheet.command.insert-col-before",
    "sheet.command.insert-col-after",
    "sheet.command.insert-multi-cols-before",
    "sheet.command.insert-multi-cols-right",
    "sheet.command.remove-col",
    "sheet.command.remove-col-confirm",
    "sheet.command.move-cols",
  ];
  for (const id of colCommands) {
    const ev = h.fireBeforeCommand(id, { range: { startColumn: 1, endColumn: 1 } });
    assert.equal(ev.cancel, true, `Expected ${id} to be cancelled`);
  }

  const otherWb = h.fireBeforeCommand("sheet.command.insert-col", { unitId: "wb-other" });
  assert.equal(otherWb.cancel, undefined);

  h.adapter.applying = true;
  const duringApply = h.fireBeforeCommand("sheet.command.insert-col", {});
  assert.equal(duringApply.cancel, undefined);
  h.adapter.applying = false;
});

test("BeforeCommandExecute routes insert-row single/multi, UP/DOWN, and protects header row 0", () => {
  const h = createAdapterHarness();

  // Single row UP above row 2 (item 102)
  let ev = h.fireBeforeCommand("sheet.command.insert-row", {
    direction: 0,
    range: { startRow: 2, endRow: 2 },
  });
  assert.equal(ev.cancel, true);
  assert.deepEqual(h.records.inserts.at(-1), { id: 102, where: "above", count: 1 });

  // Multi-row (count = 3) DOWN below row 1 (item 101 -> Univer passes startRow = 2, endRow = 4, direction = 2)
  ev = h.fireBeforeCommand("sheet.command.insert-row", {
    direction: 2,
    range: { startRow: 2, endRow: 4 },
  });
  assert.equal(ev.cancel, true);
  assert.deepEqual(h.records.inserts.at(-1), { id: 101, where: "below", count: 3 });

  // String direction "DOWN" on last row (row 3 -> Univer passes startRow = 4, endRow = 5)
  ev = h.fireBeforeCommand("sheet.command.insert-row-by-range", {
    direction: "DOWN",
    range: { startRow: 4, endRow: 5 },
  });
  assert.equal(ev.cancel, true);
  assert.deepEqual(h.records.inserts.at(-1), { id: 103, where: "below", count: 2 });

  // Header row 0 + UP: cancelled as no-op without tail-append!
  const prevCount = h.records.inserts.length;
  ev = h.fireBeforeCommand("sheet.command.insert-row", {
    direction: 0,
    range: { startRow: 0, endRow: 0 },
  });
  assert.equal(ev.cancel, true);
  assert.equal(h.records.inserts.length, prevCount);

  // Pre-insert commands (insert-row-before / insert-multi-rows-above) when header row 0 is active
  h.fSheet.setSelectionRow(0);
  ev = h.fireBeforeCommand("sheet.command.insert-row-before", { value: 1 });
  assert.equal(ev.cancel, true);
  ev = h.fireBeforeCommand("sheet.command.insert-multi-rows-above", { value: 3 });
  assert.equal(ev.cancel, true);
  assert.equal(h.records.inserts.length, prevCount);

  // Header row 0 + DOWN (Univer passes startRow = 1, direction = 2): inserts ABOVE first item (101)!
  ev = h.fireBeforeCommand("sheet.command.insert-row", {
    direction: 2,
    range: { startRow: 1, endRow: 2 },
  });
  assert.equal(ev.cancel, true);
  assert.deepEqual(h.records.inserts.at(-1), { id: 101, where: "above", count: 2 });

  // Empty sheet + Header row 0 + DOWN: appends count rows via onInsert(null, "below", count)
  const emptyHarness = createAdapterHarness({}, []);
  const emptyEv = emptyHarness.fireBeforeCommand("sheet.command.insert-row", {
    direction: 2,
    range: { startRow: 1, endRow: 3 },
  });
  assert.equal(emptyEv.cancel, true);
  assert.deepEqual(emptyHarness.records.inserts, [{ id: null, where: "below", count: 3 }]);

  // Post-execution onCommandExecuted no longer double-fires structural insert
  const beforePostCount = h.records.inserts.length;
  h.onCmdExec()({ id: "sheet.command.insert-row", params: { range: { startRow: 1, endRow: 1 } } });
  assert.equal(h.records.inserts.length, beforePostCount);
});

test("BeforeCommandExecute routes remove-row variants, protects header row 0, and clears structuralPending when confirm is cancelled", async () => {
  let resolveConfirm;
  const confirmPromise = new Promise((r) => { resolveConfirm = r; });
  const h = createAdapterHarness({
    onDelete: () => confirmPromise,
  });

  // Header row 0 alone -> cancelled no-op
  let ev = h.fireBeforeCommand("sheet.command.remove-row", {
    range: { startRow: 0, endRow: 0 },
  });
  assert.equal(ev.cancel, true);
  assert.equal(h.adapter.structuralPending, false);

  // Explicit single-row range -> calls onDelete, holds structuralPending while confirm is open, clears on cancel
  const rangeCallsBefore = h.fSheet.calls.clearContent.length + h.fSheet.calls.setValues.length;
  ev = h.fireBeforeCommand("sheet.command.remove-row", {
    range: { startRow: 1, endRow: 1 },
  });
  assert.equal(ev.cancel, true);
  assert.equal(h.adapter.structuralPending, true);

  resolveConfirm(false);
  await confirmPromise;
  await Promise.resolve();
  assert.equal(h.adapter.structuralPending, false);
  const rangeCallsAfter = h.fSheet.calls.clearContent.length + h.fSheet.calls.setValues.length;
  assert.equal(rangeCallsAfter, rangeCallsBefore, "Canvas must remain untouched when confirm is cancelled");

  // Multi-range with header row 0 included -> skips row 0 and deduplicates
  const h2 = createAdapterHarness();
  ev = h2.fireBeforeCommand("sheet.command.remove-row-by-range", {
    ranges: [
      { startRow: 0, endRow: 1 },
      { startRow: 1, endRow: 1 },
      { startRow: 3, endRow: 3 },
    ],
  });
  assert.equal(ev.cancel, true);
  assert.deepEqual(h2.records.bulkDeletes.at(-1), [101, 103]);

  // remove-row-confirm without range -> falls back to non-contiguous ctx.selected in items order
  h2.adapter._setRowSelected(103, true);
  h2.adapter._setRowSelected(101, true);
  ev = h2.fireBeforeCommand("sheet.command.remove-row-confirm", {});
  assert.equal(ev.cancel, true);
  assert.deepEqual(h2.records.bulkDeletes.at(-1), [101, 103]);
});

test("BeforeCommandExecute routes move-rows upward/downward and blocks self-drop and header row 0 drop", () => {
  const h = createAdapterHarness({}, [
    { id: 10, version: 1, case_no: "1", title: "A" },
    { id: 20, version: 1, case_no: "2", title: "B" },
    { id: 30, version: 1, case_no: "3", title: "C" },
    { id: 40, version: 1, case_no: "4", title: "D" },
  ]);

  // Upward drag: row 3 (id=30) to row 1 (before id=10)
  let ev = h.fireBeforeCommand("sheet.command.move-rows", {
    fromRange: { startRow: 3, endRow: 3 },
    toRange: { startRow: 1, endRow: 1 },
  });
  assert.equal(ev.cancel, true);
  assert.deepEqual(h.records.moveRows.at(-1), { ids: [30], targetId: 10, place: "before" });

  // Downward drag: row 1 (id=10) below row 3 (Univer passes toRow = 4 -> anchor row 3 = id 30, "after")
  ev = h.fireBeforeCommand("sheet.command.move-rows", {
    fromRange: { startRow: 1, endRow: 1 },
    toRange: { startRow: 4, endRow: 4 },
  });
  assert.equal(ev.cancel, true);
  assert.deepEqual(h.records.moveRows.at(-1), { ids: [10], targetId: 30, place: "after" });

  // Multi-row selected drag: rows 1..2 (ids 10, 20) dragged from row 2 to end of sheet (toRow = 5 -> after id 40)
  h.adapter._setRowSelected(10, true);
  h.adapter._setRowSelected(20, true);
  ev = h.fireBeforeCommand("sheet.command.move-rows", {
    fromRange: { startRow: 2, endRow: 2 },
    toRange: { startRow: 5, endRow: 5 },
  });
  assert.equal(ev.cancel, true);
  assert.deepEqual(h.records.moveRows.at(-1), { ids: [10, 20], targetId: 40, place: "after" });
  h.adapter.clearSelection();

  // Self-drop no-ops (for rows 2..3, s=2, e=3 -> toRow in [2, 3, 4] is no-op)
  const countBeforeNoop = h.records.moveRows.length;
  for (const noopTo of [2, 3, 4]) {
    ev = h.fireBeforeCommand("sheet.command.move-rows", {
      fromRange: { startRow: 2, endRow: 3 },
      toRange: { startRow: noopTo, endRow: noopTo },
    });
    assert.equal(ev.cancel, true);
  }
  // Header drop protection: toRow = 0
  ev = h.fireBeforeCommand("sheet.command.move-rows", {
    fromRange: { startRow: 3, endRow: 3 },
    toRange: { startRow: 0, endRow: 0 },
  });
  assert.equal(ev.cancel, true);
  assert.equal(h.records.moveRows.length, countBeforeNoop);
});

test("setRowFormats 1-arg and 2-arg overloads, >1000 dynamic row expansion, and shrinking stale-row clear", () => {
  const h = createAdapterHarness();

  // Render 1200 rows -> _ensureHeight grows sheet beyond 1000 rows without throwing!
  const bigItems = Array.from({ length: 1200 }, (_, i) => ({
    id: i + 1,
    version: 1,
    case_no: `TC-${i + 1}`,
    title: `Row ${i + 1}`,
  }));
  h.adapter.setSheetData("test", bigItems);
  assert.ok(h.fSheet.calls.setRowCount.includes(1221));
  assert.equal(h.fSheet.getMaxRows(), 1221);

  // 1-arg setRowFormats overload (targets active sheet) + preserves errCells
  h.adapter.setCellErrors({ 2: { cells: ["title"], message: "Invalid" } });
  h.fSheet.calls.setBackgroundColor.length = 0;
  h.adapter.setRowFormats({ 1: "#e6ffed", 2: "#ffeef0" });
  assert.equal(h.fSheet.calls.setBackgroundColor[0].numRows, 1200);
  assert.equal(h.fSheet.calls.setBackgroundColor[0].color, null);
  assert.ok(h.fSheet.calls.setBackgroundColor.some((c) => c.row === 1 && c.color === "#e6ffed"));
  assert.ok(h.fSheet.calls.setBackgroundColor.some((c) => c.row === 2 && c.color === "#ffeef0"));
  assert.ok(h.fSheet.calls.setBackgroundColor.some((c) => c.row === 2 && c.col === 1 && c.color === "#fff4f4"));

  // 2-arg setRowFormats overload clears previous 1200 formatted rows even with empty map
  h.fSheet.calls.setBackgroundColor.length = 0;
  h.adapter.setRowFormats("test", { 3: "#fffbdd" });
  assert.equal(h.fSheet.calls.setBackgroundColor[0].numRows, 1200);
  assert.ok(h.fSheet.calls.setBackgroundColor.some((c) => c.row === 3 && c.color === "#fffbdd"));

  // Shrinking from 1200 rows to 5 rows clears all 1200 stale rows (clamped to maxRows - 1)
  h.fSheet.calls.clearContent.length = 0;
  h.adapter.setSheetData("test", bigItems.slice(0, 5));
  assert.equal(h.fSheet.calls.clearContent[0].numRows, 1200);
});

test("createMenu registers Ribbon/ContextMenu entries, _runAtRendered handles already-Rendered stage, setSuspended, and dispose", () => {
  const makeSlotNode = (id) => {
    const node = {
      id,
      hidden: true,
      parentNode: null,
      parentElement: null,
    };
    return node;
  };
  const footerSlot = makeSlotNode("lm-footer-slot");
  const toolbarLeft = makeSlotNode("lm-toolbar-left");
  const toolbarRight = makeSlotNode("lm-toolbar-right");
  const fakeWrapper = {
    children: [footerSlot, toolbarLeft, toolbarRight],
    removeChild(child) {
      const idx = this.children.indexOf(child);
      if (idx !== -1) this.children.splice(idx, 1);
      child.parentNode = null;
      child.parentElement = null;
    },
  };
  footerSlot.parentNode = fakeWrapper;
  toolbarLeft.parentNode = fakeWrapper;
  toolbarRight.parentNode = fakeWrapper;

  // _initialStage = 2 (Rendered) -> _runAtRendered fires synchronously during init() without waiting for LifeCycleChanged!
  const h = createAdapterHarness({
    _initialStage: 2,
    footerSlot,
    toolbarLeft,
    toolbarRight,
  });
  let darkDisconnected = false;
  h.adapter._darkObserver = { disconnect() { darkDisconnected = true; } };

  const menuById = Object.fromEntries(h.createdMenus.map((m) => [m.spec.id, m]));
  assert.deepEqual(toHostRealm(menuById["lm.menu.import"].targets), ["ribbon.start.others", "ribbon.data.others"]);
  assert.deepEqual(toHostRealm(menuById["lm.menu.export"].targets), ["ribbon.start.others", "ribbon.data.others"]);
  assert.deepEqual(toHostRealm(menuById["lm.menu.queue"].targets), ["ribbon.start.others", "ribbon.data.others"]);
  // 队列测试 sits between 导出 and 帮助 in the Ribbon group it is appended to.
  const mountedIds = h.createdMenus.map((menu) => menu.spec.id);
  assert.ok(mountedIds.indexOf("lm.menu.queue") > mountedIds.indexOf("lm.menu.export"));
  assert.ok(mountedIds.indexOf("lm.menu.queue") < mountedIds.indexOf("lm.menu.help"));
  assert.deepEqual(toHostRealm(menuById["lm.menu.help"].targets), ["ribbon.start.others"]);
  // 帮助 is merged into the built-in keyboard-icon button: our item borrows that
  // glyph, and the built-in shortcut item is hidden so only one help entry shows.
  assert.equal(menuById["lm.menu.help"].spec.icon, "ShortcutIcon");
  assert.equal(menuById["lm.menu.help"].spec.title, "帮助");
  assert.deepEqual(toHostRealm(menuById["lm.ctx.duplicate-row"].targets), [
    ["contextMenu.rowHeader", "contextMenu.others"],
    ["contextMenu.mainArea", "contextMenu.others"],
  ]);
  assert.deepEqual(toHostRealm(menuById["lm.ctx.move-row-up"].targets), [
    ["contextMenu.rowHeader", "contextMenu.others"],
    ["contextMenu.mainArea", "contextMenu.others"],
  ]);
  assert.deepEqual(toHostRealm(menuById["lm.ctx.move-row-down"].targets), [
    ["contextMenu.rowHeader", "contextMenu.others"],
    ["contextMenu.mainArea", "contextMenu.others"],
  ]);

  // Firing LifeCycleChanged Steady afterwards does not duplicate menus
  (h.handlers.LifeCycleChanged || []).forEach((fn) => fn({ stage: 3 }));
  assert.equal(h.createdMenus.length, 7);

  // Invoke Ribbon & ContextMenu actions
  menuById["lm.menu.import"].spec.action();
  menuById["lm.menu.export"].spec.action();
  menuById["lm.menu.queue"].spec.action();
  menuById["lm.menu.help"].spec.action();
  assert.equal(h.records.imports, 1);
  assert.equal(h.records.exports, 1);
  assert.equal(h.records.queues, 1);
  assert.equal(h.records.helps, 1);

  h.adapter._setRowSelected(102, true);
  menuById["lm.ctx.duplicate-row"].spec.action();
  menuById["lm.ctx.move-row-up"].spec.action();
  menuById["lm.ctx.move-row-down"].spec.action();
  assert.deepEqual(h.records.bulkDuplicates, [[102]]);
  assert.deepEqual(h.records.moves, [
    { ids: [102], dir: "up" },
    { ids: [102], dir: "down" },
  ]);

  // setSuspended(true) disables shortcuts on cached FShortcut, sets editable=false, and cancels mutating commands
  h.adapter.setSuspended(true);
  assert.equal(h.adapter.isSuspended(), true);
  assert.equal(h.shortcutCalls.getShortcut, 1);
  assert.equal(h.shortcutCalls.disable, 1);
  assert.deepEqual(h.editableCalls, [false]);

  const pasteEv = h.fireBeforeCommand("sheet.command.paste", {});
  const undoEv = h.fireBeforeCommand("univer.command.undo", {});
  assert.equal(pasteEv.cancel, true);
  assert.equal(undoEv.cancel, true);

  // setSuspended(false) reuses the SAME FShortcut instance so enableShortcut actually disposes the forceDisable lock!
  h.adapter.setSuspended(false);
  assert.equal(h.adapter.isSuspended(), false);
  assert.equal(h.shortcutCalls.getShortcut, 1);
  assert.equal(h.shortcutCalls.enable, 1);
  assert.deepEqual(h.editableCalls, [false, true]);

  // dispose() is idempotent, detaches adopted slots, disconnects _darkObserver, disposes listeners & univerAPI
  h.adapter.dispose();
  h.adapter.dispose();
  assert.equal(footerSlot.parentNode, null);
  assert.equal(toolbarLeft.parentNode, null);
  assert.equal(toolbarRight.parentNode, null);
  assert.equal(darkDisconnected, true);
  assert.equal(h.records.disposes, 1);
  assert.equal(h.disposedItems.filter((x) => x === "univerAPI.dispose").length, 1);
});

test("UniverStepsView handles already-Rendered lifecycle stage and idempotently attaches secondary presets", () => {
  let tooltipInits = 0;
  const { exports } = loadTsModule("../src/steps_adapter.ts", {
    "./adapter": { toCellData: (v) => v, cellReadToText: (v) => v },
    "./cell_tooltip": {
      CellTooltip: class {
        init() { tooltipInits++; }
        dispose() {}
      },
    },
  });
  const UniverStepsView = exports.UniverStepsView;
  const registeredPlugins = [];
  const loadedLocales = [];
  const LocaleServiceToken = Symbol("LocaleService");

  const univer = {
    registerPlugin(p) { registeredPlugins.push(p); },
    __getInjector: () => ({
      get: (tok) => (tok === LocaleServiceToken ? { load: (l) => loadedLocales.push(l) } : null),
    }),
  };
  const univerAPI = {
    Enum: { LifecycleStages: { Rendered: 2, Steady: 3 } },
    Event: { LifeCycleChanged: "LifeCycleChanged" },
    getCurrentLifecycleStage: () => 2,
    addEvent: () => ({ dispose() {} }),
    createWorkbook: () => ({ getId: () => "wb-steps", getActiveSheet: () => null }),
    dispose() {},
  };
  const sharedDeps = {
    createUniver: () => ({ univer, univerAPI }),
    defaultTheme: {},
    LocaleType: { ZH_CN: "zhCN" },
    LocaleService: LocaleServiceToken,
    mergeLocales: (...args) => Object.assign({}, ...args),
    UniverSheetsCorePreset: () => ({ plugins: [] }),
    UniverPresetSheetsCoreZhCN: { core: true },
  };
  const host = { style: {}, classList: { add() {} } };
  const view = new UniverStepsView(sharedDeps, host);
  view.init();

  // Tooltip mounted immediately because getCurrentLifecycleStage() === Rendered!
  assert.equal(tooltipInits, 1);

  const extra = {
    UniverSheetsDataValidationPreset: () => ({ plugins: ["dv-plugin"] }),
    UniverPresetSheetsDataValidationZhCN: { dv: "zh" },
    UniverSheetsFindReplacePreset: () => ({ plugins: ["fr-plugin"] }),
    UniverPresetSheetsFindReplaceZhCN: { fr: "zh" },
    UniverSheetsConditionalFormattingPreset: () => ({ plugins: ["cf-plugin"] }),
    UniverPresetSheetsConditionalFormattingZhCN: { cf: "zh" },
  };
  // Simulate univer-esm-entry mutating sharedDeps before calling view.attachSecondaryPresets(extra)
  Object.assign(sharedDeps, extra);
  view.attachSecondaryPresets(extra);
  view.attachSecondaryPresets(extra);

  assert.deepEqual(toHostRealm(registeredPlugins), ["fr-plugin", "cf-plugin", "dv-plugin"]);
  assert.equal(loadedLocales.length, 1);
  assert.deepEqual(toHostRealm(loadedLocales[0]), { zhCN: { dv: "zh", fr: "zh", cf: "zh" } });

  let onDisposedCount = 0;
  view._onDisposed = () => { onDisposedCount++; };
  view.dispose();
  view.dispose();
  assert.equal(onDisposedCount, 1);
});

test("univer-esm-entry schedules secondary load for steps views and prunes disposed instances", async () => {
  const attachedGrid = [];
  const attachedSteps = [];
  let idleCb = null;

  class FakeAdapter {
    constructor() { this._onDisposed = null; }
    init() {}
    attachSecondaryPresets(extra) { attachedGrid.push(extra); }
    dispose() { if (this._onDisposed) this._onDisposed(); }
  }

  class FakeStepsView {
    constructor() { this._onDisposed = null; }
    init() {}
    attachSecondaryPresets(extra) { attachedSteps.push(extra); }
    dispose() { if (this._onDisposed) this._onDisposed(); }
  }

  const win = {
    requestIdleCallback(cb) { idleCb = cb; },
    setTimeout,
  };

  loadTsModule(
    "../src/univer-esm-entry.ts",
    {
      "./adapter": { UniverGridAdapter: FakeAdapter },
      "./steps_adapter": {
        UniverStepsView: FakeStepsView,
        createStepsMount: () => () => {
          const v = new FakeStepsView();
          v.init();
          return v;
        },
      },
      "./univer-secondary": {
        secondaryDeps: { marker: "secondary-loaded" },
      },
    },
    { window: win }
  );

  const g1 = win.LMUniver.mount({ host: {} });
  const g2 = win.LMUniver.mount({ host: {} });
  const s1 = win.LMUniverSteps.mount({});
  const s2 = win.LMUniverSteps.mount({});
  assert.equal(typeof idleCb, "function");

  // Dispose g1 and s1 BEFORE secondary presets finish loading -> only g2 and s2 should receive attachSecondaryPresets!
  g1.dispose();
  s1.dispose();

  await win.LMUniver.loadSecondary();
  assert.equal(attachedGrid.length, 1);
  assert.equal(attachedSteps.length, 1);
  assert.equal(attachedGrid[0].marker, "secondary-loaded");
  assert.equal(attachedSteps[0].marker, "secondary-loaded");
  g2.dispose();
  s2.dispose();
});

test("editor_qualification leaves the editor toolbar untouched and queues the project's Clean-Room value", async () => {
  function makeDom() {
    const elements = new Map();
    const makeEl = (tag, id = "") => {
      const el = {
        tagName: String(tag).toUpperCase(),
        id,
        hidden: false,
        checked: false,
        value: "",
        style: {},
        children: [],
        parentNode: null,
        listeners: {},
        addEventListener(type, fn) {
          this.listeners[type] = fn;
        },
        appendChild(child) {
          if (child && typeof child === "object") {
            child.parentNode = this;
            this.children.push(child);
            if (child.id) elements.set(child.id, child);
          }
          return child;
        },
        insertBefore(node) {
          node.parentNode = this;
          this.children.push(node);
          if (node.id) elements.set(node.id, node);
          return node;
        },
      };
      if (id) elements.set(id, el);
      return el;
    };
    const toolbarRight = makeEl("div", "lm-toolbar-right");
    const runBtn = makeEl("button", "lm-run-selected");
    toolbarRight.appendChild(runBtn);
    const body = makeEl("body", "body");
    body.appendChild(toolbarRight);
    const doc = {
      readyState: "complete",
      body,
      getElementById: (id) => elements.get(id) || null,
      createElement: (tag) => makeEl(tag),
      createTextNode: (txt) => ({ textContent: txt }),
      // No .lm-exec-config / .lm-editor root: this is the editor page.
      querySelector: () => null,
    };
    return { doc, toolbarRight, elements };
  }

  const { doc, toolbarRight } = makeDom();
  const runCalls = [];
  let getProjectCalls = 0;
  const LMApi = {
    async runSelectedTasks(pid, payload) {
      runCalls.push({ pid, payload: toHostRealm(payload) });
      return { created: [] };
    },
    async rerunSelectedTasks() {
      return { created: [] };
    },
    async getProject() {
      getProjectCalls += 1;
      return { project: { id: 7, default_signal_list: "", default_clean_room: true } };
    },
    async patchProject() {
      throw new Error("the editor must not write execution settings");
    },
  };
  const window = { LM: { projectId: 7 }, LMApi };
  const { exports } = loadTsModule("../src/lanmatrix/editor_qualification.ts", {}, {
    window,
    document: doc,
  });
  const ctrl = exports.controller || exports.default || window.LMEditorQualification;
  assert.ok(ctrl, "the module exports a controller");

  const toolbarBefore = toolbarRight.children.map((child) => child.id);
  assert.deepEqual(toolbarBefore, ["lm-run-selected"], "no control is injected into the editor toolbar");

  // The page no longer hosts a checkbox, so the queued value is read from the project.
  ctrl.init();
  assert.equal(ctrl.isCleanRoomEnabled(), false, "callers see false until the setting is read");
  await LMApi.runSelectedTasks(7, { test_ids: ["TC_7"] });
  assert.equal(runCalls.length, 1);
  assert.equal(runCalls[0].payload.clean_room, true, "clean_room rides on the existing task body");
  assert.equal(getProjectCalls, 1);
  await LMApi.rerunSelectedTasks(7, ["TC_7"]);
  assert.equal(getProjectCalls, 1, "the saved switch is read once per page");
  assert.deepEqual(toolbarRight.children.map((child) => child.id), ["lm-run-selected"]);
  assert.equal(typeof ctrl.syncVisibility, "undefined", "the toolbar visibility hook is gone");
});

test("editor_qualification refuses to queue when the Clean-Room setting cannot be read", async () => {
  const elements = new Map();
  const doc = {
    readyState: "complete",
    body: { appendChild() {} },
    getElementById: (id) => elements.get(id) || null,
    createElement: () => ({ id: "", children: [], appendChild() {}, setAttribute() {} }),
    createTextNode: (txt) => ({ textContent: txt }),
    querySelector: () => null,
  };

  async function load(getProject) {
    const runCalls = [];
    const LMApi = {
      async runSelectedTasks(pid, payload) {
        runCalls.push({ pid, payload });
        return { created: [] };
      },
      async rerunSelectedTasks(pid, keys) {
        runCalls.push({ pid, keys, rerun: true });
        return { created: [] };
      },
      getProject,
    };
    const window = { LM: { projectId: 7 }, LMApi };
    const { exports } = loadTsModule("../src/lanmatrix/editor_qualification.ts", {}, {
      window,
      document: doc,
    });
    const ctrl = exports.controller || exports.default || window.LMEditorQualification;
    ctrl.init();
    return { LMApi, runCalls };
  }

  const failed = await load(async () => { throw new Error("offline"); });
  await assert.rejects(
    () => failed.LMApi.runSelectedTasks(7, { test_ids: ["TC_7"], clean_room: true }),
    /已取消入队/,
  );
  assert.equal(failed.runCalls.length, 0, "a failed read must not enqueue, even if the caller set clean_room");

  const empty = await load(async () => ({ project: { id: 7 } }));
  await assert.rejects(
    () => empty.LMApi.rerunSelectedTasks(7, ["TC_7"]),
    /已取消入队/,
  );
  assert.equal(empty.runCalls.length, 0, "a project payload without a boolean must not enqueue as off");
});

test("UniverGridAdapter temporarily restores workbook editability during programmatic updates while suspended", () => {
  const { exports } = loadTsModule("../src/adapter.ts", {
    "./collab_overlay": {
      CollabOverlay: class {
        setSelections() {}
        refresh() {}
        dispose() {}
      },
    },
    "./cell_tooltip": {
      CellTooltip: class {
        init() {}
        dispose() {}
      },
    },
  });
  const Adapter = exports.UniverGridAdapter;

  let editable = true;
  const editableLog = [];
  const violations = [];
  const commandLog = [];
  const recordCommand = (name) => {
    commandLog.push({ name, editable });
    if (!editable) violations.push(name);
  };

  let maxRows = 1000;
  let maxCols = 64;
  const fSheet = {
    getSheetId: () => "sheet-test",
    getSheetName: () => "矩阵",
    getMaxRows: () => maxRows,
    getRowCount: () => maxRows,
    setRowCount: (n) => { recordCommand("setRowCount"); maxRows = n; },
    getMaxColumns: () => maxCols,
    getColumnCount: () => maxCols,
    setColumnCount: (n) => { recordCommand("setColumnCount"); maxCols = n; },
    setColumnWidth: () => recordCommand("setColumnWidth"),
    setRowHeightsForced: () => recordCommand("setRowHeightsForced"),
    setFreeze: () => recordCommand("setFreeze"),
    getFilter: () => ({ remove: () => recordCommand("filter.remove") }),
    getRange: () => ({
      clearContent: () => recordCommand("clearContent"),
      setValues: () => recordCommand("setValues"),
      setValue: () => recordCommand("setValue"),
      setBackgroundColor: () => recordCommand("setBackgroundColor"),
      setFontWeight: () => recordCommand("setFontWeight"),
      setHorizontalAlignment: () => recordCommand("setHorizontalAlignment"),
      setDataValidation: () => recordCommand("setDataValidation"),
      setNote: () => recordCommand("setNote"),
      createFilter: () => recordCommand("createFilter"),
    }),
  };
  const fWorkbook = {
    getId: () => "wb-main",
    getActiveSheet: () => fSheet,
    getSheetByName: () => fSheet,
    setActiveSheet: () => {},
    setEditable: (v) => {
      editable = Boolean(v);
      editableLog.push(editable);
    },
  };
  const univerAPI = {
    Enum: { LifecycleStages: { Rendered: 2, Steady: 3 }, BuiltInUIPart: { FOOTER: "footer", TOOLBAR: "toolbar" } },
    Event: { LifeCycleChanged: "LifeCycleChanged" },
    getCurrentLifecycleStage: () => 2,
    addEvent: () => ({ dispose() {} }),
    onCommandExecuted: () => ({ dispose() {} }),
    createWorkbook: () => fWorkbook,
    newDataValidation: () => {
      const builder = {
        requireValueInList: () => builder,
        requireDateBetween: () => builder,
        setOptions: () => builder,
        build: () => ({ built: true }),
      };
      return builder;
    },
    dispose: () => {},
  };
  const univer = { registerPlugin: () => {} };

  const host = { style: {}, classList: { add() {} } };
  const adapter = new Adapter(
    {
      createUniver: () => ({ univer, univerAPI }),
      defaultTheme: {},
      LocaleType: { ZH_CN: "zhCN" },
      mergeLocales: (...args) => Object.assign({}, ...args),
      UniverSheetsCorePreset: () => ({}),
      UniverPresetSheetsCoreZhCN: {},
      UniverSheetsFilterPreset: () => ({}),
    },
    {
      host,
      sheets: [{ key: "test", name: "矩阵" }],
      fields: [
        { field_key: "test_id", display_name: "Test ID", data_type: "text" },
        { field_key: "result", display_name: "結果", data_type: "single_select", options: ["OK", "NG"] },
        { field_key: "steps", display_name: "步骤明细", data_type: "steps" },
      ],
    },
  );
  adapter.init();
  adapter.setSheetData("test", [{ id: 1, version: 1, test_id: "T1", result: "OK", steps: "{}" }]);

  adapter.setSuspended(true);
  assert.equal(editable, false);
  editableLog.length = 0;
  commandLog.length = 0;

  const checkBatch = (label, fn) => {
    editableLog.length = 0;
    const beforeCount = commandLog.length;
    fn();
    assert.ok(commandLog.length > beforeCount, `${label}: expected Univer commands to execute`);
    assert.deepEqual(toHostRealm(editableLog), [true, false], `${label}: should lift editable once for the batch and restore false`);
    assert.equal(editable, false, `${label}: must remain non-editable while suspended`);
    assert.equal(adapter.isSuspended(), true);
  };

  checkBatch("setSheetFields", () => {
    adapter.setSheetFields("test", [
      { field_key: "test_id", display_name: "Test ID", data_type: "text" },
      { field_key: "result", display_name: "結果", data_type: "single_select", options: ["OK", "NG"] },
      { field_key: "steps", display_name: "步骤明细", data_type: "steps" },
    ]);
  });
  checkBatch("setSheetData", () => {
    adapter.setSheetData("test", [
      { id: 1, version: 2, test_id: "T1", result: "NG", steps: '{"steps":[{}]}' },
      { id: 2, version: 1, test_id: "T2", result: "OK", steps: "{}" },
    ]);
  });
  checkBatch("patchSheetData", () => {
    adapter.patchSheetData("test", [
      { id: 1, version: 3, test_id: "T1", result: "OK", steps: '{"steps":[{}]}' },
      { id: 2, version: 1, test_id: "T2", result: "OK", steps: "{}" },
    ]);
  });
  checkBatch("setRowFormats", () => {
    adapter.setRowFormats("test", { 1: "#eaf7ee", 2: "#fdecec" });
  });
  checkBatch("setCellErrors", () => {
    adapter.setCellErrors({ 1: { cells: ["test_id"], message: "冲突" } });
  });
  checkBatch("attachSecondaryPresets", () => {
    adapter.attachSecondaryPresets({
      UniverSheetsDataValidationPreset: () => ({ plugins: ["dv"] }),
    });
  });

  assert.deepEqual(toHostRealm(violations), []);

  adapter.setSuspended(false);
  assert.equal(editable, true);
  adapter.dispose();
});
