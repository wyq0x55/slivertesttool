// Guide `accessibility`: "Use the `<table>` element to identify the table, the
// `<caption>` element to provide a title and accessible name for the
// description, the `<tr>` element to identify individual rows in the table, and
// the `<th>` and `<td>` elements to identify header and data cells
// respectively. (1.3.1 Info and Relationships (Level A))"
//
// The eleven data tables below are named by their nearest visual heading, but a
// heading is not part of the table's accessible name, so each table opens with
// a visually hidden `<caption class="lm-sr-only">`. The caption is the table's
// FIRST child; adding `aria-label` instead would duplicate the name and is
// rejected here.
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");

const root = path.resolve(__dirname, "../..");
const templates = path.join(root, "app/templates/lanmatrix");
const read = (file) => fs.readFileSync(path.join(templates, file), "utf8");

// Document order, so a table that loses its caption or swaps position fails
// with a message naming the page and the caption that was expected.
const EXPECTED = {
  "admin.html": ["账号列表", "全部任务"],
  "audit.html": ["审计日志记录"],
  "dashboard.html": ["模型修订效果", "版本结果对比", "用例运行历史"],
  "editor.html": ["键盘快捷键", "其他操作方式"],
  "fields.html": ["字段列表"],
  "tokens.html": ["API Token 列表"],
  "trash.html": ["回收站条目"],
};

const TABLE = /<table\b([^>]*)>([\s\S]*?)<\/table>/g;
const CAPTION_AT_TOP = /^\s*<caption\b([^>]*)>([\s\S]*?)<\/caption>/;

test("each of the eleven tables opens with a non-empty, unique sr-only caption", () => {
  let tables = 0;

  for (const [file, expected] of Object.entries(EXPECTED)) {
    const html = read(file);
    const captions = [];

    for (const match of html.matchAll(TABLE)) {
      const [, attrs, inner] = match;
      tables += 1;
      const line = html.slice(0, match.index).split("\n").length;

      assert.doesNotMatch(attrs, /\baria-label\s*=/, `${file}:${line} table must not use aria-label in place of a caption`);

      const caption = CAPTION_AT_TOP.exec(inner);
      assert.ok(caption, `${file}:${line} table must open with a <caption> as its first child`);
      assert.match(caption[1], /(?:^|\s)class="[^"]*\blm-sr-only\b[^"]*"/, `${file}:${line} caption must be visually hidden with class="lm-sr-only"`);

      const text = caption[2].replace(/<[^>]*>/g, "").replace(/\s+/g, " ").trim();
      assert.ok(text, `${file}:${line} caption must not be empty`);
      captions.push(text);
    }

    assert.deepEqual(captions, expected, `${file} captions, in document order`);
    assert.equal(new Set(captions).size, captions.length, `${file} caption text must be unique within the page`);
  }

  // Guard the scan itself: a markup change that stops the regex from matching
  // must not let this test pass by checking nothing.
  assert.equal(tables, 11, `expected the seven templates to hold exactly 11 tables, found ${tables}`);
});

test(".lm-sr-only clips captions from sight without hiding them from assistive tech", () => {
  const css = fs.readFileSync(path.join(root, "app/static/css/app.css"), "utf8");
  const blocks = [...css.matchAll(/\.lm-sr-only\s*\{([\s\S]*?)\}/g)];
  assert.equal(blocks.length, 1, "app.css defines .lm-sr-only exactly once");

  const rule = blocks[0][1];
  assert.match(rule, /position:\s*absolute/, ".lm-sr-only is taken out of the layout flow");
  assert.match(rule, /clip-path:\s*inset\(50%\)/, ".lm-sr-only is clipped to zero area");
  assert.match(rule, /white-space:\s*nowrap/, ".lm-sr-only cannot wrap into visible lines");
});