// The UT pipeline's header checkbox must paint three states -- none, mixed,
// all -- over the selectable rows the current filter lets through. render()
// used to write only .checked, so a partial selection rendered as an empty box
// and a stale mixed state could survive a full selection. These tests drive
// the real script with the template's own ids and assert the DOM property pair
// after row changes, filtering, searching, select-all and refresh.
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const scripts = path.resolve(__dirname, "../../app/static/js/lanmatrix");
const templates = path.resolve(__dirname, "../../app/templates/lanmatrix");

function settle(rounds = 12) {
  let chain = Promise.resolve();
  for (let i = 0; i < rounds; i += 1) {
    chain = chain.then(() => new Promise((resolve) => setImmediate(resolve)));
  }
  return chain;
}

function makeNode(id) {
  return {
    id,
    value: "",
    checked: false,
    indeterminate: false,
    hidden: true,
    disabled: false,
    innerHTML: "",
    textContent: "",
    tabIndex: 0,
    dataset: {},
    options: [],
    attributes: {},
    listeners: {},
    classList: { toggle() {}, add() {}, remove() {} },
    setAttribute(name, value) { this.attributes[name] = String(value); },
    removeAttribute(name) { delete this.attributes[name]; },
    contains() { return false; },
    closest() { return null; },
    querySelectorAll() { return []; },
    focus() {},
    replaceChildren() { this.innerHTML = ""; },
    addEventListener(event, handler) {
      (this.listeners[event] || (this.listeners[event] = [])).push(handler);
    },
  };
}

function loadTemplateNodes(file) {
  const html = fs.readFileSync(path.join(templates, file), "utf8");
  const nodes = new Map();
  for (const match of html.matchAll(/\bid="([^"]+)"/g)) {
    if (!nodes.has(match[1])) nodes.set(match[1], makeNode(match[1]));
  }
  return nodes;
}

function escapeHtml(value) {
  return String(value)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

// One selectable target per interesting state: plain, approved, stale, and
// in-flight. Stale and in-flight rows stay visible but cannot be selected.
function fixtureTargets() {
  function target(id, { state = null, inIndex = true } = {}) {
    return {
      id,
      row_no: id,
      function: "fn_" + id,
      source_rel: "src/fn_" + id + ".c",
      source_name: "fn_" + id + ".c",
      in_index: inIndex,
      latest_job: state
        ? { id: 900 + id, state, last_verdict: "", repair_rounds: 0 }
        : null,
    };
  }
  return [
    target(1),
    target(2),
    target(3, { state: "approved" }),
    target(4, { inIndex: false }),
    target(5, { state: "queued" }),
  ];
}

async function loadPipeline({ targets = fixtureTargets(), canEdit = true } = {}) {
  const nodes = new Map([
    ...loadTemplateNodes("_ut_filters.html"),
    ...loadTemplateNodes("ut_pipeline.html"),
  ]);
  const api = {
    targets,
    async utTargets() { return this.targets; },
    async utGetConfig() { return { manifest_path: "project.yaml" }; },
  };
  const context = vm.createContext({
    URLSearchParams,
    console,
    document: {
      hidden: false,
      getElementById: (id) => nodes.get(id) || null,
      querySelector: (selector) => (selector === ".lm-ut" ? { dataset: { project: "42" } } : null),
      addEventListener() {},
    },
    addEventListener() {},
    setTimeout: () => 1,
    clearTimeout() {},
    setInterval: () => 1,
    clearInterval() {},
    LM: { projectCapabilities: { edit: canEdit } },
    LMUrl: { get: () => "", replace() {}, onPop() {} },
    LMApi: api,
    LMPill: {
      esc: escapeHtml,
      html: (className, label) => '<span class="' + className + '">' + escapeHtml(label) + "</span>",
      apply(node, className, label) { if (node) node.textContent = label; },
    },
  });
  context.window = context;
  for (const file of ["ut_common.js", "ut_pipeline.js"]) {
    vm.runInContext(fs.readFileSync(path.join(scripts, file), "utf8"), context, { filename: file });
  }
  await settle();
  return { nodes, api };
}

function fire(node, event, extra) {
  const payload = Object.assign({ preventDefault() {} }, extra);
  (node.listeners[event] || []).forEach((handler) => handler.call(node, payload));
}

// Same handler the row table listens on; a real click sets .checked first.
function toggleRow(nodes, id, checked) {
  fire(nodes.get("ut-rows"), "change", {
    target: { dataset: { target: String(id) }, checked },
  });
}

function chooseFilter(nodes, filter) {
  fire(nodes.get("ut-filter"), "click", {
    target: {
      closest: (selector) => (selector === "button[data-filter]" ? { dataset: { filter } } : null),
    },
  });
}

function search(nodes, value) {
  const input = nodes.get("ut-q");
  input.value = value;
  fire(input, "input");
}

async function refresh(nodes) {
  fire(nodes.get("ut-refresh"), "click");
  await settle();
}

function rowIds(nodes) {
  return [...nodes.get("ut-rows").innerHTML.matchAll(/data-target="(\d+)"/g)].map((m) => Number(m[1]));
}

function assertHeader(nodes, checked, indeterminate) {
  const box = nodes.get("ut-all");
  assert.equal(box.checked, checked, "header .checked");
  assert.equal(box.indeterminate, indeterminate, "header .indeterminate");
}

test("header checkbox reports none, partial and all over selectable rows", async () => {
  const { nodes } = await loadPipeline();
  const label = nodes.get("ut-enqueue-label");
  const enqueue = nodes.get("ut-enqueue");

  assertHeader(nodes, false, false);
  assert.equal(label.textContent, "加入队列");
  assert.equal(enqueue.disabled, true);

  toggleRow(nodes, 1, true);
  assertHeader(nodes, false, true);
  assert.equal(label.textContent, "加入队列（1）");
  assert.equal(enqueue.disabled, false);
  assert.match(nodes.get("ut-rows").innerHTML, /data-target="1" checked/);

  toggleRow(nodes, 2, true);
  assertHeader(nodes, false, true);
  assert.equal(label.textContent, "加入队列（2）");

  toggleRow(nodes, 3, true);
  assertHeader(nodes, true, false);
  assert.equal(label.textContent, "加入队列（3）");

  toggleRow(nodes, 1, false);
  assertHeader(nodes, false, true);
  assert.equal(label.textContent, "加入队列（2）");
  assert.doesNotMatch(nodes.get("ut-rows").innerHTML, /data-target="1" checked/);
});

test("stale and in-flight rows stay out of the header arithmetic", async () => {
  const { nodes } = await loadPipeline();

  assert.deepEqual(rowIds(nodes), [1, 2, 3, 4, 5]);
  assert.match(nodes.get("ut-rows").innerHTML, /data-target="4" disabled/);
  assert.match(nodes.get("ut-rows").innerHTML, /data-target="5" disabled/);
  assert.doesNotMatch(nodes.get("ut-rows").innerHTML, /data-target="1" disabled/);

  toggleRow(nodes, 1, true);
  toggleRow(nodes, 2, true);
  toggleRow(nodes, 3, true);
  // Disabled boxes cannot dispatch change in a browser, so selecting every
  // selectable row must already read as fully selected.
  assertHeader(nodes, true, false);
  assert.equal(nodes.get("ut-enqueue-label").textContent, "加入队列（3）");
  assert.doesNotMatch(nodes.get("ut-rows").innerHTML, /data-target="4" checked/);
  assert.doesNotMatch(nodes.get("ut-rows").innerHTML, /data-target="5" checked/);

  // A filter that only surfaces a non-selectable row leaves no scope at all.
  chooseFilter(nodes, "active");
  assert.deepEqual(rowIds(nodes), [5]);
  assertHeader(nodes, false, false);

  chooseFilter(nodes, "stopped");
  assert.deepEqual(rowIds(nodes), []);
  assert.match(nodes.get("ut-rows").innerHTML, /没有符合条件的函数/);
  assertHeader(nodes, false, false);
});

test("filtering narrows the header scope while keeping the selection", async () => {
  const { nodes } = await loadPipeline();

  toggleRow(nodes, 1, true);
  toggleRow(nodes, 2, true);
  toggleRow(nodes, 3, true);
  assertHeader(nodes, true, false);

  chooseFilter(nodes, "none");
  assert.deepEqual(rowIds(nodes), [1, 2, 4]);
  assertHeader(nodes, true, false);

  toggleRow(nodes, 2, false);
  assertHeader(nodes, false, true);
  assert.equal(nodes.get("ut-enqueue-label").textContent, "加入队列（2）");

  chooseFilter(nodes, "approved");
  assert.deepEqual(rowIds(nodes), [3]);
  assertHeader(nodes, true, false);

  chooseFilter(nodes, "");
  assertHeader(nodes, false, true);
  assert.equal(nodes.get("ut-enqueue-label").textContent, "加入队列（2）");

  toggleRow(nodes, 1, false);
  assertHeader(nodes, false, true);
  assert.equal(nodes.get("ut-enqueue-label").textContent, "加入队列（1）");

  chooseFilter(nodes, "none");
  assertHeader(nodes, false, false);
  // The scope is empty but the selection is not: only a refresh prunes it.
  assert.equal(nodes.get("ut-enqueue-label").textContent, "加入队列（1）");
});

test("searching narrows the header scope and an empty result clears it", async () => {
  const { nodes } = await loadPipeline();

  toggleRow(nodes, 1, true);
  toggleRow(nodes, 2, true);
  toggleRow(nodes, 3, true);
  assertHeader(nodes, true, false);

  search(nodes, "fn_2");
  assert.deepEqual(rowIds(nodes), [2]);
  assertHeader(nodes, true, false);

  search(nodes, "zzz");
  assert.deepEqual(rowIds(nodes), []);
  assertHeader(nodes, false, false);

  search(nodes, "");
  assertHeader(nodes, true, false);
});

test("select-all clears a mixed header and select-none rebuilds it", async () => {
  const { nodes } = await loadPipeline();
  const box = nodes.get("ut-all");

  toggleRow(nodes, 1, true);
  assertHeader(nodes, false, true);

  // A click on a mixed box selects everything; render must also drop the
  // dash, even if the element still carries a stale mixed state.
  box.checked = true;
  fire(box, "change");
  assertHeader(nodes, true, false);
  assert.equal(nodes.get("ut-enqueue-label").textContent, "加入队列（3）");

  box.checked = false;
  fire(box, "change");
  assertHeader(nodes, false, false);
  assert.equal(nodes.get("ut-enqueue-label").textContent, "加入队列");
  assert.doesNotMatch(nodes.get("ut-rows").innerHTML, / checked/);
});

test("a refresh that drops rows prunes the selection and the header", async () => {
  const { nodes, api } = await loadPipeline();

  toggleRow(nodes, 1, true);
  toggleRow(nodes, 2, true);
  toggleRow(nodes, 3, true);
  assertHeader(nodes, true, false);

  api.targets = [];
  await refresh(nodes);
  assert.deepEqual(rowIds(nodes), []);
  assertHeader(nodes, false, false);
  assert.equal(nodes.get("ut-enqueue-label").textContent, "加入队列");

  api.targets = fixtureTargets();
  await refresh(nodes);
  assert.deepEqual(rowIds(nodes), [1, 2, 3, 4, 5]);
  assertHeader(nodes, false, false);

  toggleRow(nodes, 3, true);
  assertHeader(nodes, false, true);
});

test("without edit capability nothing is selectable", async () => {
  const { nodes } = await loadPipeline({ canEdit: false });
  const html = nodes.get("ut-rows").innerHTML;

  for (const id of [1, 2, 3, 4, 5]) {
    assert.match(html, new RegExp('data-target="' + id + '" disabled'));
  }
  // Nothing is selectable without edit capability, so the header stays empty
  // even when the rendered rows all exist.
  assertHeader(nodes, false, false);
  assert.equal(nodes.get("ut-enqueue-label").textContent, "加入队列");
});