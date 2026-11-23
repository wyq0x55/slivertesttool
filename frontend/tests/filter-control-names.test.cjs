// Guide `accessibility`: "Mandatory - Provide a label for all form controls"
// (3.3.2 Labels or Instructions, Level A) and "Don't use placeholders as
// primary labels" — a control named only by `title` has no reliable accessible
// name: `title` is announced inconsistently and is unavailable on touch. The
// SQL editor was named only by its placeholder, which disappears on input.
//
// Each list-filter control now carries an `aria-label`. For the seven controls
// that already had a `title`, the aria-label repeats the same words (so the
// Chinese vocabulary does not drift) and the title stays as the mouse hint.
// For `#lm-db-sql` the aria-label names the field and the placeholder stays as
// the example hint. Nothing visual changes: no class is altered and no new
// element is added.
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");

const templates = path.resolve(__dirname, "../../app/templates/lanmatrix");
const read = (file) => fs.readFileSync(path.join(templates, file), "utf8");

const escapeRe = (s) => s.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");

// The whole opening tag of the element whose id is `id`, across line breaks
// (the SQL textarea breaks its attributes over two lines).
function openingTag(html, tag, id) {
  const re = new RegExp(`<${tag}\\b[^>]*\\sid="${escapeRe(id)}"[^>]*>`, "i");
  const match = html.match(re);
  assert.ok(match, `<${tag} id="${id}"> must exist`);
  return match[0];
}

const attr = (tag, name) => {
  const match = tag.match(new RegExp(`(?:^|\\s)${name}="([^"]*)"`, "i"));
  return match ? match[1] : null;
};

const countAttr = (tag, name) =>
  (tag.match(new RegExp(`(?:^|\\s)${name}\\s*=`, "gi")) || []).length;

// The seven controls that were named only by `title`, plus the SQL editor that
// was named only by its placeholder. `title` is the exact pre-existing wording.
const CONTROLS = [
  { file: "home.html", tag: "select", id: "lm-h-project", label: "按项目筛选", class: "selbox" },
  {
    file: "home.html",
    tag: "input",
    id: "lm-h-check-all",
    label: "全选/全不选 Silver 运行（当前筛选）",
  },
  { file: "project_tasks.html", tag: "select", id: "lm-f-submitter", label: "按提交者筛选", class: "selbox" },
  {
    file: "project_tasks.html",
    tag: "input",
    id: "lm-check-all",
    label: "全选/全不选（当前筛选）",
  },
  { file: "ut_tasks.html", tag: "select", id: "ut-batch", label: "按批次筛选", class: "selbox" },
  { file: "ut_pipeline.html", tag: "input", id: "ut-all", label: "全选/全不选（当前筛选）" },
  { file: "ai_drafts.html", tag: "select", id: "lm-ai-f-scenario", label: "按场景筛选", class: "selbox" },
  // No title before or after: the placeholder remains the example hint.
  { file: "admin_db.html", tag: "textarea", id: "lm-db-sql", label: "SQL 语句", class: "lm-db-sql" },
];

// A visible control must not be hidden from assistive tech or the layout.
function assertVisible(tag, id) {
  assert.doesNotMatch(tag, /(?:^|\s)hidden(?:\s|=|>)/i, `#${id} must not carry hidden`);
  assert.notEqual(attr(tag, "aria-hidden"), "true", `#${id} must not be aria-hidden`);
  const style = attr(tag, "style");
  if (style) {
    assert.doesNotMatch(style, /display\s*:\s*none|visibility\s*:\s*hidden/i, `#${id} must not be hidden by style`);
  }
}

for (const { file, tag, id, label, class: className } of CONTROLS) {
  test(`${file}: #${id} has a stable accessible name`, () => {
    const html = read(file);
    const open = openingTag(html, tag, id);

    const ariaLabel = attr(open, "aria-label");
    assert.equal(typeof ariaLabel, "string", `#${id} must carry aria-label`);
    assert.ok(ariaLabel.trim().length > 0, `#${id} aria-label must not be empty`);
    assert.equal(ariaLabel, label, `#${id} aria-label must keep the UI wording`);
    assert.equal(countAttr(open, "aria-label"), 1, `#${id} must have exactly one aria-label`);

    assertVisible(open, id);

    if (className) {
      assert.equal(attr(open, "class"), className, `#${id} class must not change`);
    } else {
      assert.equal(attr(open, "class"), null, `#${id} must not gain a class`);
    }
  });
}

// `title` stays as the mouse hint and its wording is unchanged; the SQL editor
// never had one and must not gain one.
for (const { file, tag, id, label } of CONTROLS.filter((c) => c.id !== "lm-db-sql")) {
  test(`${file}: #${id} keeps its original title text`, () => {
    const open = openingTag(read(file), tag, id);
    assert.equal(attr(open, "title"), label, `#${id} title text must not change`);
    assert.equal(attr(open, "aria-label"), attr(open, "title"), `#${id} aria-label must mirror title`);
  });
}

test("admin_db.html: #lm-db-sql keeps its placeholder as the example hint", () => {
  const open = openingTag(read("admin_db.html"), "textarea", "lm-db-sql");
  assert.equal(attr(open, "title"), null, "#lm-db-sql must not add a title");
  assert.equal(attr(open, "placeholder"), "SELECT * FROM lm_project LIMIT 50;");
  assert.match(open, /spellcheck="false"/, "#lm-db-sql must keep spellcheck off");
});

test("every control in the list is covered exactly once", () => {
  const ids = CONTROLS.map((c) => c.id);
  assert.equal(new Set(ids).size, ids.length, "the control list must not repeat an id");
  for (const { file, tag, id } of CONTROLS) {
    const open = openingTag(read(file), tag, id);
    assert.ok(attr(open, "aria-label"), `#${id} must remain named`);
  }
});