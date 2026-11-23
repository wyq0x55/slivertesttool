// Guide `responsive-table`: a container that scrolls horizontally is not a tab
// stop, so a keyboard user cannot scroll it. The dashboard's two version-
// compare tables were the last wide tables missing the wrapper. Each gets
// tabindex="0", role="region" and a name taken from an existing heading: the
// results table is named by its section's <h3 id="lm-vc-title">, the history
// table by its existing <h4 id="lm-vc-history-title">. No key handler is
// added: a focused scroll container already scrolls with the arrow keys.
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");

const html = fs.readFileSync(
  path.resolve(__dirname, "../../app/templates/lanmatrix/dashboard.html"),
  "utf8",
);

const escapeRe = (s) => s.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
const HAS_ATTR = (tag, name) => new RegExp(`(?:^|\\s)${escapeRe(name)}="([^"]*)"`).exec(tag);

// The wrapper that directly contains the table: the only tag between the
// table's opening tag and its parent is a `<div>` opening tag, and the table's
// closing tag is followed by that div's closing tag. Anchoring on the
// surrounding text (rather than indentation) keeps cosmetic whitespace from
// fooling the check.
function directWrapper(scope, tableOpenIndex, tableCloseIndex) {
  const prefix = scope.slice(0, tableOpenIndex).trimEnd();
  const opening = prefix.match(/<div\b[^>]*>$/);
  assert.ok(
    opening,
    `table at offset ${tableOpenIndex} must be a direct child of a <div>, found: ${prefix.slice(-80)}`,
  );
  const suffix = scope.slice(tableCloseIndex);
  assert.match(suffix, /^<\/table>\s*<\/div>/, `the wrapper of the table at offset ${tableOpenIndex} must close right after it`);
  return opening[0];
}

function assertScrollAttributes(tag, where) {
  const style = HAS_ATTR(tag, "style");
  assert.ok(style, `${where} needs a style attribute`);
  assert.equal(style[1].replace(/\s+/g, ""), "overflow-x:auto", `${where} must scroll horizontally`);
  assert.match(tag, /(?:^|\s)tabindex="0"(?=\s|>)/, `${where} needs tabindex="0" so it can be focused`);
  const role = HAS_ATTR(tag, "role");
  assert.ok(role, `${where} needs role="region"`);
  assert.equal(role[1], "region", `${where} needs role="region"`);
}

test("version-compare results table is a named, keyboard-scrollable region", () => {
  const open = html.match(/<table\b[^>]*\bid="lm-vc-table"[^>]*>/);
  assert.ok(open, "dashboard.html must define the version-compare results table");
  const tableOpen = open.index;
  const tableClose = html.indexOf("</table>", tableOpen);
  assert.notEqual(tableClose, -1, "the version-compare results table must be closed");

  const wrapper = directWrapper(html, tableOpen, tableClose);
  assertScrollAttributes(wrapper, "#lm-vc-table wrapper");

  const labelled = HAS_ATTR(wrapper, "aria-labelledby");
  assert.ok(labelled, "#lm-vc-table wrapper needs an accessible name");
  assert.equal(labelled[1].trim(), "lm-vc-title", "#lm-vc-table wrapper must be named by the section heading");
  assert.equal(HAS_ATTR(wrapper, "aria-label"), null, "#lm-vc-table is named by its heading, not a literal label");

  const heading = /<h[1-6]\b[^>]*\sid="lm-vc-title"[^>]*>\s*版本结果对比/.exec(html);
  assert.ok(heading, 'aria-labelledby must point at the existing <h3>版本结果对比</h3>');
});

test("version-compare history table is a named, keyboard-scrollable region", () => {
  const section = html.match(/<section\b[^>]*\sid="lm-vc-history"[^>]*>/);
  assert.ok(section, "dashboard.html must define the version-compare history section");
  const sectionStart = section.index + section[0].length;
  const sectionEnd = html.indexOf("</section>", sectionStart);
  assert.notEqual(sectionEnd, -1, "the history section must be closed");
  const scope = html.slice(sectionStart, sectionEnd);

  const open = scope.match(/<table\b[^>]*>/);
  assert.ok(open, "the history section must contain a table");
  const tableClose = scope.indexOf("</table>", open.index);
  assert.notEqual(tableClose, -1, "the history table must be closed");

  const wrapper = directWrapper(scope, open.index, tableClose);
  assertScrollAttributes(wrapper, "#lm-vc-history table wrapper");

  const labelled = HAS_ATTR(wrapper, "aria-labelledby");
  assert.ok(labelled, "#lm-vc-history table wrapper needs an accessible name");
  assert.equal(labelled[1].trim(), "lm-vc-history-title", "#lm-vc-history table must be named by its heading");
  assert.equal(HAS_ATTR(wrapper, "aria-label"), null, "#lm-vc-history is named by aria-labelledby only");

  const heading = /<h[1-6]\b[^>]*\sid="lm-vc-history-title"/.exec(scope);
  assert.ok(heading, "aria-labelledby must point at a heading inside the history section");
});

test("dashboard.html holds exactly the three expected scroll regions", () => {
  const regions = html.match(/(?:^|\s)role="region"(?=\s|>)/g) || [];
  assert.equal(regions.length, 3, "dashboard.html should have three role=\"region\" table wrappers");
});