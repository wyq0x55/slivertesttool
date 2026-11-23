// Guide `accessibility`: a `<th>` without `scope` leaves the cell-to-header
// relationship to the screen reader's guess. Every header cell in the
// lanmatrix tables is a column header (all of them live in `<thead>`), so each
// one declares `scope="col"` explicitly -- (1.3.1 Info and Relationships,
// Level A).
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");

const templates = path.resolve(__dirname, "../../app/templates/lanmatrix");
const files = fs.readdirSync(templates).filter((file) => file.endsWith(".html")).sort();
const read = (file) => fs.readFileSync(path.join(templates, file), "utf8");
const TH = /<th\b[^>]*>/g;

test("every table header cell declares col or row scope", () => {
  const offenders = [];
  let headers = 0;

  for (const file of files) {
    const html = read(file);
    for (const match of html.matchAll(TH)) {
      headers += 1;
      const tag = match[0];
      const scope = tag.match(/(?:^|\s)scope="([^"]*)"/);
      if (!scope || (scope[1] !== "col" && scope[1] !== "row")) {
        const line = html.slice(0, match.index).split("\n").length;
        offenders.push(`${file}:${line} ${tag}`);
      }
    }
  }

  // Guard the regex itself: if template markup changes shape the scan must not
  // pass by matching nothing.
  assert.ok(headers > 100, `expected the template scan to find many headers, found ${headers}`);
  assert.deepEqual(offenders, [], `headers missing scope="col" or scope="row":\n${offenders.join("\n")}`);
});

test("ut_tasks and dashboard tables mark their columns", () => {
  const utTasks = read("ut_tasks.html");
  assert.match(utTasks, /<th scope="col" class="ut-check">[\s\S]*?<input type="checkbox" id="ut-requeue-all"/);
  for (const label of ["作业", "函数", "状态", "机器判定", "模型修正", "更新时间"]) {
    assert.match(utTasks, new RegExp(`<th scope="col">${label}`), `ut_tasks.html header "${label}" must be scoped`);
  }
  assert.match(utTasks, /<th scope="col" title="[^"]*">覆盖率/);
  assert.match(utTasks, /<th scope="col">#<\/th><th scope="col">来源<\/th><th scope="col">机器判定<\/th>/);

  const dashboard = read("dashboard.html");
  for (const label of ["模型", "作业", "轮次", "输入 token/轮", "用例", "执行时间", "矩阵行"]) {
    assert.match(dashboard, new RegExp(`<th scope="col">${label}<`), `dashboard.html header "${label}" must be scoped`);
  }
});

test("empty action columns still carry scope", () => {
  // The blank header above a row's buttons is still a column header; without
  // scope the cells below it look unlabelled to a screen reader.
  for (const [file, expected] of [
    ["admin.html", 2],
    ["admin_db.html", 1],
    ["ai_drafts.html", 1],
    ["fields.html", 2],
    ["home.html", 2],
    ["project_tasks.html", 1],
    ["ut_pipeline.html", 1],
    ["ut_tasks.html", 2],
  ]) {
    const html = read(file);
    const empty = html.match(/<th scope="col"[^>]*>\s*<\/th>/g) || [];
    assert.equal(empty.length, expected, `${file} should mark ${expected} empty header cells with scope="col"`);
  }
});