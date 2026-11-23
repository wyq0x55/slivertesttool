// Guards the curve panel's data tables: every header cell in the 事件列表
// (loadEvents) and 判定检查点 (renderChecks) tables must carry scope="col" so a
// screen-reader user can tell which column each cell describes. The raw-data
// table (loadRaw) is a separate change and is deliberately not asserted here.
//
// Both real functions are executed in a node:vm sandbox with a minimal DOM
// shim; the header markup they assign is parsed back into cells so the
// assertions cover what the browser would receive, not only source text.
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const root = path.resolve(__dirname, "../..");
const source = fs.readFileSync(
  path.join(root, "app/static/js/lanmatrix/task_curves.js"), "utf8");

const EVENTS_TEXTS = ["时间 (s)", "记录", "变量", "从", "到", "步骤"];
const CHECKS_TEXTS = ["步骤", "结果", "时刻 (s)", "变量", "期望", "实测", "确认时机"];

// ------------------------------------------------------------- extraction

// Slice one top-level function out of the controller IIFE. Functions are
// indented by two spaces and closed by a line holding exactly "  }".
function extractFunction(name) {
  const marker = `function ${name}(`;
  const at = source.indexOf(marker);
  assert.ok(at >= 0, `task_curves.js must still define ${name}()`);
  const start = source.slice(at - 6, at) === "async " ? at - 6 : at;
  const end = source.indexOf("\n  }", at);
  assert.ok(end > at, `${name}() must end on its own top-level line`);
  return source.slice(start, end + 4);
}

// ------------------------------------------------------------- DOM shim

function makeSandbox(extra) {
  const elements = new Map();
  function makeElement(id) {
    return {
      id,
      innerHTML: "",
      textContent: "",
      checked: false,
      disabled: false,
      children: [],
      append(child) { this.children.push(child); },
      addEventListener() {},
    };
  }
  const document = {
    getElementById(id) {
      if (!elements.has(id)) elements.set(id, makeElement(id));
      return elements.get(id);
    },
    createElement(tag) {
      return {
        tagName: tag,
        innerHTML: "",
        textContent: "",
        tabIndex: 0,
        className: "",
        children: [],
        append(child) { this.children.push(child); },
        addEventListener() {},
      };
    },
  };
  const sandbox = Object.assign({ document, console }, extra);
  sandbox.$ = id => sandbox.document.getElementById(id);
  return { sandbox, elements };
}

function executeFunction(name, extra) {
  const { sandbox, elements } = makeSandbox(extra);
  const fn = extractFunction(name);
  vm.runInNewContext(`${fn}\n;globalThis.__run = ${name};`, sandbox);
  return { run: sandbox.__run, elements, sandbox };
}

// ------------------------------------------------------------- parsing

function parseAttrs(raw) {
  const attrs = {};
  raw.replace(/([^\s=/]+)(?:\s*=\s*"([^"]*)")?/g, (_, key, value) => {
    attrs[key] = value === undefined ? "" : value;
    return "";
  });
  return attrs;
}

function parseHeader(markup) {
  const thead = markup.match(/<thead>([\s\S]*?)<\/thead>/i);
  assert.ok(thead, "the table must start with a <thead>");
  const row = thead[1].match(/<tr>([\s\S]*?)<\/tr>/i);
  assert.ok(row, "the <thead> must hold one header <tr>");
  const leftovers = row[1].replace(/<th\b[^>]*>[\s\S]*?<\/th>/gi, "").trim();
  assert.equal(leftovers, "", "the header row must contain only <th> cells");
  const cells = [...row[1].matchAll(/<th\b([^>]*)>([\s\S]*?)<\/th>/gi)].map(m => ({
    attrs: parseAttrs(m[1]),
    text: m[2].replace(/<[^>]*>/g, "").trim(),
  }));
  assert.ok(cells.length > 0, "the header row must contain at least one <th>");
  return cells;
}

function assertScopedHeader(cells, texts, table) {
  assert.deepEqual(cells.map(cell => cell.text), texts,
    `${table} header cells keep their text and order`);
  cells.forEach((cell, index) => {
    assert.deepEqual(cell.attrs, { scope: "col" },
      `${table} column ${index + 1} ("${cell.text}") must declare only scope="col"`);
  });
}

// ------------------------------------------------------------- tests

test("事件列表 header cells are scoped columns", async () => {
  const { run, elements } = executeFunction("loadEvents", {
    params: extra => ({ ...extra }),
    lanes: [],
    view: { start: 0, end: 1 },
    api: async () => ({ events: [], total: 0 }),
    R: { fmtValue: value => String(value) },
    esc: value => String(value === undefined || value === null ? "" : value),
    display: column => column.name,
    valueLabel: value => String(value),
    columnById: () => ({ name: "" }),
    stepAt: () => null,
    ensureLanes() {},
    setCursor() {},
    ensureVisible() {},
    status() {},
    eventsOffset: 0,
    density: "ready",
    loadDensity() { throw new Error("density is already loaded"); },
  });
  await run();

  const table = elements.get("cv-events");
  assertScopedHeader(parseHeader(table.innerHTML), EVENTS_TEXTS, "事件列表");
  assert.equal(table.children.length, 1, "the events body is still appended once");
  assert.equal(table.children[0].tagName, "tbody");
  assert.equal(elements.get("cv-events-info").textContent, "没有跳变",
    "the empty-events message is unchanged");
});

test("判定检查点 header cells are scoped columns", () => {
  const { run, elements } = executeFunction("renderChecks", {
    ctx: { judge: { steps: [] } },
    columnByName: () => null,
    esc: value => String(value === undefined || value === null ? "" : value),
    R: { fmtValue: value => String(value) },
    ensureLanes() {},
    setCursor() {},
    ensureVisible() {},
  });
  run();

  const table = elements.get("cv-checks");
  assertScopedHeader(parseHeader(table.innerHTML), CHECKS_TEXTS, "判定检查点");
  assert.equal(table.children.length, 1, "the checks body is still appended once");
  assert.match(table.children[0].innerHTML, /colspan="7"/,
    "the empty-checks row still spans all seven columns");
  assert.match(table.children[0].innerHTML, /没有判定检查点/,
    "the empty-checks message is unchanged");
});

test("判定检查点 pass/fail glyphs are hidden and carry text alternatives", () => {
  const { run, elements } = executeFunction("renderChecks", {
    ctx: {
      judge: {
        steps: [{
          step: 3,
          checks: [
            { ok: true, at: 1.5, var: null, name: "", expected: "A", observed: "A", timing: "立即" },
            { ok: false, at: 2.5, var: null, name: "", expected: "A", observed: "B", timing: "立即" },
          ],
        }],
      },
    },
    columnByName: () => null,
    esc: value => String(value === undefined || value === null ? "" : value),
    R: { fmtValue: value => String(value) },
  });
  run();

  const table = elements.get("cv-checks");
  const rows = table.children[0].children;
  assert.equal(rows.length, 2, "both check rows are rendered");

  function resultCell(row) {
    const cells = [...row.innerHTML.matchAll(/<td\b([^>]*)>([\s\S]*?)<\/td>/gi)];
    assert.equal(cells.length, 7, "each check row keeps seven cells");
    return cells[1][2];
  }

  const pass = resultCell(rows[0]);
  assert.match(pass, /<span class="jbadge pass" aria-hidden="true">✓<\/span>/,
    "the pass glyph keeps its visual class and is hidden from assistive tech");
  assert.match(pass, /<span class="lm-sr-only">通过<\/span>/,
    "the pass result still exposes 通过 to assistive tech");
  assert.doesNotMatch(pass, /class="lm-sr-only"[^>]*aria-hidden/,
    "the 通过 text alternative is not itself hidden");

  const fail = resultCell(rows[1]);
  assert.match(fail, /<span class="jbadge fail" aria-hidden="true">✕<\/span>/,
    "the fail glyph keeps its visual class and is hidden from assistive tech");
  assert.match(fail, /<span class="lm-sr-only">未通过<\/span>/,
    "the fail result still exposes 未通过 to assistive tech");
  assert.doesNotMatch(fail, /class="lm-sr-only"[^>]*aria-hidden/,
    "the 未通过 text alternative is not itself hidden");

  // Removing the hidden glyph leaves exactly the words a screen reader gets.
  const spoken = pass
    .replace(/<span\b[^>]*aria-hidden="true"[^>]*>[\s\S]*?<\/span>/gi, "")
    .replace(/<[^>]*>/g, "")
    .trim();
  assert.equal(spoken, "通过", "assistive tech reads the pass result as text, not a glyph");
});