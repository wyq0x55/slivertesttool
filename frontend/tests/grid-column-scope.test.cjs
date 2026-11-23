const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const rootDir = path.resolve(__dirname, "../..");
const gridPath = path.join(rootDir, "app/static/js/lanmatrix/grid.js");
const adminDbPath = path.join(rootDir, "app/static/js/lanmatrix/admin_db.js");
const aiDraftsPath = path.join(rootDir, "app/static/js/lanmatrix/ai_drafts.js");
const taskCurvesPath = path.join(rootDir, "app/static/js/lanmatrix/task_curves.js");
const auditPath = path.join(rootDir, "app/static/js/lanmatrix/audit.js");

function read(file) {
  return fs.readFileSync(file, "utf8");
}

// grid.js is an IIFE over `window`; run it for real so render() builds the
// markup instead of asserting on source text. The host stub captures
// innerHTML and returns no cells, which is all render() needs: the header
// strings exist before _bind()/selection painting touches the DOM.
function loadGrid() {
  function makeElement(tag) {
    return {
      tagName: String(tag).toUpperCase(),
      className: "",
      style: { position: "" },
      children: [],
      appendChild() {},
      replaceChildren() {},
      remove() {},
      setAttribute() {},
      addEventListener() {},
    };
  }
  const host = {
    _html: "",
    style: {},
    set innerHTML(value) { this._html = value; },
    get innerHTML() { return this._html; },
    querySelector() { return null; },
    querySelectorAll() { return []; },
    appendChild() {},
    replaceChildren() {},
  };
  const sandbox = {
    document: {
      addEventListener() {},
      createElement: makeElement,
      getElementById() { return null; },
    },
    getComputedStyle() { return { position: "static" }; },
    console,
    addEventListener() {},
  };
  sandbox.window = sandbox;
  vm.createContext(sandbox);
  vm.runInContext(read(gridPath), sandbox, { filename: gridPath });
  return { host, LMGrid: sandbox.LMGrid };
}

function theadHtml(html) {
  const m = /<thead><tr>([\s\S]*?)<\/tr><\/thead>/.exec(html);
  assert.ok(m, "grid render() should emit a thead row");
  return m[1];
}

function thTags(html) {
  return html.match(/<th\b[^>]*>/g) || [];
}

test("grid field header cells carry scope=col and keep title/data-key/text", () => {
  const { host, LMGrid } = loadGrid();
  const fields = [
    {
      field_key: "title",
      display_name: "标题",
      help_text: 'say "hi" <b>',
      is_required: true,
    },
    {
      field_key: "status",
      display_name: "状态",
      help_text: "",
    },
    {
      field_key: "hidden",
      display_name: "隐藏",
      help_text: "",
      is_active: false,
    },
  ];
  const grid = new LMGrid.FallbackGrid({ host, fields, onDelete() {} });
  grid.render();
  const head = theadHtml(host.innerHTML);

  assert.ok(
    head.includes(
      '<th scope="col" title="say &quot;hi&quot; &lt;b&gt;" data-key="title">' +
        '标题<span class="lm-req">*</span></th>'
    ),
    "required field header keeps title, data-key, escaping and content"
  );
  assert.ok(
    head.includes(
      '<th scope="col" title="" data-key="status">状态</th>'
    ),
    "plain field header keeps its attributes"
  );
  assert.ok(!head.includes("隐藏"), "inactive fields stay hidden");
});

test("grid selection, row-number and action headers carry scope=col", () => {
  const { host, LMGrid } = loadGrid();
  const grid = new LMGrid.FallbackGrid({
    host,
    fields: [{ field_key: "a", display_name: "A" }],
    onDelete() {},
  });
  grid.render();
  const head = theadHtml(host.innerHTML);

  // The checkbox never had a name attribute; asserting the exact markup pins
  // type/class/title and keeps the absence of `name` unchanged as well.
  assert.ok(
    head.includes(
      '<th scope="col" class="lm-selcol"><input type="checkbox" ' +
        'class="lm-sel-all" title="全选/取消"></th>'
    ),
    "selection header keeps its checkbox untouched"
  );
  assert.ok(
    head.includes('<th scope="col" class="lm-rownum">#</th>'),
    "row-number header is scoped"
  );
  assert.ok(
    head.includes('<th scope="col" class="lm-actcol">操作</th>'),
    "action header is scoped"
  );
});

test("every grid header cell declares a column scope; actions stay optional", () => {
  const { host, LMGrid } = loadGrid();
  const withActions = new LMGrid.FallbackGrid({
    host,
    fields: [
      { field_key: "a", display_name: "A" },
      { field_key: "b", display_name: "B" },
    ],
    onDelete() {},
  });
  withActions.render();
  const head = theadHtml(host.innerHTML);
  const tags = thTags(head);
  assert.equal(tags.length, 5, "sel + rownum + 2 fields + action");
  for (const tag of tags) {
    assert.ok(tag.startsWith('<th scope="col"'), `unscoped header: ${tag}`);
  }

  const plain = new LMGrid.FallbackGrid({
    host,
    fields: [{ field_key: "a", display_name: "A" }],
  });
  plain.render();
  const plainHead = theadHtml(host.innerHTML);
  assert.ok(!plainHead.includes("lm-actcol"), "no action column without actions");
  for (const tag of thTags(plainHead)) {
    assert.ok(tag.startsWith('<th scope="col"'), `unscoped header: ${tag}`);
  }
});

test("admin_db ad-hoc result header scopes every column", () => {
  const source = read(adminDbPath);
  const line = source
    .split("\n")
    .find((l) => l.includes("head.innerHTML = `<tr>${(d.columns"));
  assert.ok(line, "result-header builder line should still exist");

  const head = { innerHTML: "" };
  const d = { columns: ["id", "name"] };
  const esc = (value) => String(value);
  vm.runInNewContext(line.trim(), { head, d, esc });
  assert.equal(
    head.innerHTML,
    '<tr><th scope="col">id</th><th scope="col">name</th></tr>'
  );
});

test("ai_drafts usage tables scope every header on both tables", () => {
  const lines = read(aiDraftsPath).split("\n");
  const scenario = lines.find((l) => l.includes("<th scope=\"col\">场景</th>"));
  const month = lines.find((l) => l.includes("<th scope=\"col\">月份</th>"));
  assert.ok(scenario, "scenario table header line");
  assert.ok(month, "month table header line");

  assert.ok(
    scenario.includes(
      '<th scope="col">场景</th><th scope="col">草稿数</th>' +
        '<th scope="col">输入</th><th scope="col">输出</th>'
    )
  );
  assert.ok(
    month.includes(
      '<th scope="col">月份</th><th scope="col">草稿数</th>' +
        '<th scope="col">输入</th><th scope="col">输出</th>'
    )
  );
  for (const line of [scenario, month]) {
    assert.ok(!/<th>/.test(line), `unscoped header remains: ${line}`);
  }
});

test("task_curves raw-data header sets scope before appending", () => {
  const line = read(taskCurvesPath)
    .split("\n")
    .find((l) => l.includes("...page.header"));
  assert.ok(line, "raw-data header builder line should still exist");
  assert.ok(
    line.indexOf('th.scope = "col"') > -1 &&
      line.indexOf('th.scope = "col"') < line.indexOf("tr.append(th)"),
    "scope must be set before the header cell is appended"
  );

  const ths = [];
  const page = { header: ["A", "B"] };
  const tr = { append: (el) => ths.push(el) };
  const document = {
    createElement: (tag) => ({ tagName: tag.toUpperCase(), textContent: "" }),
  };
  vm.runInNewContext(line.trim(), { page, tr, document });
  assert.deepEqual(
    ths.map((th) => [th.tagName, th.scope, th.textContent]),
    [
      ["TH", "col", "记录"],
      ["TH", "col", "A"],
      ["TH", "col", "B"],
    ]
  );
});

test("already-scoped tables keep their scope attributes", () => {
  const adminSource = read(adminDbPath);
  assert.ok(
    adminSource.includes('<th scope="col" class="lm-db-sortable'),
    "admin_db sortable header keeps scope=col"
  );
  assert.ok(
    adminSource.includes('<th scope="col" class="lm-db-opcol">操作</th>'),
    "admin_db operations header keeps scope=col"
  );
  assert.ok(
    read(auditPath).includes('th.scope = "row";'),
    "audit diff table keeps its row scope"
  );
});