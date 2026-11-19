const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");

// Comments are stripped so selector lookups only see real rules.
const css = fs
  .readFileSync(path.join(__dirname, "../../app/static/css/design-system.css"), "utf8")
  .replace(/\/\*[\s\S]*?\*\//g, "");

// Returns the body of the first flat rule whose selector list has an entry
// matching `selector` (a regex source), or null when no such rule exists.
function bodyFor(source, selector) {
  const wanted = new RegExp(`^(?:${selector})$`);
  const rule = /([^{}]+)\{([^{}]*)\}/g;
  let match;
  while ((match = rule.exec(source))) {
    const selectors = match[1].split(",").map((entry) => entry.trim());
    if (selectors.some((entry) => wanted.test(entry))) return match[2];
  }
  return null;
}

// Returns the body of the first `@media (<query>)` block, brace-balanced, or
// null when the query is absent.
function mediaBlock(source, query) {
  const at = source.search(new RegExp(`@media\\s*\\(\\s*${query}\\s*\\)\\s*\\{`));
  if (at === -1) return null;
  const open = source.indexOf("{", at);
  let depth = 0;
  for (let i = open; i < source.length; i += 1) {
    if (source[i] === "{") depth += 1;
    else if (source[i] === "}") {
      depth -= 1;
      if (depth === 0) return source.slice(open + 1, i);
    }
  }
  return null;
}

test("the card action overlay is revealed by keyboard focus and mouse hover", () => {
  assert.ok(bodyFor(css, "\\.card:hover\\s+\\.card-acts"), "expected a .card:hover .card-acts rule");

  const focus = bodyFor(css, "\\.card:focus-within\\s+\\.card-acts");
  assert.ok(focus, "expected a .card:focus-within .card-acts rule");
  assert.match(focus, /\bopacity:\s*1\s*;/);
  assert.match(focus, /\btransform:\s*none\s*;/);
});

test("the table-row action overlay is revealed by keyboard focus and mouse hover", () => {
  assert.ok(bodyFor(css, "\\.trow:hover\\s+\\.tacts"), "expected a .trow:hover .tacts rule");

  const focus = bodyFor(css, "\\.trow:focus-within\\s+\\.tacts");
  assert.ok(focus, "expected a .trow:focus-within .tacts rule");
  assert.match(focus, /\bopacity:\s*1\s*;/);
});

test("devices without hover show both action overlays at rest", () => {
  const block = mediaBlock(css, "hover:\\s*none");
  assert.ok(block, "expected a @media (hover: none) block");

  const card = bodyFor(block, "\\.card\\s+\\.card-acts");
  assert.ok(card, "expected a .card .card-acts rule inside @media (hover: none)");
  assert.match(card, /\bopacity:\s*1\s*;/);
  assert.match(card, /\btransform:\s*none\s*;/);

  const row = bodyFor(block, "\\.trow\\s+\\.tacts");
  assert.ok(row, "expected a .trow .tacts rule inside @media (hover: none)");
  assert.match(row, /\bopacity:\s*1\s*;/);
});