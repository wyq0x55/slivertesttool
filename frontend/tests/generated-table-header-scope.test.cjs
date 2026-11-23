// Guides `accessibility` and `responsive-table`: every header cell the step
// editor and the UT task detail page build at runtime must declare
// scope="col". Without it a screen reader has to guess which column a body
// cell belongs to (1.3.1 Info and Relationships, Level A).
//
// steps_editor.js is hand-written. ut_tasks.js is generated from
// frontend/src/lanmatrix/ut_tasks.ts (`npm run build:lanmatrix`), so the
// assertions run on both and a stale build -- one that still emits bare
// `<th>` -- fails here instead of shipping.
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");

const root = path.resolve(__dirname, "../..");
const STEPS_EDITOR = "app/static/js/lanmatrix/steps_editor.js";
const UT_TASKS_TS = "frontend/src/lanmatrix/ut_tasks.ts";
const UT_TASKS_JS = "app/static/js/lanmatrix/ut_tasks.js";

const read = (rel) => fs.readFileSync(path.join(root, rel), "utf8");
const TH = /<th\b[^>]*>/g;

// The four header-building sites. Each marker is unique in both the source and
// the generated file, and the slice ends at the site's `</thead>`.
const SITES = [
  { name: "stub rounds", marker: 'ut-issue-table"><thead><tr>', headers: 4 },
  { name: "stage timing", marker: 'aria-label="步骤用时"><thead><tr>', headers: 4 },
  { name: "line coverage", marker: 'class="ut-cov-lines"', headers: 6 },
  { name: "detail keys", marker: "'\"><thead><tr>' + keys.map", headers: 1 },
];

function headerTags(source) {
  return [...source.matchAll(TH)].map((match) => match[0]);
}

function missingScope(source) {
  return headerTags(source).filter((tag) => !/\bscope="col"/.test(tag));
}

function siteSlice(source, marker) {
  const at = source.indexOf(marker);
  assert.ok(at >= 0, `header site not found (marker ${JSON.stringify(marker)})`);
  const end = source.indexOf("</thead>", at);
  assert.ok(end > at, `header site ${JSON.stringify(marker)} must close its <thead>`);
  return source.slice(at, end);
}

// The TypeScript source spells non-ASCII header text as \uXXXX escapes; decode
// them so the generated file, which contains the real characters, compares
// equal.
function decodeEscapes(text) {
  return text.replace(/\\u([0-9a-fA-F]{4})/g, (_, hex) =>
    String.fromCharCode(parseInt(hex, 16)));
}

function headerTexts(slice) {
  return [...slice.matchAll(TH)].map((match) => {
    const start = match.index + match[0].length;
    const end = slice.indexOf("</th>", start);
    // Source concatenation adds spaces and line breaks around interpolations;
    // collapse them so only the header text itself is compared.
    return decodeEscapes(slice.slice(start, end)).replace(/\s+/g, " ").trim();
  });
}

// The two steps_editor.js header sites. `_signalsTable` builds its <thead>
// inline; `_stepsTable` assembles a `head` string that its <thead> later
// interpolates, so that slice runs from `const head =` to the row builder.
const STEP_SITES = [
  { name: "signal table", start: 'class="lm-table lm-se-table"', end: "</thead>", headers: 3 },
  { name: "steps table", start: "const head =", end: "const rows = this.doc.steps", headers: 5 },
];

function spanSlice(source, start, end) {
  const at = source.indexOf(start);
  assert.ok(at >= 0, `header site not found (marker ${JSON.stringify(start)})`);
  const stop = source.indexOf(end, at);
  assert.ok(stop > at, `header site ${JSON.stringify(start)} must close before ${JSON.stringify(end)}`);
  return source.slice(at, stop);
}

// Split each <th> into its open tag and its inner markup, so a test can ask
// both "does the cell declare scope?" and "does it name the column?".
function headerCells(slice) {
  return [...slice.matchAll(TH)].map((match) => {
    const start = match.index + match[0].length;
    return { open: match[0], inner: slice.slice(start, slice.indexOf("</th>", start)) };
  });
}

// The text a screen reader would get: markup stripped (so a visually hidden
// label counts as a name), nbsp and whitespace collapsed.
function textOf(inner) {
  return inner.replace(/<[^>]*>/g, "").replace(/&nbsp;/g, " ").replace(/\s+/g, " ").trim();
}

test("steps_editor.js: every runtime <th> declares column scope", () => {
  const source = read(STEPS_EDITOR);
  const tags = headerTags(source);
  const offenders = missingScope(source, STEPS_EDITOR);

  // Guard the scan itself: the signal and step tables must both be present.
  assert.equal(tags.length, 8, `expected 8 header cells in steps_editor.js, found ${tags.length}`);
  assert.deepEqual(offenders, [], `steps_editor.js headers without scope="col": ${offenders.join(", ")}`);

  // The trailing delete-column header holds only buttons, so it is visually
  // blank by design — but a blank cell names nothing, and a screen reader then
  // announces the column as unnamed. It must keep scope="col" and carry a
  // visually hidden label (1.3.1 Info and Relationships, Level A).
  for (const site of STEP_SITES) {
    const cells = headerCells(spanSlice(source, site.start, site.end));
    assert.equal(cells.length, site.headers,
      `${site.name}: expected ${site.headers} header cells, found ${cells.length}`);
    const action = cells[cells.length - 1];
    assert.match(action.open, /\bscope="col"/,
      `${site.name}: the action header must stay a column header, got ${action.open}`);
    assert.equal(textOf(action.inner), "操作",
      `${site.name}: the action header must name the column for screen readers`);
    assert.match(action.inner, /class="lm-sr-only"/,
      `${site.name}: the action header name must stay visually hidden`);
  }
});

test("steps_editor.js: no header cell is both silent and unnamed", () => {
  // Guard the whole header row, not just the delete column: any cell that
  // renders no text and carries no aria/title name is announced as an unnamed
  // column, which a table reader cannot identify.
  const source = read(STEPS_EDITOR);
  for (const site of STEP_SITES) {
    for (const cell of headerCells(spanSlice(source, site.start, site.end))) {
      const named = textOf(cell.inner) !== "" ||
        /\baria-label(?:ledby)?=/.test(cell.open) || /\btitle=/.test(cell.open);
      assert.ok(named, `${site.name}: header cell ${cell.open} is empty and unnamed`);
    }
  }
});

test("ut_tasks.ts: all four runtime header sites declare column scope", () => {
  const source = read(UT_TASKS_TS);
  assert.deepEqual(missingScope(source, UT_TASKS_TS), [],
    `ut_tasks.ts headers without scope="col": ${missingScope(source, UT_TASKS_TS).join(", ")}`);
  assert.equal(headerTags(source).length, 15, "ut_tasks.ts header-cell count changed; update the scope audit");

  for (const site of SITES) {
    const slice = siteSlice(source, site.marker);
    assert.equal(headerTags(slice).length, site.headers,
      `${site.name}: expected ${site.headers} header cells`);
    assert.deepEqual(missingScope(slice, site.name), [],
      `${site.name}: headers without scope="col": ${missingScope(slice, site.name).join(", ")}`);
  }
});

test("generated ut_tasks.js matches the TypeScript header sites", () => {
  const source = read(UT_TASKS_JS);
  const ts = read(UT_TASKS_TS);

  // A stale build leaves bare `<th>` in the shipped file.
  assert.deepEqual(missingScope(source, UT_TASKS_JS), [],
    `generated ut_tasks.js headers without scope="col" (stale build?): ${missingScope(source, UT_TASKS_JS).join(", ")}`);
  assert.equal(headerTags(source).length, 15, "generated header-cell count changed; rebuild and re-audit");

  for (const site of SITES) {
    const built = siteSlice(source, site.marker);
    const typed = siteSlice(ts, site.marker);
    assert.deepEqual(headerTags(built), headerTags(typed),
      `${site.name}: generated tags diverged from the TypeScript source`);
    assert.deepEqual(headerTexts(built), headerTexts(typed),
      `${site.name}: generated header text diverged from the TypeScript source`);
  }

  // The c1 label is interpolated, so it has no literal text to compare; guard
  // both files keep the interpolation instead of quietly dropping it.
  const coverageBuilt = siteSlice(source, SITES[2].marker);
  const coverageTyped = siteSlice(ts, SITES[2].marker);
  assert.match(coverageTyped, /esc\(c1Label\)/, "ut_tasks.ts must keep the c1 label header");
  assert.match(coverageBuilt, /esc\(c1Label\)/, "generated ut_tasks.js must keep the c1 label header");
});