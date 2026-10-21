const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const source = fs.readFileSync(
  path.resolve(__dirname, "../../app/static/js/lanmatrix/ut_testcsv.js"), "utf8");

function lib() {
  const window = {};
  vm.runInContext(source, vm.createContext({ window }));
  return window.LMTestCsv;
}

// Mirrors the generator's layout: stub call counters first, then the ROM
// constants and globals a decision reads, then outputs.
const CSV = 'mod,"a.c/f","f",6,1\r\n' +
  '#COMMENT,"AMSTB_SrcFile.c/AMSTB_g@CALLCNT_g","cu1_valid","a.c/xs[0].byte",' +
  '"a.c/xs[1].mode","a.c/other","a.c/cu1_valid_old","a.c/f@@"\r\n' +
  ";$L$,if ( ((u1)1) == cu1_valid )\r\n;$L$,TRUE\r\n,0,1,0,0,0,0,1\r\n" +
  ";$L$,FALSE\r\n,0,0,0,0,0,0,0\r\n" +
  ";$L$,組合せ(FALSE(1))\r\n,0,2,0,0,0,0,0\r\n" +
  ";$L$,else if ( (((u1)0) == xs[1].mode) && (other == 2U) )\r\n,0,0,0,1,2,0,1\r\n" +
  ";$L$,T&&T => T\r\n,0,0,0,1,2,0,1\r\n";

function cellsByStep(marks) {
  const out = {};
  for (const m of marks) (out[m.step] = out[m.step] || []).push(m.index);
  return out;
}

test("a decision marks the inputs it reads until the next decision", () => {
  const T = lib();
  const marks = T.branchMarks(T.parse(CSV));
  assert.ok(marks.every((m) => m.kind === "input" && m.color === "#fff0b3"));
  assert.deepEqual(cellsByStep(marks), {
    0: [1], 1: [1], 2: [1],     // if ... cu1_valid, its FALSE and MC/DC rows
    3: [3, 4], 4: [3, 4],       // compound else-if: xs[1].mode and other
  });
});

test("casts, keywords, similar names and stubs are not mistaken for the variable", () => {
  const T = lib();
  const model = T.parse(CSV);
  const indexes = new Set(T.branchMarks(model).map((m) => m.index));
  assert.ok(!indexes.has(0), "the AMSTB_ stub counter is not read by the decision");
  assert.ok(!indexes.has(5), "cu1_valid_old only shares a prefix");
  assert.ok(!indexes.has(2), "xs[0].byte is another element than xs[1].mode");
});

test("rows before any decision, and a CSV without decisions, get no marks", () => {
  const T = lib();
  const plain = 'mod,"a.c/f","f",1,1\r\n#COMMENT,"a.c/x","a.c/f@@"\r\n;$L$,境界値\r\n,0,1\r\n';
  assert.equal(T.branchMarks(T.parse(plain)).length, 0);  // array from the vm realm
});
