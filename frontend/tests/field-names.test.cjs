/*
 * Accessible names for controls whose name was previously only a placeholder
 * (placeholders disappear while typing and cannot serve as a name).
 *
 * Hidden file inputs are intentionally out of scope. Templates are read as
 * text: each target id must carry the required aria-label / aria-labelledby,
 * and every aria-labelledby reference must resolve inside the same file.
 */
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");

const app = path.resolve(__dirname, "../../app");
const templates = path.join(app, "templates/lanmatrix");

function read(name) {
  return fs.readFileSync(path.join(templates, name), "utf8");
}

function attributes(markup, id) {
  const tag = markup.match(new RegExp(`<[^<>]*\\sid="${id}"(?=[\\s>])[^<>]*>`));
  assert.ok(tag, `Expected an element with id="${id}"`);
  const entries = [...tag[0].matchAll(/\s([\w-]+)(?:="([^"]*)")?/g)];
  return new Map(entries.map(([, name, value]) => [name, value ?? ""]));
}

// Search inputs: aria-label must equal the visible placeholder.
const searchInputs = [
  ["home.html", "lm-h-text", "搜索任务 / test id / 函数…"],
  ["members.html", "lm-mem-search", "搜索用户名 / 姓名，或从下方直接选择…"],
  ["project_tasks.html", "lm-f-text", "搜索任务 / test id…"],
  ["project_tasks.html", "cv-search", "搜索变量名或显示名…"],
  ["projects.html", "lm-proj-search", "搜索项目编码或名称…"],
  ["ut_pipeline.html", "ut-q", "搜索函数 / 源文件…"],
  ["ut_tasks.html", "ut-q", "搜索函数 / 源文件…"],
];

for (const [file, id, placeholder] of searchInputs) {
  test(`${file} #${id} is named by its placeholder text`, () => {
    const attrs = attributes(read(file), id);
    assert.equal(attrs.get("aria-label"), placeholder);
    assert.equal(attrs.get("placeholder"), placeholder);
  });
}

// Controls without a visible label get an explicit aria-label.
test("members.html #lm-mem-role has an explicit name", () => {
  assert.equal(attributes(read("members.html"), "lm-mem-role").get("aria-label"), "成员角色");
});

// Controls whose visible name sits next to them reference it, not copy it.
const labelledBy = [
  ["members.html", "lm-rv-pol-reviewer"],
  ["ai_drafts.html", "lm-ai-gen-payload"],
  ["ai_drafts.html", "lm-ai-signals-text"],
];

for (const [file, id] of labelledBy) {
  test(`${file} #${id} is named via aria-labelledby`, () => {
    const attrs = attributes(read(file), id);
    const refs = (attrs.get("aria-labelledby") ?? "").split(/\s+/).filter(Boolean);
    assert.ok(refs.length > 0, `#${id} must have aria-labelledby`);
    assert.equal(attrs.has("aria-label"), false, `#${id} must not duplicate the name in aria-label`);
  });
}

// Every aria-labelledby reference in these templates resolves in the same file.
for (const file of [
  "home.html",
  "members.html",
  "project_tasks.html",
  "projects.html",
  "ut_pipeline.html",
  "ut_tasks.html",
  "ai_drafts.html",
]) {
  test(`${file}: aria-labelledby targets exist in the same file`, () => {
    const markup = read(file);
    const ids = new Set([...markup.matchAll(/\sid="([^"]+)"/g)].map((m) => m[1]));
    const refs = [...markup.matchAll(/\saria-labelledby="([^"]+)"/g)]
      .flatMap((m) => m[1].split(/\s+/))
      .filter(Boolean);
    assert.ok(refs.length > 0, `${file} should contain at least one aria-labelledby`);
    for (const ref of refs) {
      assert.ok(ids.has(ref), `${file}: aria-labelledby="${ref}" has no matching id`);
    }
  });
}