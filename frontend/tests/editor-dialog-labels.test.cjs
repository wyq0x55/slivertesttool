const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");

const editorHtml = fs.readFileSync(path.resolve(__dirname, "../../app/templates/lanmatrix/editor.html"), "utf8");

function inputTag(id) {
  const tags = editorHtml.match(new RegExp(`<[^>]+\\sid="${id}"[^>]*>`, "g")) || [];
  assert.equal(tags.length, 1, `#${id} must be unique`);
  assert.match(tags[0], /^<input\s/);
  return tags[0];
}

test("queue filter has a persistent label and retains its search hints", () => {
  const input = inputTag("lm-queue-filter");
  assert.ok(/<label for="lm-queue-filter">筛选用例<\/label>/.test(editorHtml), "expected a visible label for the queue filter");
  assert.match(input, /\stype="search"/);
  assert.match(input, /\splaceholder="按 test_id 或说明筛选…"/);
  assert.match(input, /\sautocomplete="off"/);
  assert.match(input, /\sautocorrect="off"/);
  assert.match(input, /\sautocapitalize="none"/);
  assert.match(input, /\sspellcheck="false"/);
});

test("Excel label targets the visible import picker and leaves the upload proxy hidden", () => {
  const picker = inputTag("lm-import-dialog-file");
  assert.ok(
    /<label for="lm-import-dialog-file">Excel 文件（\.xlsx）\s*<input\b[^>]*\sid="lm-import-dialog-file"[^>]*>\s*<\/label>/.test(editorHtml),
    "expected a visible label for the import picker",
  );
  assert.match(picker, /\stype="file"/);
  assert.match(picker, /\saccept="\.xlsx"/);
  assert.doesNotMatch(picker, /\shidden(?=[\s=>])/);

  const proxy = inputTag("lm-import-file");
  assert.match(proxy, /\stype="file"/);
  assert.match(proxy, /\saccept="\.xlsx"/);
  assert.match(proxy, /\shidden(?=[\s=>])/);
  assert.doesNotMatch(editorHtml, /<label\b[^>]*\sfor="lm-import-file"/);
});
