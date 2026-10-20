const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const window = {};
vm.runInContext(
  fs.readFileSync(path.resolve(__dirname, "../../app/static/js/lanmatrix/ut_testcsv.js"), "utf8"),
  vm.createContext({ window }));
const T = window.LMTestCsv;
const plain = (value) => JSON.parse(JSON.stringify(value));

const CSV = [
  'mod,"p.c/f","f 単体テスト",2,1,,,,CPP,,,"",0',
  '%,"AMSTB_g","g"',
  '#COMMENT,"AMSTB_SrcFile.c/AMSTB_g@CALLCNT_g","p.c/x","p.c/f@@"',
  ";$L$,if ( x == 1 )",
  ";$L$,TRUE",
  ",0,1,1",
  ";$L$,FALSE",
  ',0,"0",0',
  ";$L$,trailing",
].join("\r\n") + "\r\n";

test("an unedited table serialises back to the original CSV", () => {
  const model = T.parse(CSV);
  assert.equal(T.serialize(model, T.toDoc(model)), CSV);
});

test("columns follow the mod counts and comments attach to the next data row", () => {
  const doc = plain(T.toDoc(T.parse(CSV)));
  assert.deepEqual(doc.input_signals, [["AMSTB_g@CALLCNT_g", "AMSTB_SrcFile.c/AMSTB_g@CALLCNT_g"],
                                       ["x", "p.c/x"]]);
  assert.deepEqual(doc.expected_signals, [["f@@", "p.c/f@@"]]);
  assert.equal(doc.steps[0].purpose, "if ( x == 1 )\nTRUE");
  assert.deepEqual(doc.steps[1].inputs, ["0", "0"]);
  assert.deepEqual(doc.steps[1].expecteds, ["0"]);
});

test("edited values and comments are written in TestCsv form; the rest is kept", () => {
  const model = T.parse(CSV);
  const doc = plain(T.toDoc(model));
  doc.steps[1].expecteds = ["1"];
  doc.steps[1].purpose = "FALSE, checked";
  doc.steps.push({ purpose: "", inputs: ["1", null], expecteds: ["2"] });
  assert.equal(T.serialize(model, doc), [
    'mod,"p.c/f","f 単体テスト",2,1,,,,CPP,,,"",0',
    '%,"AMSTB_g","g"',
    '#COMMENT,"AMSTB_SrcFile.c/AMSTB_g@CALLCNT_g","p.c/x","p.c/f@@"',
    ";$L$,if ( x == 1 )",
    ";$L$,TRUE",
    ",0,1,1",
    ";$L$,FALSE, checked",
    ",0,0,1",
    ",1,,2",
    ";$L$,trailing",
  ].join("\r\n") + "\r\n");
});

test("result columns after the outputs survive an edit of the row", () => {
  const model = T.parse("mod,f,t,1,1\r\n#COMMENT,x,ret\r\n,1,2,NG,1ms\r\n");
  const doc = plain(T.toDoc(model));
  doc.steps[0].expecteds = ["3"];
  assert.equal(T.serialize(model, doc), "mod,f,t,1,1\r\n#COMMENT,x,ret\r\n,1,3,NG,1ms\r\n");
});

test("disputed expected values map to their output cell", () => {
  const model = T.parse(CSV);
  const marks = T.marks(model, { issues: [
    { code: "ORACLE_MISMATCH", detail: [{ row: 1, column: "p.c/f@@", expected: "0", actual: 1 },
                                        { row: 0, column: "p.c/unknown" }] },
    { code: "COVERAGE_C0_INCOMPLETE", detail: { c0: 50 } },
  ] });
  assert.deepEqual(plain(marks), [{ step: 1, kind: "expected", index: 0, color: "#f9c9c9" }]);
});

test("text without a #COMMENT header is not a TestCsv", () => {
  assert.equal(T.parse("just,some\r\ncsv\r\n"), null);
});

test("typed values read back from the sheet keep their TestCsv spelling", () => {
  const model = T.parse("mod,f,t,1,1\r\n#COMMENT,x,ret\r\n;$L$,TRUE\r\n,0,1\r\n");
  const doc = plain(T.toDoc(model));
  doc.steps[0].purpose = true;
  doc.steps[0].inputs = [0];
  doc.steps[0].expecteds = [1];
  assert.equal(T.serialize(model, doc), "mod,f,t,1,1\r\n#COMMENT,x,ret\r\n;$L$,TRUE\r\n,0,1\r\n");
});
