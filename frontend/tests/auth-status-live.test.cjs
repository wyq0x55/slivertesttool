// Guide `accessibility` (live region urgency): critical messages such as a
// failed API call use assertive (`role="alert"`), while ordinary success status
// uses polite (`role="status"`). Guide `accessible-error-announcement` also
// requires the region to be changed, not shown: "a live region that is shown,
// rather than changed, isn't announced reliably". So the four auth status boxes
// stay mounted with no `hidden` attribute and the inline scripts only rewrite
// their text. `password.html` is rendered inside base_lm.html's `<main>` for
// logged-in users, so it must not open a second `<main>` of its own.
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");

const templates = path.resolve(__dirname, "../../app/templates/lanmatrix");
const read = (file) => fs.readFileSync(path.join(templates, file), "utf8");

const escapeRe = (s) => s.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");

// The full opening tag of the single element carrying this id.
function tagFor(html, id) {
  const found = [
    ...html.matchAll(new RegExp(`<div\\b[^>]*\\bid="${escapeRe(id)}"[^>]*>`, "g")),
  ];
  assert.equal(found.length, 1, `expected exactly one element with id ${id}`);
  return found[0][0];
}

// Template comments explain the landmark rule in prose, so strip them before
// counting landmarks: only real markup should count.
const markupOf = (html) => html.replace(/\{#[\s\S]*?#\}/g, "");

// Every inline script block on the page.
function scriptsOf(html) {
  return [...html.matchAll(/<script>([\s\S]*?)<\/script>/g)]
    .map((m) => m[1])
    .join("\n");
}

const LIVE_REGIONS = {
  "login.html": { "lm-login-error": "alert" },
  "register.html": {
    "lm-register-error": "alert",
    "lm-register-ok": "status",
  },
  "password.html": { "lm-password-error": "alert" },
};

test("auth status boxes are mounted live regions with the right urgency", () => {
  for (const [file, boxes] of Object.entries(LIVE_REGIONS)) {
    const html = read(file);
    for (const [id, role] of Object.entries(boxes)) {
      const tag = tagFor(html, id);
      assert.match(
        tag,
        new RegExp(`\\brole="${role}"`),
        `${file} #${id} must carry role="${role}"`,
      );
      assert.doesNotMatch(
        tag,
        /\bhidden\b/,
        `${file} #${id} must stay mounted, not start hidden`,
      );
    }
  }
});

test("inline scripts change the live-region text instead of toggling hidden", () => {
  for (const file of Object.keys(LIVE_REGIONS)) {
    const script = scriptsOf(read(file));
    assert.notEqual(script.trim(), "", `${file} must have an inline script`);
    assert.doesNotMatch(
      script,
      /\bhidden\b/,
      `${file} must not toggle the hidden attribute on its status box`,
    );
    assert.match(
      script,
      /err\.textContent\s*=/,
      `${file} must write the error message into textContent`,
    );
    assert.match(
      script,
      /textContent\s*=\s*''/,
      `${file} must clear the region's text when the next submit starts`,
    );
  }
});

test("an empty live region does not leave a styled box behind", () => {
  for (const file of Object.keys(LIVE_REGIONS)) {
    const html = read(file);
    const rule = html.match(/\.login-card \.login-err:empty\s*\{([^}]*)\}/);
    assert.ok(rule, `${file} must scope a :empty rule to the auth card`);
    assert.match(rule[1], /padding:\s*0/, `${file} must collapse empty padding`);
    assert.match(
      rule[1],
      /background:\s*transparent/,
      `${file} must drop the empty background`,
    );
    assert.doesNotMatch(
      rule[1],
      /display\s*:/,
      `${file} must keep the region in the accessibility tree (no display:none)`,
    );
  }
});

test("register success box keeps its success colors without an inline style", () => {
  const html = read("register.html");
  assert.match(tagFor(html, "lm-register-ok"), /\blogin-ok\b/);
  assert.match(html, /\.login-card \.login-ok\s*\{[^}]*--ok-soft[^}]*--ok\)/);
  assert.doesNotMatch(
    tagFor(html, "lm-register-ok"),
    /style=/,
    "colors belong in the scoped rule, not a tag that :empty cannot override",
  );
});

test("password.html leaves the main landmark to base_lm.html", () => {
  const html = markupOf(read("password.html"));
  assert.doesNotMatch(
    html,
    /<main\b/,
    "base_lm.html already renders <main> for logged-in users",
  );
  assert.doesNotMatch(html, /<\/main>/);
  assert.match(
    html,
    /<div class="login-main">/,
    "the wrapper keeps login-main for styling",
  );
});

test("guest auth pages keep exactly one main landmark", () => {
  for (const file of ["login.html", "register.html"]) {
    const html = markupOf(read(file));
    assert.equal(
      (html.match(/<main\b/g) || []).length,
      1,
      `${file} must render exactly one <main>`,
    );
    assert.equal(
      (html.match(/<\/main>/g) || []).length,
      1,
      `${file} must close exactly one <main>`,
    );
  }
});