const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");

const adminHtml = fs.readFileSync(
  path.resolve(__dirname, "../../app/templates/lanmatrix/admin.html"),
  "utf8",
);

function attr(tag, name) {
  const match = tag.match(new RegExp(`\\s${name}="([^"]*)"`));
  return match ? match[1] : null;
}

function tagsWithId(id) {
  return (adminHtml.match(/<[a-z][^>]*>/gi) || []).filter((tag) => attr(tag, "id") === id);
}

function innerText(id, tagName) {
  const escaped = id.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  const match = adminHtml.match(
    new RegExp(`<${tagName}\\b[^>]*\\sid="${escaped}"[^>]*>([\\s\\S]*?)</${tagName}>`, "i"),
  );
  return match ? match[1].replace(/\s+/g, " ").trim() : null;
}

test("concurrency limit input is named by its visible label", () => {
  const inputs = tagsWithId("lm-license-count");
  assert.equal(inputs.length, 1, "expected exactly one #lm-license-count control");
  const input = inputs[0];

  assert.equal(
    attr(input, "aria-labelledby"),
    "lm-license-count-label",
    "the accessible name must come from the visible label via aria-labelledby",
  );
  assert.equal(
    attr(input, "aria-label"),
    null,
    "do not duplicate the visible text in a second aria-label",
  );

  const labelTags = tagsWithId("lm-license-count-label");
  assert.equal(labelTags.length, 1, "expected exactly one #lm-license-count-label element");
  assert.match(labelTags[0], /^<b\b/i, "the visible label is the <b> element");
  assert.equal(
    innerText("lm-license-count-label", "b"),
    "并发上限",
    "the naming element's text must be exactly the visible label",
  );

  assert.equal(
    attr(input, "aria-describedby"),
    "lm-license-count-hint",
    "the hint must be exposed as the description",
  );
  const hintTags = tagsWithId("lm-license-count-hint");
  assert.equal(hintTags.length, 1, "expected exactly one #lm-license-count-hint element");
  assert.match(hintTags[0], /^<small\b/i, "the hint is the <small> element");
  assert.equal(
    innerText("lm-license-count-hint", "small"),
    "同时运行的测试任务上限，超出的任务将进入队列等待",
    "the described element keeps the original visible hint text",
  );
});

test("stepper buttons keep their own accessible names", () => {
  const dec = tagsWithId("lm-lic-dec");
  const inc = tagsWithId("lm-lic-inc");
  assert.equal(dec.length, 1, "expected exactly one #lm-lic-dec button");
  assert.equal(inc.length, 1, "expected exactly one #lm-lic-inc button");
  assert.equal(attr(dec[0], "aria-label"), "减少");
  assert.equal(attr(inc[0], "aria-label"), "增加");
});