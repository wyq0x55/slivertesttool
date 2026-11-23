"use strict";

/* Regression: the four dialog error boxes were revealed by removing the
 * `hidden` attribute, which assistive tech does not announce. They now follow
 * the signed-in login card (#lm-login-error): role="alert", no `hidden`, a
 * shared `.login-err:empty` collapse rule, and visibility driven by text only.
 * This suite pins the markup, the CSS rule and the handlers' runtime behavior. */

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const ROOT = path.resolve(__dirname, "..", "..");
const read = (relative) => fs.readFileSync(path.join(ROOT, relative), "utf8");
const settle = () => new Promise((resolve) => setImmediate(resolve));

function section(source, start, end) {
  const from = source.indexOf(start);
  assert.notEqual(from, -1, `missing section start: ${start}`);
  const to = source.indexOf(end, from);
  assert.notEqual(to, -1, `missing section end: ${end}`);
  return source.slice(from, to);
}

const ALERTS = [
  { file: "app/templates/lanmatrix/admin.html", id: "lm-user-error" },
  { file: "app/templates/lanmatrix/fields.html", id: "lm-f-error" },
  { file: "app/templates/lanmatrix/projects.html", id: "lm-np-error" },
  { file: "app/templates/lanmatrix/projects.html", id: "lm-ep-error" },
];

// --- element shim -----------------------------------------------------------
// `hidden` is an accessor so every write is recorded; the runtime tests then
// fail if a handler toggles it again instead of writing text.
function makeElement(id) {
  const listeners = new Map();
  const node = {
    id,
    textContent: "",
    value: "",
    checked: false,
    disabled: false,
    hiddenWrites: [],
    showModal() {},
    close() {},
    addEventListener(type, fn) {
      if (!listeners.has(type)) listeners.set(type, []);
      listeners.get(type).push(fn);
    },
    dispatch(type) {
      const event = {
        type,
        target: node,
        defaultPrevented: false,
        preventDefault() { this.defaultPrevented = true; },
      };
      return Promise.all((listeners.get(type) || []).map((fn) => fn(event)));
    },
  };
  Object.defineProperty(node, "hidden", {
    get() { return false; },
    set(value) { node.hiddenWrites.push(value); },
  });
  return node;
}

function makeDocument() {
  const nodes = new Map();
  return {
    nodes,
    node(id) {
      if (!nodes.has(id)) nodes.set(id, makeElement(id));
      return nodes.get(id);
    },
    document: {
      getElementById(id) {
        if (!nodes.has(id)) nodes.set(id, makeElement(id));
        return nodes.get(id);
      },
    },
  };
}

function run(script, prelude, globals) {
  const page = makeDocument();
  const sandbox = Object.assign({ document: page.document, console }, globals);
  sandbox.window = sandbox;
  vm.createContext(sandbox);
  vm.runInContext(`${prelude}\n${script}`, sandbox, { filename: "dialog-handler.js" });
  return { page, sandbox };
}

const rejectToken = () => Promise.reject(new Error("令牌已失效"));

// --- markup -----------------------------------------------------------------
test("the four dialog error boxes are live alert regions", () => {
  for (const { file, id } of ALERTS) {
    const html = read(file);
    const occurrences = html.match(new RegExp(`id="${id}"`, "g")) || [];
    assert.equal(occurrences.length, 1, `${id} must appear exactly once in ${file}`);

    const tag = html.match(new RegExp(`<div\\b[^>]*\\bid="${id}"[^>]*>`));
    assert.ok(tag, `missing markup for #${id} in ${file}`);
    assert.match(tag[0], /\bclass="[^"]*\blogin-err\b[^"]*"/, `#${id} keeps the login-err class`);
    assert.match(tag[0], /\brole="alert"/, `#${id} must be a live alert region`);
    assert.doesNotMatch(tag[0], /\bhidden\b/, `#${id} must not start hidden`);
  }
});

test("no dialog handler reveals its error box through the hidden property", () => {
  const scripts = [
    "app/static/js/lanmatrix/admin_console.js",
    "app/static/js/lanmatrix/fields.js",
    "app/static/js/lanmatrix/projects.js",
  ].map(read).join("\n");
  for (const { id } of ALERTS) {
    const lines = scripts.split("\n").filter((line) => line.includes(id));
    assert.ok(lines.length > 0, `expected handlers referencing #${id}`);
    for (const line of lines) {
      assert.doesNotMatch(line, /\bhidden\b/, `#${id} still handled via hidden: ${line.trim()}`);
    }
  }
});

test("an empty dialog error box collapses instead of painting an empty bar", () => {
  const css = read("app/static/css/design-system.css");
  assert.match(
    css,
    /\.login-err:empty\s*\{[^}]*margin-top\s*:\s*0[^}]*padding\s*:\s*0[^}]*background\s*:\s*transparent[^}]*\}/,
    "design-system.css must collapse .login-err while it has no text",
  );
});

// --- runtime ----------------------------------------------------------------
test("admin user dialog writes error text and never toggles hidden", async () => {
  const source = read("app/static/js/lanmatrix/admin_console.js");
  const script = section(
    source,
    '  const dlg = $("lm-user-dialog");',
    "  async function delUser(id) {",
  );
  const { page } = run(script, 'function $(id) { return document.getElementById(id); }', {
    LMApi: { adminCreateUser: rejectToken, adminUpdateUser: rejectToken },
    toast() {},
    loadUsers() {},
    LMUI: { confirm: () => Promise.resolve(false) },
  });

  const err = page.node("lm-user-error");
  err.textContent = "旧错误";
  await page.node("lm-user-new").dispatch("click");
  assert.equal(err.textContent, "", "opening the dialog clears stale error text");

  await page.node("lm-user-ok").dispatch("click");
  assert.equal(err.textContent, "令牌已失效");
  assert.deepEqual(err.hiddenWrites, [], "the error box must not be revealed via hidden");
});

test("field dialog writes error text and never toggles hidden", async () => {
  const source = read("app/static/js/lanmatrix/fields.js");
  const prelude = [
    'function $(id) { return document.getElementById(id); }',
    'const dialog = document.getElementById("lm-field-dialog");',
    'const pid = "proj-1";',
    "let editingId = null;",
  ].join("\n");
  const reset = section(source, "  function resetDialog(mode, field) {", "\n  async function load() {");
  const submit = section(
    source,
    '  $("lm-f-ok").addEventListener("click"',
    "\n  initDragSort();",
  );
  const { page, sandbox } = run(`${reset}\n${submit}`, prelude, {
    LMApi: { addField: rejectToken, patchField: rejectToken },
    toast() {},
    load() {},
  });

  const err = page.node("lm-f-error");
  err.textContent = "旧错误";
  vm.runInContext('resetDialog("create", null)', sandbox);
  assert.equal(err.textContent, "", "opening the dialog clears stale error text");

  await page.node("lm-f-ok").dispatch("click");
  assert.equal(err.textContent, "令牌已失效");
  assert.deepEqual(err.hiddenWrites, [], "the error box must not be revealed via hidden");
});

test("new project dialog writes error text and never toggles hidden", async () => {
  const source = read("app/static/js/lanmatrix/projects.js");
  const prelude = 'const dialog = document.getElementById("lm-new-dialog");';
  const script = section(
    source,
    "  /* --- new project dialog --- */",
    "  /* --- import as new --- */",
  );
  const { page } = run(script, prelude, {
    LMApi: { createProject: rejectToken, patchProject: rejectToken },
    toast() {},
    load() {},
    all: [],
    render() {},
    LM: { urls: { login: "/lanmatrix/login" } },
  });

  const err = page.node("lm-np-error");
  err.textContent = "旧错误";
  await page.node("lm-new-project").dispatch("click");
  assert.equal(err.textContent, "", "opening the dialog clears stale error text");

  await page.node("lm-np-ok").dispatch("click");
  assert.equal(err.textContent, "令牌已失效");
  assert.deepEqual(err.hiddenWrites, [], "the error box must not be revealed via hidden");
});

test("edit project dialog writes client and server errors without toggling hidden", async () => {
  const source = read("app/static/js/lanmatrix/projects.js");
  const prelude = 'const dialog = document.getElementById("lm-new-dialog");';
  const script = section(
    source,
    "  /* --- new project dialog --- */",
    "  /* --- import as new --- */",
  );
  const calls = [];
  const { page, sandbox } = run(script, prelude, {
    LMApi: {
      createProject: rejectToken,
      patchProject(...args) { calls.push(args); return rejectToken(); },
    },
    toast() {},
    load() {},
    all: [],
    render() {},
    LM: { urls: { login: "/lanmatrix/login" } },
  });

  const err = page.node("lm-ep-error");
  err.textContent = "旧错误";
  vm.runInContext('openEdit({ id: 7, name: "存量项目", description: "", status: "draft" })', sandbox);
  assert.equal(err.textContent, "", "opening the dialog clears stale error text");

  page.node("lm-ep-name").value = "   ";
  await page.node("lm-ep-ok").dispatch("click");
  assert.equal(err.textContent, "项目名称不能为空");
  assert.equal(calls.length, 0, "an empty name must not hit the server");

  page.node("lm-ep-name").value = "新名称";
  await page.node("lm-ep-ok").dispatch("click");
  await settle();
  assert.equal(err.textContent, "令牌已失效");
  assert.deepEqual(err.hiddenWrites, [], "the error box must not be revealed via hidden");
});