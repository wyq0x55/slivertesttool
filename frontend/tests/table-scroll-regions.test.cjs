// Guide `responsive-table`: a container that scrolls horizontally is not a tab
// stop, so a keyboard user cannot scroll it. Each `<table>` wrapper gets
// tabindex="0", role="region" and an aria-labelledby that names it after the
// nearest existing heading. No key handler is added: a focused scroll container
// already scrolls with the arrow keys.
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");

const templates = path.resolve(__dirname, "../../app/templates/lanmatrix");
const read = (file) => fs.readFileSync(path.join(templates, file), "utf8");

// file -> how many table scroll wrappers it holds (admin, home and ut_tasks
// have two tables each, so each of them needs two different heading names).
// admin.html and project_tasks.html were held back from the first pass while
// other branches wrote them; they are covered here now that the writers landed.
const REGION_FILES = {
  "admin.html": 2,
  "ai_drafts.html": 1,
  "audit.html": 1,
  "dashboard.html": 1,
  "fields.html": 1,
  "home.html": 2,
  "project_tasks.html": 1,
  "tokens.html": 1,
  "trash.html": 1,
  "ut_pipeline.html": 1,
  "ut_tasks.html": 2,
};

const escapeRe = (s) => s.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
const ID_ATTR = (id) => new RegExp(`(?:^|\\s)id="${escapeRe(id)}"`);
const HEADING_WITH_ID = (id) => new RegExp(`<h[1-6]\\b[^>]*\\sid="${escapeRe(id)}"`);

// Every `<div style="overflow-x:auto">` that directly wraps a table, as its
// opening tag plus the text it encloses.
function tableScrollWrappers(html) {
  const wrappers = [];
  for (const match of html.matchAll(/<div\b[^>]*style="overflow-x:auto"[^>]*>/g)) {
    const openTag = match[0];
    const close = html.indexOf("</div>", match.index);
    assert.notEqual(close, -1, `unclosed scroll wrapper at offset ${match.index}`);
    const inner = html.slice(match.index + openTag.length, close);
    assert.match(inner, /^\s*<table\b/, `scroll wrapper at offset ${match.index} must wrap a table`);
    wrappers.push({ openTag, offset: match.index });
  }
  return wrappers;
}

test("each scrolling table is a named, keyboard-scrollable region", () => {
  for (const [file, expected] of Object.entries(REGION_FILES)) {
    const html = read(file);
    const wrappers = tableScrollWrappers(html);
    assert.equal(wrappers.length, expected, `${file} should hold ${expected} table scroll wrappers`);

    const names = [];
    for (const { openTag, offset } of wrappers) {
      const where = `${file} scroll wrapper at offset ${offset}`;
      assert.match(openTag, /(?:^|\s)tabindex="0"(?=\s|>)/, `${where} needs tabindex="0" so it can be focused`);
      assert.match(openTag, /(?:^|\s)role="region"(?=\s|>)/, `${where} needs role="region"`);

      const labelled = openTag.match(/(?:^|\s)aria-labelledby="([^"]*)"/);
      assert.ok(labelled, `${where} needs aria-labelledby`);
      const ids = labelled[1].trim().split(/\s+/).filter(Boolean);
      assert.equal(ids.length, 1, `${where} must be named by exactly one element`);

      const [id] = ids;
      assert.match(html, ID_ATTR(id), `${where} points at "${id}", which no element in ${file} defines`);
      assert.match(html, HEADING_WITH_ID(id), `${where} must be named by a heading carrying id="${id}"`);
      names.push(id);
    }

    assert.equal(
      new Set(names).size, names.length,
      `${file} must not point two scroll regions at the same heading (got ${names.join(", ")})`,
    );
  }
});
