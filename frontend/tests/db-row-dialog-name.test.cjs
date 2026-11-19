const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");

const adminDbHtml = fs.readFileSync(
  path.resolve(__dirname, "../../app/templates/lanmatrix/admin_db.html"),
  "utf8",
);

test("row dialog is named by its visible title", () => {
  const dialogTags = adminDbHtml.match(/<dialog\b[^>]*\sid="lm-db-row-dialog"[^>]*>/g) || [];
  assert.equal(dialogTags.length, 1, "expected exactly one #lm-db-row-dialog element");
  assert.match(
    dialogTags[0],
    /\saria-labelledby="lm-db-row-title"/,
    "the row dialog must reference its heading via aria-labelledby",
  );
  assert.doesNotMatch(dialogTags[0], /\saria-label=/, "the name must come from the heading, not a second copy");

  const titleTags = adminDbHtml.match(/<h3\b[^>]*\sid="lm-db-row-title"[^>]*>/g) || [];
  assert.equal(titleTags.length, 1, "expected exactly one #lm-db-row-title heading in the same template");
  assert.ok(
    adminDbHtml.indexOf(titleTags[0]) > adminDbHtml.indexOf(dialogTags[0]),
    "the naming target must live inside the template after the dialog opens",
  );
});