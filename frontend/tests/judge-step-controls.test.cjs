// Guards the judge-result step rows after replacing <div role="button"> with
// native sibling buttons. The disclosure and the "locate in curve" action are
// separate controls, so the tests execute the real render and wiring helpers
// through a minimal DOM shim instead of asserting source text alone.
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const root = path.resolve(__dirname, "../..");
const scriptPath = path.join(root, "app/static/js/lanmatrix/project_tasks.js");
const designCssPath = path.join(root, "app/static/css/design-system.css");
const curvesCssPath = path.join(root, "app/static/css/lanmatrix-curves.css");
const source = fs.readFileSync(scriptPath, "utf8");
const designCss = fs.readFileSync(designCssPath, "utf8").replace(/\/\*[\s\S]*?\*\//g, "");
const curvesCss = fs.readFileSync(curvesCssPath, "utf8").replace(/\/\*[\s\S]*?\*\//g, "");

// Source slice of a top-level `function name(...) { ... }` inside the IIFE.
// The extracted helpers contain balanced braces only (template `${...}` pairs
// and regex literals included), so plain depth counting is enough here.
function functionSource(text, name) {
  const start = text.indexOf(`  function ${name}(`);
  assert.notEqual(start, -1, `${name} should exist`);
  const open = text.indexOf("{", start);
  let depth = 0;
  for (let i = open; i < text.length; i += 1) {
    if (text[i] === "{") depth += 1;
    else if (text[i] === "}") {
      depth -= 1;
      if (depth === 0) return text.slice(start, i + 1);
    }
  }
  assert.fail(`${name} body is not closed`);
}

// Runs the production helpers that build one step and wire its two buttons.
const helperNames = [
  "esc", "stripLogPrefix", "bodyHtml", "stepBadge", "judgeTitle",
  "judgeStepHtml", "wireJudgeStep",
];
const dispatched = [];
const judge = vm.runInNewContext(
  `${helperNames.map((name) => functionSource(source, name)).join("\n")}\n` +
    `;({ ${helperNames.join(", ")} });`,
  {
    document: { dispatchEvent(event) { dispatched.push(event); return true; } },
    CustomEvent: class { constructor(type, options) { this.type = type; this.detail = options.detail; } },
  },
);

function box(classes = [], props = {}) {
  const node = {
    hidden: false,
    dataset: {},
    classSet: new Set(classes),
    attributes: new Map(),
    listeners: new Map(),
    classList: {
      add(name) { node.classSet.add(name); },
      remove(name) { node.classSet.delete(name); },
      contains(name) { return node.classSet.has(name); },
      toggle(name, on) {
        const next = on === undefined ? !node.classSet.has(name) : Boolean(on);
        if (next) node.classSet.add(name); else node.classSet.delete(name);
      },
    },
    setAttribute(name, value) { node.attributes.set(name, String(value)); },
    getAttribute(name) { return node.attributes.has(name) ? node.attributes.get(name) : null; },
    addEventListener(type, handler) {
      if (!node.listeners.has(type)) node.listeners.set(type, []);
      node.listeners.get(type).push(handler);
    },
    click() { for (const handler of node.listeners.get("click") || []) handler({ type: "click", target: node }); },
    querySelector() { return null; },
  };
  return Object.assign(node, props);
}

// The subset of the step tree wireJudgeStep() reaches.
function judgeStep(step) {
  const toggle = box([], { attributes: new Map([["aria-expanded", "false"]]) });
  const body = box([], { hidden: true });
  const curve = box();
  const stepNode = box(["jstep"], {
    dataset: { step },
    querySelector(selector) {
      if (selector === ".jhead-toggle") return toggle;
      if (selector === ".jbody") return body;
      if (selector === ".jcurve") return curve;
      return null;
    },
  });
  return { stepNode, toggle, body, curve };
}

// Splits a selector list on top-level commas only, so :where(a,b) stays whole.
function selectorList(prelude) {
  const parts = [];
  let current = "";
  let depth = 0;
  for (const char of prelude) {
    if (char === "(") depth += 1;
    else if (char === ")") depth -= 1;
    if (char === "," && depth === 0) {
      parts.push(current.trim());
      current = "";
    } else current += char;
  }
  parts.push(current.trim());
  return parts;
}

// First flat rule whose selector list contains `selector` exactly.
function ruleBody(text, selector) {
  const pattern = /([^{}]+)\{([^{}]*)\}/g;
  let match;
  while ((match = pattern.exec(text))) {
    if (selectorList(match[1]).includes(selector)) return match[2];
  }
  return null;
}

function atRuleBody(text, query) {
  const at = text.search(new RegExp(`@media\\s*\\(\\s*${query}\\s*\\)\\s*\\{`));
  if (at === -1) return null;
  const open = text.indexOf("{", at);
  let depth = 0;
  for (let i = open; i < text.length; i += 1) {
    if (text[i] === "{") depth += 1;
    else if (text[i] === "}") {
      depth -= 1;
      if (depth === 0) return text.slice(open + 1, i);
    }
  }
  return null;
}

test("a judge step renders a native disclosure button and a sibling curve button", () => {
  const html = judge.judgeStepHtml({
    step: 4,
    result: "FAIL",
    phase: "P4",
    at: "2026-11-19 10:02",
    description: ["opens <valve>"],
    lines: ["▲", "assert <flow> > 0"],
  }, 1);

  assert.match(html, /<div class="jhead">/);
  assert.doesNotMatch(html, /class="jhead"[^>]*\brole="button"/);
  assert.doesNotMatch(html, /\btabindex=/);
  assert.match(html, /<button class="jhead-toggle" type="button" aria-expanded="false" aria-controls="jbody-1">/);
  assert.match(html, /<svg class="jchev"[^>]*aria-hidden="true"[^>]*focusable="false"/);
  assert.match(html, /<button class="btn small ghost jcurve" type="button" aria-label="曲线：定位 Step\.4">曲线<\/button>/);
  assert.doesNotMatch(html, /\btitle=/);
  assert.match(html, /<pre class="jbody" id="jbody-1" hidden>/);
  assert.match(html, /Step\.4/);
  assert.match(html, /&lt;valve&gt;/);
  assert.match(html, /&lt;flow&gt;/);
  assert.match(html, /class="jbadge fail"/);
});

test("clicking the disclosure button syncs hidden state and aria-expanded", () => {
  const { stepNode, toggle, body } = judgeStep("2");
  judge.wireJudgeStep(stepNode);

  toggle.click();
  assert.equal(body.hidden, false);
  assert.equal(toggle.getAttribute("aria-expanded"), "true");
  assert.equal(stepNode.classList.contains("open"), true);

  toggle.click();
  assert.equal(body.hidden, true);
  assert.equal(toggle.getAttribute("aria-expanded"), "false");
  assert.equal(stepNode.classList.contains("open"), false);
});

test("the programmatic _toggle contract still opens and closes a step", () => {
  const { stepNode, toggle, body } = judgeStep("3");
  judge.wireJudgeStep(stepNode);

  stepNode._toggle(true);
  assert.equal(body.hidden, false);
  assert.equal(toggle.getAttribute("aria-expanded"), "true");
  assert.equal(stepNode.classList.contains("open"), true);

  stepNode._toggle(false);
  assert.equal(body.hidden, true);
  assert.equal(toggle.getAttribute("aria-expanded"), "false");
});

test("the curve action is independent of the disclosure and names its step", () => {
  dispatched.length = 0;
  const { stepNode, toggle, body, curve } = judgeStep("7");
  judge.wireJudgeStep(stepNode);

  curve.click();
  assert.equal(body.hidden, true, "curve action must not expand the step");
  assert.equal(toggle.getAttribute("aria-expanded"), "false");
  assert.equal(dispatched.length, 1);
  assert.equal(dispatched[0].type, "lm-curves-focus-step");
  assert.equal(dispatched[0].detail.step, "7");

  toggle.click();
  assert.equal(body.hidden, false);
});

test("several steps expand independently (no exclusive accordion behavior)", () => {
  const first = judgeStep("1");
  const second = judgeStep("2");
  judge.wireJudgeStep(first.stepNode);
  judge.wireJudgeStep(second.stepNode);

  first.toggle.click();
  second.toggle.click();
  assert.equal(first.body.hidden, false);
  assert.equal(second.body.hidden, false);

  first.toggle.click();
  assert.equal(first.body.hidden, true);
  assert.equal(second.body.hidden, false, "collapsing one step keeps the other open");
});

test("renderJudge uses the native helpers and drops the custom key handler", () => {
  assert.match(source, /el\.innerHTML = rows\.map\(\(s, i\) => judgeStepHtml\(s, i\)\)\.join\(""\);/);
  assert.match(source, /el\.querySelectorAll\("\.jstep"\)\.forEach\(wireJudgeStep\);/);

  const wiring = functionSource(source, "wireJudgeStep");
  assert.doesNotMatch(wiring, /keydown/);
  assert.doesNotMatch(wiring, /role="button"/);
  assert.match(wiring, /toggleBtn\.addEventListener\("click", \(\) => toggle\(\)\)/);
});

test("empty, raw-log, fail-only, and focus-step paths are preserved", () => {
  assert.match(source, /el\.innerHTML = '<div class="jempty">（空）<\/div>';/);
  assert.match(source, /el\.innerHTML = '<pre class="jraw"><\/pre>';/);
  assert.match(source, /judgeSteps\.filter\(\(s\) => !failOnly \|\| s\.result === "FAIL"\)/);
  assert.match(source, /node\._toggle\(true\); node\.scrollIntoView\(\{ block: "center" \}\); node\.classList\.add\("jflash"\)/);
  assert.match(source, /querySelector\(`\.jstep\[data-step="\$\{CSS\.escape\(String\(ev\.detail\.step\)\)\}"\]`\)/);
});

test("the row keeps its layout and the toggle uses the native button focus ring", () => {
  const head = ruleBody(designCss, ".jhead");
  assert.ok(head, "expected a .jhead rule");
  assert.match(head, /padding:\s*11px 14px/);
  assert.match(head, /gap:\s*12px/);
  assert.doesNotMatch(head, /cursor:\s*pointer/, "the non-interactive head must not look clickable");

  const toggle = ruleBody(designCss, ".jhead-toggle");
  assert.ok(toggle, "expected a .jhead-toggle rule");
  assert.match(toggle, /\bflex:\s*1/);
  assert.match(toggle, /min-width:\s*0/);
  assert.match(toggle, /font:\s*inherit/);
  assert.match(toggle, /border:\s*0/);
  assert.match(toggle, /background:\s*none/);
  assert.match(toggle, /cursor:\s*pointer/);

  assert.equal(ruleBody(designCss, ".jhead:focus-visible"), null,
    "the dead .jhead focus rule should be removed");
  const shared = ruleBody(designCss, ":where(input,select,textarea,button,a,[tabindex]):focus-visible");
  assert.ok(shared, "expected the shared focus-visible rule to cover native buttons");
  assert.match(shared, /outline:\s*none/);
  assert.match(shared, /box-shadow:\s*0 0 0 3px var\(--brand-ring\)/);
});

test("forced-colors keeps a visible ring on the native disclosure button", () => {
  const forced = atRuleBody(designCss, "forced-colors:\\s*active");
  assert.ok(forced, "expected a forced-colors block");
  assert.match(forced, /:where\(input,select,textarea,button,a,\[tabindex\]\):focus-visible/);
  assert.match(forced, /outline:\s*3px solid Highlight/);
});

test("lanmatrix-curves.css remains compatible with the new sibling layout", () => {
  assert.match(curvesCss, /\.jstep \.jcurve\s*\{\s*margin-left:auto;\s*flex:0 0 auto;\s*\}/);
  assert.match(curvesCss, /\.jstep\.jflash > \.jhead\s*\{[^}]*box-shadow:inset 3px 0 0 var\(--brand\)/);
});