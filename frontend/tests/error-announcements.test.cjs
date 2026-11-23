// Guide `accessibility` (live region urgency): the critical row says
// "`assertive` (or `role="alert"`) ... Interrupts immediately, clears speech
// queue | Session timeout, API failure", and "When to announce live regions"
// says "Announce dynamic errors via live regions: Use `aria-live` or shift
// focus to error lists (4.1.3 Status Messages (Level AA))." These six boxes
// are filled and unhidden by JS after a failed fetch, so they are the page's
// critical error channel and need static alert semantics. Their `hidden`
// toggling stays in JS; only the role is added to the opening tag.
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");

const templates = path.resolve(__dirname, "../../app/templates/lanmatrix");
const read = (file) => fs.readFileSync(path.join(templates, file), "utf8");
const escapeRe = (s) => s.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");

// Template comments explain the rule in prose, so strip them before scanning:
// only real markup should count as an element.
const markupOf = (html) => html.replace(/\{#[\s\S]*?#\}/g, "");

// The full opening tag of the single element carrying this id.
function tagFor(html, id) {
  const found = [
    ...html.matchAll(
      new RegExp(`<(?:div|p)\\b[^>]*\\bid="${escapeRe(id)}"[^>]*>`, "g"),
    ),
  ];
  assert.equal(found.length, 1, `expected exactly one element with id ${id}`);
  return found[0][0];
}

// Attribute value with either quote style (and unquoted values), or null.
function attr(tag, name) {
  const found = tag.match(
    new RegExp(`\\b${name}\\s*=\\s*(?:"([^"]*)"|'([^']*)'|([^\\s>]+))`),
  );
  return found ? (found[1] ?? found[2] ?? found[3]) : null;
}

function assertRoleAlert(tag, id) {
  const role = attr(tag, "role");
  assert.equal(role && role.trim(), "alert", `#${id} must carry role="alert"`);
}

// The six containers filled and unhidden by JS after a failed fetch.
const DYNAMIC_ERRORS = [
  // dashboard.js:272/317
  { file: "dashboard.html", id: "lm-dash-err" },
  // dashboard.js:415/573/586
  { file: "dashboard.html", id: "lm-vc-err" },
  // dashboard.js:459/474
  { file: "dashboard.html", id: "lm-vc-history-error" },
  // home.js:709 sets text, home.js:710 unhides, home.js:721 hides
  { file: "home.html", id: "lm-home-err" },
  // project_tasks.js:1098/1104 toggles hidden
  { file: "project_tasks.html", id: "lm-tasks-denied" },
  // projects.js:8-9 resolve the elements; projects.js:280-281 unhide and fill it
  { file: "projects.html", id: "lm-project-err" },
];

test("failed-fetch error containers carry role=alert", () => {
  const cache = new Map();
  const htmlOf = (file) => {
    if (!cache.has(file)) cache.set(file, markupOf(read(file)));
    return cache.get(file);
  };

  for (const { file, id } of DYNAMIC_ERRORS) {
    const tag = tagFor(htmlOf(file), id);
    assertRoleAlert(tag, id);
    // The task is to add semantics only: the class and the server-rendered
    // `hidden` state that the JS toggles must survive unchanged.
    assert.equal(
      attr(tag, "class"),
      "state err",
      `#${id} must keep its state err classes`,
    );
    assert.ok(
      /\shidden(?:\s|=|\/?>)/.test(tag),
      `#${id} must stay hidden until JS shows it`,
    );
  }
});

// Alert regions that already existed before this change. None may lose its
// role: this allow-list is the removal guard for the rest of the templates.
const PRE_EXISTING_ALERTS = [
  { file: "admin_db.html", id: "lm-db-tv-error" },
  { file: "admin_db.html", id: "lm-db-error" },
  { file: "admin_db.html", id: "lm-db-row-error" },
  { file: "editor.html", id: "lm-queue-error" },
  { file: "editor.html", id: "lm-export-error" },
  { file: "editor.html", id: "lm-import-error" },
  { file: "login.html", id: "lm-login-error" },
  { file: "models.html", id: "lm-ver-err" },
  { file: "password.html", id: "lm-password-error" },
  { file: "project_tasks.html", id: "lm-tasks-error" },
  { file: "register.html", id: "lm-register-error" },
  { file: "ut_binding.html", id: "ut-binding-error" },
];

test("pre-existing alert regions keep role=alert", () => {
  const cache = new Map();
  const htmlOf = (file) => {
    if (!cache.has(file)) cache.set(file, markupOf(read(file)));
    return cache.get(file);
  };

  for (const { file, id } of PRE_EXISTING_ALERTS) {
    assertRoleAlert(tagFor(htmlOf(file), id), id);
  }

  // base_lm.html's workspace error region is id-less, so match its markup.
  assert.match(
    htmlOf("base_lm.html"),
    /<div class="state err" role="alert">/,
    "base_lm.html workspace error region must keep role=alert",
  );
});