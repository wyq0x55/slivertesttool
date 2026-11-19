const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");

test("the shared reduced-motion rule limits animations to one short iteration", () => {
  const css = fs.readFileSync(path.join(__dirname, "../../app/static/css/design-system.css"), "utf8");
  const rule = css.match(/@media\s*\(\s*prefers-reduced-motion:\s*reduce\s*\)\s*\{\s*\*\s*\{([^}]*)\}/);

  assert.ok(rule, "expected a shared reduced-motion rule");
  const declarations = rule[1];
  assert.match(declarations, /\banimation-iteration-count:\s*1\s*!important\s*;/);
  assert.match(declarations, /\banimation-duration:\s*\.01ms\s*!important\s*;/);
  assert.match(declarations, /\btransition-duration:\s*\.01ms\s*!important\s*;/);
});
