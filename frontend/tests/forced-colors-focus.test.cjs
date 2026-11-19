const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");

const CSS_PATH = path.join(__dirname, "../../app/static/css/design-system.css");
const css = fs.readFileSync(CSS_PATH, "utf8");

// System-color keywords that survive Forced Colors Mode. A hard-coded hex is
// replaced by the forced palette, which can leave the ring invisible.
const SYSTEM_COLORS = new Set([
  "Canvas", "CanvasText", "LinkText", "VisitedText", "ActiveText",
  "ButtonFace", "ButtonText", "ButtonBorder", "Field", "FieldText",
  "Highlight", "HighlightText", "SelectedItem", "SelectedItemText",
  "Mark", "MarkText", "GrayText", "AccentColor", "AccentColorText",
]);

// Every control whose native outline is suppressed by design-system.css.
const FOCUS_SELECTORS = [
  ":where(input,select,textarea,button,a,[tabindex]):focus-visible",
  ".search input:focus",
  "input:focus",
  "select:focus",
  "textarea:focus",
  ".stepper:focus-within",
  ".roleselect:focus",
  ".addbar .search input:focus",
  ".selbox:focus",
  ".mcard.selectable:focus-visible",
  ".ipt:focus",
  ".lm-db-sql:focus",
  ".lm-db-row-form .lm-db-field:focus",
  ".lmui-input:focus",
  ".cv-canvas-wrap canvas:focus-visible",
  ".lm-steps-json-body:focus-within",
  ".lm-sbs-editor:focus-within",
];

function braceDepth(source, position) {
  let depth = 0;
  for (let i = 0; i < position; i += 1) {
    if (source[i] === "{") depth += 1;
    else if (source[i] === "}") depth -= 1;
  }
  return depth;
}

function readForcedColorsBlock(source) {
  const start = source.search(/@media\s*\(\s*forced-colors:\s*active\s*\)\s*\{/);
  if (start === -1) return null;

  const open = source.indexOf("{", start);
  let depth = 0;
  for (let i = open; i < source.length; i += 1) {
    if (source[i] === "{") depth += 1;
    else if (source[i] === "}") {
      depth -= 1;
      if (depth === 0) {
        return { index: start, end: i + 1, body: source.slice(open + 1, i) };
      }
    }
  }
  return null;
}

// Split a selector list on commas that sit outside :where() / :is() parens and
// attribute brackets, so `:where(input,select)` stays one selector.
function splitSelectors(prelude) {
  const parts = [];
  let current = "";
  let parens = 0;
  let brackets = 0;
  for (const char of prelude) {
    if (char === "(") parens += 1;
    else if (char === ")") parens -= 1;
    else if (char === "[") brackets += 1;
    else if (char === "]") brackets -= 1;

    if (char === "," && parens === 0 && brackets === 0) {
      parts.push(current);
      current = "";
    } else {
      current += char;
    }
  }
  parts.push(current);
  return parts.map((selector) => selector.trim().replace(/\s+/g, " ")).filter(Boolean);
}

function parseRules(body) {
  const source = body.replace(/\/\*[\s\S]*?\*\//g, "");
  const rules = [];
  let ruleStart = 0;
  let open = -1;
  let depth = 0;

  for (let i = 0; i < source.length; i += 1) {
    const char = source[i];
    if (char === "{") {
      if (depth === 0) {
        open = i;
      }
      depth += 1;
    } else if (char === "}" && depth > 0) {
      depth -= 1;
      if (depth === 0) {
        rules.push({
          selectors: splitSelectors(source.slice(ruleStart, open)),
          declarations: source.slice(open + 1, i),
        });
        ruleStart = i + 1;
        open = -1;
      }
    }
  }
  return rules;
}

function ruleFor(rules, selector) {
  return rules.find((rule) => rule.selectors.includes(selector));
}

function assertOutlineRule(rule, selector) {
  assert.ok(rule, `expected an exact selector match for ${selector} in the forced-colors block`);

  const outline = rule.declarations.match(/outline\s*:\s*(\d+(?:\.\d+)?)px\s+solid\s+([A-Za-z]+)\s*;/);
  assert.ok(
    outline,
    `expected ${selector} to get \`outline: <width> solid <system-color>\`; a box-shadow ring alone does not survive forced colors`,
  );
  assert.ok(Number(outline[1]) >= 2, `expected ${selector} to get a focus ring at least 2px wide`);
  assert.ok(SYSTEM_COLORS.has(outline[2]), `expected a system color for ${selector}, got ${outline[2]}`);
  assert.doesNotMatch(outline[0], /#[0-9a-f]{3,8}/i, "a hard-coded color does not survive forced-colors mode");
  assert.match(
    rule.declarations,
    /outline-offset\s*:\s*-?\d+px\s*;/,
    `expected ${selector} to set outline-offset so the ring clears the control border`,
  );
}

test("forced-colors mode restores an outline-based focus indicator", () => {
  const block = readForcedColorsBlock(css);
  assert.ok(block, "expected a @media (forced-colors: active) block in design-system.css");

  // The block must sit at the top level of the stylesheet and come after the
  // focus rules it repairs, so its equal-specificity declarations win.
  assert.equal(braceDepth(css, block.index), 0, "expected the forced-colors block at the top level");
  assert.ok(
    block.index > css.indexOf(":where(input,select,textarea,button,a,[tabindex]):focus-visible"),
    "expected the forced-colors block after the shared focus rule",
  );

  const rules = parseRules(block.body);
  for (const selector of FOCUS_SELECTORS) {
    assertOutlineRule(ruleFor(rules, selector), selector);
  }

  // The step buttons keep one ring around the group; their own ring must not
  // stack a second outline inside the group ring.
  const stepperInput = ruleFor(rules, ".stepper input:focus");
  assert.ok(stepperInput, "expected the stepper input outline suppression in the forced-colors block");
  assert.match(stepperInput.declarations, /outline\s*:\s*none\s*;/);
});

test("selector matching is exact, not substring based", () => {
  const rules = parseRules(
    ".addbar .search input:focus{ outline:3px solid Highlight; outline-offset:2px; }",
  );
  assert.equal(
    ruleFor(rules, ".search input:focus"),
    undefined,
    "`.search input:focus` must not be satisfied by `.addbar .search input:focus`",
  );
});

test("without the block no forced-colors outline remains", () => {
  const block = readForcedColorsBlock(css);
  assert.ok(block, "precondition: the block is present");

  const withoutBlock = css.slice(0, block.index) + css.slice(block.end);
  assert.equal(readForcedColorsBlock(withoutBlock), null, "expected the block to be removable");

  // The base stylesheet has its own (box-shadow-only) focus rules, so selector
  // presence alone proves nothing. What must disappear with the block is the
  // forced-colors repair itself: a system-color outline on a focus selector.
  assert.doesNotMatch(
    withoutBlock,
    /@media\s*\(\s*forced-colors:\s*active\s*\)/,
    "expected no forced-colors media query once the block is removed",
  );
  const systemOutline = new RegExp(
    `outline\\s*:\\s*\\d+(?:\\.\\d+)?px\\s+solid\\s+(?:${[...SYSTEM_COLORS].join("|")})\\b`,
  );
  assert.doesNotMatch(
    withoutBlock.replace(/\/\*[\s\S]*?\*\//g, ""),
    systemOutline,
    "expected no system-color outline outside the forced-colors block",
  );
});