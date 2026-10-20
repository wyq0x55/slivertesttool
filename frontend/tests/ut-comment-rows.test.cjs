const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");
const ts = require("typescript");

const root = path.resolve(__dirname, "../..");
const source = path.join(root, "frontend/src/steps_adapter.ts");
const compiled = ts.transpileModule(fs.readFileSync(source, "utf8"), {
  compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2020 },
});
const exports_ = {};
vm.runInNewContext(compiled.outputText, {
  exports: exports_, console,
  require: () => ({ toCellData: v => v, cellReadToText: v => v }),
});
const window = {};
vm.runInNewContext(fs.readFileSync(path.join(root, "app/static/js/lanmatrix/ut_testcsv.js"), "utf8"), { window });
const csv = window.LMTestCsv;
const plain = value => JSON.parse(JSON.stringify(value));
const CSV = [
  "mod,f,t,1,1",
  "#COMMENT,x,ret",
  ";$L$,if (x)",
  ";$L$,TRUE",
  ",1,2",
  ";$L$,FALSE",
  ",0,0",
].join("\r\n") + "\r\n";

function sheet() {
  const sh = { data: [], merges: [], paints: [], heights: [], wraps: [], cols: 40, rows: 500 };
  sh.getRange = (row, col, rows = 1, cols = 1) => {
    const area = { startRow: row, endRow: row + rows - 1, startColumn: col, endColumn: col + cols - 1 };
    return {
      getRange: () => area,
      clearContent() {
        for (let r = row; r < row + rows; r++) {
          if (!sh.data[r]) continue;
          for (let c = col; c < col + cols; c++) delete sh.data[r][c];
        }
        while (sh.data.length && !(sh.data.at(-1) || []).some(v => v != null && v !== "")) sh.data.pop();
      },
      setValues(matrix) {
        matrix.forEach((cells, i) => cells.forEach((value, j) => {
          (sh.data[row + i] ||= [])[col + j] = typeof value === "object" ? value.v : value;
        }));
      },
      getValues: () => sh.data,
      merge() { sh.merges.push(this); },
      breakApart() { sh.merges = sh.merges.filter(range => range !== this); },
      setBackgroundColor(color) { sh.paints.push({ ...area, color }); },
      setFontWeight() {},
      setHorizontalAlignment() {},
      setWrapStrategy(strategy) { sh.wraps.push({ ...area, strategy }); },
    };
  };
  sh.getMergedRanges = () => [...sh.merges];
  sh.getDataRange = () => ({
    getValues: () => sh.data,
    getRange: () => ({
      startRow: 0, startColumn: 0, endRow: Math.max(0, sh.data.length - 1),
      endColumn: Math.max(0, ...sh.data.map(row => (row || []).length - 1)),
    }),
  });
  sh.getMaxRows = () => sh.rows;
  sh.getMaxColumns = () => sh.cols;
  sh.setRowCount = n => { sh.rows = n; };
  sh.setColumnCount = n => { sh.cols = n; };
  sh.getRowHeight = () => 24;
  sh.setRowHeightsForced = (start, count, height) => sh.heights.push({ start, count, height });
  sh.setFreeze = options => { sh.freeze = options; };
  sh.activate = () => {};
  sh.hideSheet = () => { sh.hidden = true; };
  sh.showSheet = () => { sh.hidden = false; };
  sh.isSheetHidden = () => !!sh.hidden;
  return sh;
}

function harness(text = CSV) {
  const view = new exports_.UniverStepsView({}, {});
  const sheets = new Map(["入力値", "期待値", "手順"].map(name => [name, sheet()]));
  let commandListener;
  view.univerAPI = {
    Enum: { WrapStrategy: { CLIP: 1 } },
    onCommandExecuted(listener) { commandListener = listener; return { dispose() {} }; },
  };
  view.fWorkbook = { getSheetByName: name => sheets.get(name), setEditable() {}, getId: () => "test-workbook" };
  const model = csv.parse(text);
  view.setDoc(csv.toDoc(model), {
    layout: csv.LAYOUT,
    marks: csv.marks(model, { issues: [{ code: "ORACLE_MISMATCH",
      detail: [{ row: 1, column: "ret" }] }] }),
  });
  return { view, model, sh: sheets.get("手順"),
    command: id => commandListener({ id, params: { unitId: "test-workbook" } }) };
}

test("each comment occupies its own single-height full-width row before the values", () => {
  const { sh } = harness();
  assert.deepEqual(plain(sh.data), [
    ["x", "ret"], ["if (x)", ""], ["TRUE", ""], ["1", "2"], ["FALSE", ""], ["0", "0"],
  ]);
  assert.deepEqual(plain(sh.merges.map(range => range.getRange())), [
    { startRow: 1, endRow: 1, startColumn: 0, endColumn: 1 },
    { startRow: 2, endRow: 2, startColumn: 0, endColumn: 1 },
    { startRow: 4, endRow: 4, startColumn: 0, endColumn: 1 },
  ]);
  assert(sh.heights.every(h => h.height === 24));
  assert.equal(sh.wraps.length, 3);
  assert(sh.wraps.every(wrap => wrap.strategy === 1));
});

test("comment, input, output and disputed cells have distinct colours at the displayed row", () => {
  const { sh } = harness();
  function colorAt(row, col) {
    return sh.paints.filter(p => row >= p.startRow && row <= p.endRow &&
      col >= p.startColumn && col <= p.endColumn).at(-1).color;
  }
  assert.equal(colorAt(1, 1), "#e7f5ec");
  assert.equal(colorAt(3, 0), "#eef6ff");
  assert.equal(colorAt(3, 1), "#fff6ec");
  // Case #2 is on sheet row 6 after inserting three comment rows.
  assert.equal(colorAt(5, 1), "#f9c9c9");
  assert.equal(new Set([colorAt(1, 1), colorAt(3, 0), colorAt(3, 1), colorAt(5, 1)]).size, 4);
});

test("reading the projected rows round-trips the original CSV", () => {
  const { view, model } = harness();
  assert.equal(csv.serialize(model, view.getDoc()), CSV);
});

test("editing one comment and one value keeps the case order and other comments", () => {
  const { view, model, sh } = harness();
  sh.data[1][0] = "if (x == 1)";
  sh.data[5][1] = "3";
  assert.equal(csv.serialize(model, view.getDoc()),
    CSV.replace("if (x)", "if (x == 1)").replace(",0,0\r\n", ",0,3\r\n"));
});

test("comment cells containing line breaks are rejected instead of changing the CSV layout", () => {
  const { view, sh } = harness();
  sh.data[1][0] = "line one\nline two";
  assert.throws(() => view.getDoc(), /每条注释只占一行/);
});

test("single-signal candidates keep comment and data rows distinct", () => {
  const text = "mod,f,t,0,1\r\n#COMMENT,ret\r\n;$L$,TRUE\r\n,1\r\n";
  const { view, model } = harness(text);
  assert.equal(csv.serialize(model, view.getDoc()), text);
});

test("reusing the drawer clears old comment merges and restores the Silver layout", () => {
  const { view, sh } = harness();
  view.setDoc({ input_signals: [["x", "x"]], expected_signals: [["ret", "ret"]],
    steps: [{ no: 1, purpose: "purpose", inputs: ["1"], expecteds: ["2"] }] });
  assert.equal(sh.merges.length, 0);
  assert.deepEqual(plain(sh.data[0]), [
    "手順番号", "手順目的", "操作手順", "サブルーチン", "引数", "x", "ret", "確認タイミング",
  ]);
  const doc = view.getDoc();
  assert.equal(doc.steps.length, 1);
  assert.equal(doc.steps[0].purpose, "purpose");
  assert.deepEqual(plain(doc.steps[0].expecteds), ["2"]);
});

test("deleting a value row rejects saving instead of recreating phantom CSV cases", () => {
  const { view, sh, command } = harness();
  sh.data.pop();
  command("sheet.mutation.remove-rows");
  assert.throws(() => view.getDoc(), /表格行列结构已改变/);
});

test("single-column sorting or row moves cannot reinterpret comments as values", () => {
  const text = "mod,f,t,0,1\r\n#COMMENT,ret\r\n;$L$,TRUE\r\n,1\r\n";
  for (const id of ["sheet.command.sort-range-asc", "sheet.mutation.move-rows", "sheet.mutation.move-range"]) {
    const { view, sh, command } = harness(text);
    [sh.data[1], sh.data[2]] = [sh.data[2], sh.data[1]];
    command(id);
    assert.throws(() => view.getDoc(), /表格行列结构已改变/);
  }
});

test("ordinary value edits and selection navigation remain editable", () => {
  const { view, model, sh, command } = harness();
  sh.data[5][1] = "5";
  command("sheet.command.move-selection");
  command("sheet.mutation.set-range-values");
  assert.equal(csv.serialize(model, view.getDoc()), CSV.replace(",0,0\r\n", ",0,5\r\n"));
});

test("switching a projection longer than 500 rows to a short CSV clears all old cases", () => {
  const { view, model } = harness();
  const long = plain(csv.toDoc(model));
  long.steps = Array.from({ length: 300 }, (_, index) => ({
    no: index + 1, purpose: `case ${index + 1}`, inputs: [String(index)], expecteds: ["1"],
  }));
  view.setDoc(long, { layout: csv.LAYOUT });
  view.setDoc(csv.toDoc(model), { layout: csv.LAYOUT });
  assert.equal(csv.serialize(model, view.getDoc()), CSV);
  view.setDoc(long, { layout: csv.LAYOUT });
  view.setDoc({ input_signals: [["x", "x"]], expected_signals: [["ret", "ret"]],
    steps: [{ no: 1, purpose: "Silver case", inputs: ["9"], expecteds: ["10"] }] });
  assert.equal(view.getDoc().steps.length, 1);
  assert.equal(view.getDoc().steps[0].purpose, "Silver case");
});

test("invalid comment projections surface the error and never submit the previous CSV", async () => {
  const editorWindow = {};
  const source = fs.readFileSync(path.join(root, "app/static/js/lanmatrix/steps_editor.js"), "utf8")
    .replace("global.LMStepsEditor = LMStepsEditor;", "global.EditorUnderTest = StepsEditor;");
  vm.runInNewContext(source, { window: editorWindow });
  const editor = Object.create(editorWindow.EditorUnderTest.prototype);
  const button = { disabled: false };
  editor.viewOpts = { layout: csv.LAYOUT, readOnly: false };
  editor.errEl = { hidden: true, textContent: "" };
  editor.dialog = { querySelector: () => button };
  editor._flushEdit = async () => {};
  editor.view = { getDoc() { throw new Error("表格行列结构已改变"); } };
  let submissions = 0;
  editor.onSave = async () => { submissions++; };
  await editor._save();
  assert.equal(submissions, 0);
  assert.equal(editor.errEl.hidden, false);
  assert.equal(editor.errEl.textContent, "表格行列结构已改变");
  assert.equal(button.disabled, false);
});
