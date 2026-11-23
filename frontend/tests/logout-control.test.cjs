// Guide `accessibility` (semantic HTML and ARIA): "Avoid `<a role="button">`
// and use `<button>` instead." Signing out is an action, not a navigation, so
// the rail control is a native <button>; that also makes Enter/Space activation
// the platform's job instead of link semantics that misreport the role to AT.
// The behaviour contract stays the delegated click handler in api.js, keyed on
// the element id rather than its tag — these tests pin both sides of that.
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");

const app = path.resolve(__dirname, "../../app");
const template = fs.readFileSync(
  path.join(app, "templates/lanmatrix/base_lm.html"),
  "utf8",
);
const api = fs.readFileSync(path.join(app, "static/js/lanmatrix/api.js"), "utf8");

// Template comments discuss the markup in prose (and mention #lm-logout), so
// strip them before scanning for tags or inline registration.
const markup = template.replace(/\{#[\s\S]*?#\}/g, "");

const escapeRe = (s) => s.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
const ID = "lm-logout";

// Opening tags of every element carrying the given id, whatever their name.
function tagsWithId(html, id) {
  return [
    ...html.matchAll(new RegExp(`<[a-z][a-z0-9-]*\\b[^<>]*\\bid="${escapeRe(id)}"[^<>]*>`, "g")),
  ].map((m) => m[0]);
}

// Attribute map of one opening tag; boolean attributes map to "".
function attributes(tag) {
  const attrs = {};
  for (const m of tag.matchAll(/([a-z][a-z0-9-]*)(?:\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s>]+)))?/gi)) {
    const name = m[1].toLowerCase();
    if (name === tag.slice(1).split(/[\s>]/)[0].toLowerCase()) continue; // tag name
    attrs[name] = m[2] ?? m[3] ?? m[4] ?? "";
  }
  return attrs;
}

// Every inline script body on the page, joined.
function inlineScripts(html) {
  return [...html.matchAll(/<script(?![^>]*\bsrc=)[^>]*>([\s\S]*?)<\/script>/g)]
    .map((m) => m[1])
    .join("\n");
}

test("the sign-out control is a native button with the expected content", () => {
  const tags = tagsWithId(markup, ID);
  assert.equal(tags.length, 1, `expected exactly one element with id ${ID}`);
  const tag = tags[0];
  assert.ok(/^<button\b/.test(tag), `expected <button>, got ${tag}`);

  const attrs = attributes(tag);
  assert.equal(attrs.type, "button", "an explicit type keeps it out of submit semantics");
  const classes = (attrs.class || "").split(/\s+/).filter(Boolean);
  assert.ok(classes.includes("btn"), "the control keeps the shared .btn styling");
  assert.ok(classes.includes("ghost"), "the rail keeps the ghost (borderless) variant");

  const body = markup.slice(markup.indexOf(tag), markup.indexOf("</button>", markup.indexOf(tag)));
  assert.match(body, /<svg\b[^>]*>[\s\S]*?<path\b/, "the sign-out icon is preserved");
  assert.match(body, /<span>\s*退出登录\s*<\/span>/, "the visible label is preserved");
});

test("no anchor still carries the sign-out id", () => {
  const anchors = [...markup.matchAll(/<a\b[^<>]*>/g)].map((m) => m[0]);
  assert.deepEqual(
    anchors.filter((tag) => attributes(tag).id === ID),
    [],
    "a placeholder href on an action link misreports the role and needs JS to stop navigation",
  );
});

test("the delegated handler still matches the control by id", () => {
  const selectors = [...api.matchAll(/closest\(\s*(["'])(#[^"']+)\1\s*\)/g)].map((m) => m[2]);
  assert.equal(selectors.length, 1, "expected one delegated logout match in api.js");
  const selector = selectors[0];
  assert.equal(selector, `#${ID}`, "the handler and the template agree on the id");

  // The id in the selector is exactly the id the template renders, so the
  // delegated lookup resolves to the button above.
  assert.deepEqual(tagsWithId(markup, selector.slice(1)).length, 1);

  // And the handler still performs the state-changing half of the action.
  const handler = api.slice(api.indexOf(`closest("${selector}")`));
  assert.match(handler, /e\.preventDefault\(\)/, "the default must not navigate");
  assert.match(handler, /await\s+LMApi\.logout\(\)/, "the session is ended server-side");
  assert.match(handler, /window\.location\s*=\s*LM\.urls\.login/, "then redirect to login");
});

test("the control needs no page-local registration", () => {
  assert.doesNotMatch(
    inlineScripts(markup),
    new RegExp(escapeRe(ID)),
    "logout is wired once, by the delegated api.js handler",
  );
  assert.match(
    template,
    /<script src="\{\{\s*static_asset_url\('js\/lanmatrix\/api\.js'\)\s*\}\}"><\/script>/,
    "api.js stays on every logged-in page, so the delegation is always attached",
  );
});

test("the collapsed rail tooltip still covers the button", () => {
  // In collapsed mode the <span> label is hidden, and the rail script copies
  // each label into a native title. The selector has to include buttons now.
  assert.match(
    inlineScripts(markup),
    /querySelectorAll\('\.nav a, \.side-foot a, \.side-foot button'\)/,
    "collapsed-rail tooltips should not silently skip the sign-out control",
  );
});