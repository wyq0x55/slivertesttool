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

test("steps_editor.js: every runtime <th> declares column scope", () => {
  const source = read(STEPS_EDITOR);
  const tags = headerTags(source);
  const offenders = missingScope(source, STEPS_EDITOR);

  // Guard the scan itself: the signal and step tables must both be present.
  assert.equal(tags.length, 8, `expected 8 header cells in steps_editor.js, found ${tags.length}`);
  assert.deepEqual(offenders, [], `steps_editor.js headers without scope="col": ${offenders.join(", ")}`);

  // The trailing delete-column header is empty on purpose; it must still be a
  // column header, not fall back to being read as a row header.
  const emptyScoped = source.match(/<th scope="col"><\/th>/g) || [];
  assert.ok(emptyScoped.length >= 2,
    `expected the two empty action headers to carry scope="col", found ${emptyScoped.length}`);
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