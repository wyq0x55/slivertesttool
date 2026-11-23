/* The project list's status filter is a filter group, not a tab widget: each
 * button re-filters the same grid, there is no tabpanel and no roving
 * arrow-key focus. The markup therefore exposes role="group" + aria-pressed,
 * mirroring #lm-ai-f-seg and #lm-f-seg, and this test runs the REAL
 * projects.js to prove the script keeps pressed state and filtering in sync.
 */
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const template = fs.readFileSync(
  path.join(__dirname, "../../app/templates/lanmatrix/projects.html"), "utf8",
);
const source = fs.readFileSync(
  path.join(__dirname, "../../app/static/js/lanmatrix/projects.js"), "utf8",
);
const settle = () => new Promise((resolve) => setImmediate(resolve));

const attr = (tag, name) => {
  const m = tag.match(new RegExp(`(?:^|\\s)${name}="([^"]*)"`));
  return m ? m[1] : null;
};

/* The segmented block exactly as it ships, so the buttons the script drives
 * are the buttons the template renders. */
function segBlock() {
  const start = template.indexOf('<div class="segmented" id="lm-proj-seg"');
  assert.notEqual(start, -1, "the project status filter group must exist");
  const end = template.indexOf("</div>", start);
  return template.slice(start, end + "</div>".length);
}

/* One string per button, opening tag through closing tag. */
function markupButtons() {
  return [...segBlock().matchAll(/<button\b[^>]*>[^<]*<\/button>/g)].map((m) => m[0]);
}

/* The smallest element that satisfies the code projects.js actually runs. */
function element(tagName, initialClasses = []) {
  const classes = new Set(initialClasses);
  return {
    tagName,
    className: initialClasses.join(" "),
    dataset: {},
    attributes: {},
    children: [],
    listeners: {},
    hidden: false,
    value: "",
    textContent: "",
    innerHTML: "",
    files: [],
    classList: {
      add(...names) { names.forEach((name) => classes.add(name)); },
      remove(...names) { names.forEach((name) => classes.delete(name)); },
      contains(name) { return classes.has(name); },
      toggle(name, force) {
        const selected = force === undefined ? !classes.has(name) : !!force;
        if (selected) classes.add(name); else classes.delete(name);
        return selected;
      },
    },
    setAttribute(name, value) { this.attributes[name] = String(value); },
    getAttribute(name) {
      return Object.prototype.hasOwnProperty.call(this.attributes, name)
        ? this.attributes[name] : null;
    },
    append(...nodes) { this.children.push(...nodes); },
    replaceChildren(...nodes) { this.children = nodes; },
    addEventListener(type, handler) { this.listeners[type] = handler; },
    querySelectorAll() { return []; },
    closest() { return null; },
    click() {},
    showModal() { this.open = true; },
    close() { this.open = false; },
  };
}

/* Buttons rebuilt from the template markup, carrying its initial attributes. */
function templateButtonNodes() {
  return markupButtons().map((segment) => {
    const open = segment.match(/^<button\b[^>]*>/)[0];
    const classes = (attr(open, "class") || "").split(/\s+/).filter(Boolean);
    const node = element("button", classes);
    const status = attr(open, "data-s");
    node.dataset.s = status;
    // Real DOM mirrors data-* attributes in both dataset and getAttribute();
    // projects.js reads the latter when it decides filter.s.
    node.setAttribute("data-s", status);
    for (const name of ["type", "aria-pressed"]) {
      const value = attr(open, name);
      if (value !== null) node.setAttribute(name, value);
    }
    node.closest = (selector) => (selector === "button[data-s]" ? node : null);
    return node;
  });
}

const projects = [
  { id: 1, code: "ALPHA", name: "Alpha", status: "active" },
  { id: 2, code: "DRAFT1", name: "Draft One", status: "draft" },
  { id: 3, code: "BETA", name: "Beta", status: "active" },
  { id: 4, code: "OLD", name: "Old", status: "archived" },
];

function harness() {
  const ids = new Set([...template.matchAll(/\bid="([^"]+)"/g)].map((m) => m[1]));
  ids.add("lm-toast");
  const nodes = new Map([...ids].map((id) => [id, element("div")]));
  const buttons = templateButtonNodes();
  nodes.get("lm-proj-seg").querySelectorAll =
    (selector) => (selector === "button" ? buttons : []);

  const context = vm.createContext({
    window: { location: {} },
    document: {
      getElementById: (id) => nodes.get(id) || null,
      querySelector: () => null,
      querySelectorAll: () => [],
      createElement: (tagName) => element(tagName),
      createElementNS: (namespace, tagName) => element(tagName),
      createTextNode: (text) => ({ textContent: String(text), nodeType: 3 }),
      addEventListener() {},
    },
    setTimeout() {},
    clearTimeout() {},
    LMPill: {
      PROJECT_ZH: { draft: "草稿", active: "进行中", frozen: "冻结", archived: "归档" },
    },
    LM: { urls: { login: "/lanmatrix/login" } },
    LMUI: { confirm: async () => false },
    LMApi: {
      async listProjects() { return { projects }; },
      async deleteProject() {},
    },
  });

  vm.runInContext(source, context, { filename: "projects.js" });

  return {
    get: (id) => nodes.get(id),
    buttons,
    grid: nodes.get("lm-project-rows"),
    click(index) {
      nodes.get("lm-proj-seg").listeners.click({ target: buttons[index] });
    },
    clickStatus(status) {
      this.click(buttons.findIndex((b) => b.dataset.s === status));
    },
  };
}

function pressed(buttons) {
  return buttons.filter((b) => b.getAttribute("aria-pressed") === "true");
}
function active(buttons) {
  return buttons.filter((b) => b.classList.contains("on"));
}

test("filter markup is a named group of toggle buttons with one pressed", () => {
  const groupTag = segBlock().match(/^<div\b[^>]*>/)[0];
  assert.equal(attr(groupTag, "id"), "lm-proj-seg");
  assert.equal(attr(groupTag, "class"), "segmented");
  assert.equal(attr(groupTag, "role"), "group");
  assert.equal(attr(groupTag, "aria-label"), "按项目状态筛选");

  const buttons = markupButtons();
  assert.equal(buttons.length, 5);
  for (const segment of buttons) {
    const open = segment.match(/^<button\b[^>]*>/)[0];
    assert.equal(attr(open, "type"), "button", `missing type=button: ${open}`);
    assert.notEqual(attr(open, "aria-pressed"), null, `missing aria-pressed: ${open}`);
  }

  // Visible text, data-s values and order stay exactly as before.
  assert.deepEqual(
    buttons.map((s) => s.match(/>([^<]*)<\/button>/)[1]),
    ["全部", "进行中", "草稿", "冻结", "归档"],
  );
  assert.deepEqual(buttons.map((s) => attr(s, "data-s")),
    ["all", "active", "draft", "frozen", "archived"]);

  // Only 全部 starts selected.
  assert.equal(segBlock().match(/aria-pressed="true"/g).length, 1);
  assert.equal(segBlock().match(/class="on"/g).length, 1);
  assert.equal(attr(buttons[0], "aria-pressed"), "true");
  for (const segment of buttons.slice(1)) {
    assert.equal(attr(segment, "aria-pressed"), "false");
  }
});

test("every click leaves exactly one pressed button and re-filters the grid", async () => {
  const h = harness();
  await settle();

  // Initial load: the template's 全部 selection matches filter.s and renders all.
  assert.deepEqual(active(h.buttons).map((b) => b.dataset.s), ["all"]);
  assert.deepEqual(pressed(h.buttons).map((b) => b.dataset.s), ["all"]);
  assert.equal(h.grid.children.length, projects.length);

  for (const status of ["active", "draft", "frozen", "archived", "all"]) {
    h.clickStatus(status);

    const on = active(h.buttons);
    const down = pressed(h.buttons);
    assert.equal(on.length, 1, `${status}: exactly one .on button`);
    assert.equal(down.length, 1, `${status}: exactly one aria-pressed=true`);
    assert.equal(on[0], down[0], `${status}: .on and aria-pressed agree`);
    assert.equal(on[0].dataset.s, status, `${status}: the clicked button wins`);

    const expected = status === "all"
      ? projects
      : projects.filter((p) => p.status === status);

    if (expected.length) {
      assert.equal(h.grid.hidden, false);
      assert.equal(h.grid.children.length, expected.length);
      // The cards the real render() produced carry the filter status, proving
      // the pushed data-s value reached the filter path.
      assert.deepEqual(
        h.grid.children.map((card) => card.dataset.status),
        expected.map((p) => p.status),
      );
    } else {
      // No frozen projects: the real render() shows its no-match state.
      assert.equal(h.grid.children.length, 1);
      assert.equal(h.grid.children[0].className, "state");
      assert.equal(h.grid.children[0].children[1].textContent, "没有匹配的项目");
    }
  }
});