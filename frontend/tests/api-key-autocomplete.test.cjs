const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");

const app = path.resolve(__dirname, "../../app");
const template = fs.readFileSync(path.join(app, "templates/lanmatrix/ai_drafts.html"), "utf8");
const inputs = template.match(/<input\b[^<>]*>/g) || [];

function inputById(id) {
  const matches = inputs.filter((tag) => tag.includes(`id="${id}"`));
  assert.equal(matches.length, 1, `Expected exactly one input with id="${id}"`);
  return matches[0];
}

function attribute(tag, name) {
  const match = tag.match(new RegExp(`\\s${name}="([^"]*)"`));
  return match ? match[1] : undefined;
}

test("the LLM gateway API key field is a password input that browser password managers leave alone", () => {
  const tag = inputById("lm-ai-set-key");

  // The field still masks the pasted key.
  assert.equal(attribute(tag, "type"), "password");
  // autocomplete="off" marks this as an API credential, not a login password,
  // so browsers neither offer to save it nor autofill a saved password into it.
  assert.equal(attribute(tag, "autocomplete"), "off");
  // Nothing else about the field changed.
  assert.equal(attribute(tag, "class"), "inbox");
  assert.equal(attribute(tag, "placeholder"), "***set***（留空保持不变）");
});

test("no other input gained an autocomplete attribute", () => {
  // Before this change the only autocomplete in the file was on
  // lm-ai-gen-item-ids; the API key field is the second and last.
  const withAutocomplete = inputs
    .map((tag) => ({ tag, id: attribute(tag, "id") }))
    .filter(({ tag }) => /\sautocomplete=/.test(tag))
    .map(({ id }) => id);

  assert.deepEqual(withAutocomplete.sort(), ["lm-ai-gen-item-ids", "lm-ai-set-key"]);
  assert.equal((template.match(/\sautocomplete=/g) || []).length, 2);
});