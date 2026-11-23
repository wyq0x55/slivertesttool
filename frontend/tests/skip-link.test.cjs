/* Skip link + focusable main landmark (WCAG 2.4.1 Bypass Blocks).
 *
 * Static checks only: the template and the stylesheet. No browser is involved,
 * so this cannot prove real keyboard/screen-magnifier behaviour — it only
 * locks the structure the guide prescribes:
 *  - the anchor is the FIRST focusable element in the signed-in shell,
 *  - it targets a <main> that carries id="lm-main" tabindex="-1",
 *  - the login branch (which shares this base) is untouched,
 *  - .lm-skip is hidden by transform and only becomes visible on focus.
 */
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");

const app = path.resolve(__dirname, "../../app");
// Normalise CRLF: the checkout uses CRLF, the markers below assume LF.
const template = fs
  .readFileSync(path.join(app, "templates/lanmatrix/base_lm.html"), "utf8")
  .replace(/\r\n/g, "\n");
const css = fs
  .readFileSync(path.join(app, "static/css/lanmatrix.css"), "utf8")
  .replace(/\r\n/g, "\n")
  .replace(/\/\*[\s\S]*?\*\//g, "");

const SKIP_ANCHOR = '<a class="lm-skip" href="#lm-main">跳到主要内容</a>';
const MAIN_TAG = '<main class="lm-main" id="lm-main" tabindex="-1">';

// Jinja comments may mention tags in prose; drop them before scanning markup.
function stripJinjaComments(source) {
  return source.replace(/\{#[\s\S]*?#\}/g, "");
}

function cssBlocks(source) {
  const blocks = [];
  const re = /([^{}]+)\{([^{}]*)\}/g;
  let match;
  while ((match = re.exec(source))) {
    blocks.push({
      selectors: match[1].split(",").map((s) => s.trim()).filter(Boolean),
      body: match[2],
    });
  }
  return blocks;
}

function cssRuleFor(selector) {
  const block = cssBlocks(css).find((b) => b.selectors.includes(selector));
  assert.ok(block, `Expected a CSS rule whose selector list includes ${selector}`);
  return block.body;
}

function shellRegion() {
  const open = "\n{% if user %}\n";
  const start = template.indexOf(open);
  assert.notEqual(start, -1, "block-level {% if user %} must exist");
  const appOpen = template.indexOf('<div class="app" id="lm-app">', start);
  assert.notEqual(appOpen, -1, "the signed-in shell must open with #lm-app");
  return template.slice(start + open.length, appOpen);
}

test("skip anchor is the only element ahead of the app shell", () => {
  // Nothing but the (comment-stripped) anchor may sit between {% if user %}
  // and #lm-app, so it is provably the first focusable element on the page.
  const region = stripJinjaComments(shellRegion()).trim();
  assert.equal(region, SKIP_ANCHOR);
});

test("skip anchor text is Chinese and matches the product language", () => {
  assert.ok(template.includes(SKIP_ANCHOR));
  assert.match(SKIP_ANCHOR, />[一-鿿]+<\/a>$/);
});

test("anchor is focusable: no tabindex=-1, hidden or inert", () => {
  const tag = template.match(/<a\b[^>]*class="lm-skip"[^>]*>/)[0];
  assert.ok(!/\stabindex=/.test(tag), "the link itself must stay in the tab order");
  assert.ok(!/\shidden\b/.test(tag));
  assert.ok(!/\sinert\b/.test(tag));
  assert.ok(/\shref="#lm-main"/.test(tag));
});

test("anchor precedes the sidebar, nav and main landmark in document order", () => {
  const anchorAt = template.indexOf(SKIP_ANCHOR);
  assert.notEqual(anchorAt, -1);
  for (const marker of ['<aside class="side"', '<nav class="nav">', MAIN_TAG]) {
    assert.ok(
      anchorAt < template.indexOf(marker),
      `${marker} must come after the skip link`,
    );
  }
});

test("main landmark exists once and is programmatically focusable", () => {
  // Prose comments (e.g. "jump straight into <main>") are not markup.
  const mains = stripJinjaComments(template).match(/<main\b[^>]*>/g) || [];
  assert.deepEqual(mains, [MAIN_TAG]);
  assert.ok(template.includes('id="lm-main"'));
  assert.match(template, /tabindex="-1"/);
});

test("login branch stays untouched", () => {
  const elseAt = template.indexOf("\n{% else %}\n", template.indexOf(MAIN_TAG));
  assert.notEqual(elseAt, -1, "expected the {% else %} login branch");
  const endifAt = template.indexOf("{% endif %}", elseAt);
  assert.notEqual(endifAt, -1);
  const loginBranch = template.slice(elseAt, endifAt + "{% endif %}".length);
  assert.ok(loginBranch.includes("{{ self.content() }}"));
  assert.ok(!loginBranch.includes("lm-skip"));
  assert.ok(!loginBranch.includes("lm-main"));
});

test(".lm-skip is off-screen until focused, and visible on focus", () => {
  const base = cssRuleFor(".lm-skip");
  assert.match(base, /position:\s*fixed/);
  assert.match(base, /transform:\s*translateY\(-/);
  assert.ok(!/display:\s*none/.test(base), "display:none would make it unfocusable");
  assert.ok(!/visibility:\s*hidden/.test(base));

  for (const selector of [".lm-skip:focus", ".lm-skip:focus-visible"]) {
    const rule = cssRuleFor(selector);
    assert.match(rule, /transform:\s*none/);
    assert.match(rule, /outline:\s*2px solid var\(--brand\)/);
  }
});

test(".lm-skip is appended after the existing visually-hidden utility", () => {
  const hiddenUtilityAt = css.indexOf(".lm-fields-status");
  const skipAt = css.indexOf(".lm-skip {");
  assert.notEqual(hiddenUtilityAt, -1);
  assert.ok(skipAt > hiddenUtilityAt, ".lm-skip must not precede .lm-fields-status");
});