const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const root = path.resolve(__dirname, "../..");
const source = fs.readFileSync(path.join(root, "app/static/js/lanmatrix/steps_editor.js"), "utf8");
const settle = () => new Promise((resolve) => setImmediate(resolve));

function section(start, end) {
  const first = source.indexOf(start);
  const last = source.indexOf(end, first);
  assert.ok(first >= 0 && last > first, "production section must exist");
  return source.slice(first, last);
}

const LOADER = section("let univerLoading", "// Step fields around the signal columns");
const UPGRADE = section("_renderWhenLoaded(item) {", "// Sync the Univer steps sheet");

// A page with an optional <link id="lm-univer-lazy">; appended <script>s are
// recorded so a test decides whether the bundle loads or fails.
function page({ hint = true } = {}) {
  const appended = [];
  const global = {};
  const document = {
    activeElement: null,
    head: { appendChild(node) { appended.push(node); } },
    createElement(tag) { return { tag }; },
    getElementById(id) {
      if (id !== "lm-univer-lazy" || !hint) return null;
      return {
        href: "/static/vendor/univer/univer.full.umd.js?v=abc",
        getAttribute: (name) => (name === "data-css" ? "/static/vendor/univer/univer.full.umd.css?v=abc" : null),
      };
    },
  };
  const timers = [];
  const context = vm.createContext({ global, document, requestAnimationFrame: (callback) => callback(),
    setTimeout: (callback, ms) => timers.push({ callback, ms }) });
  vm.runInContext(`${LOADER}; global.load = loadUniverBundle;
    global.Editor = class { ${UPGRADE} };`, context);
  return { global, document, appended, timers, scripts: () => appended.filter((node) => node.tag === "script") };
}

function editor(env, item) {
  const view = { setDoc: [], refData: null, resized: 0,
    setRefData(data) { this.refData = data; }, resize() { this.resized += 1; } };
  const instance = new env.global.Editor();
  Object.assign(instance, {
    item, view: null, dialog: { open: true }, _refData: { lib: ["SUB"] }, rendered: [],
    host: { innerHTML: "" },
    _ensureView() { this.view = env.global.LMUniverSteps ? view : null; },
    render() { this.rendered.push(this.view ? "univer" : "builtin"); },
  });
  return { instance, view };
}

test("a page without the lazy hint renders the built-in tables at once and loads nothing", () => {
  const env = page({ hint: false });
  assert.equal(env.global.load(), null);
  const { instance } = editor(env, { id: 1 });
  assert.equal(instance._renderWhenLoaded(instance.item), false);
  assert.equal(env.appended.length, 0);
  assert.equal(instance.host.innerHTML, "");
});

test("the bundle and its stylesheet are requested once, however often the drawer opens", () => {
  const env = page();
  const first = env.global.load();
  assert.equal(env.global.load(), first);
  assert.deepEqual(env.appended.map((node) => node.tag), ["link", "script"]);
  assert.equal(env.appended[0].href, "/static/vendor/univer/univer.full.umd.css?v=abc");
  assert.equal(env.scripts()[0].src, "/static/vendor/univer/univer.full.umd.js?v=abc");
});

test("the drawer waits behind a placeholder and renders straight into Univer", async () => {
  const env = page();
  const item = { id: 7 };
  const { instance, view } = editor(env, item);
  assert.equal(instance._renderWhenLoaded(item), true);
  assert.match(instance.host.innerHTML, /正在加载表格编辑器/);
  assert.deepEqual(instance.rendered, []);           // no built-in tables first
  env.global.LMUniverSteps = { mount() {} };
  env.scripts()[0].onload();
  await settle();
  assert.equal(instance.view, view);
  assert.deepEqual(instance.rendered, ["univer"]);
  assert.deepEqual(view.refData, { lib: ["SUB"] });
  assert.equal(view.resized, 1);
  env.timers[0].callback();                          // the late timeout is a no-op
  assert.deepEqual(instance.rendered, ["univer"]);
});

test("a bundle that fails or is too slow falls back to the built-in tables", async () => {
  for (const settleLoad of [(env) => env.scripts()[0].onerror(), (env) => env.timers[0].callback()]) {
    const env = page();
    const item = { id: 7 };
    const { instance } = editor(env, item);
    instance._renderWhenLoaded(item);
    assert.equal(env.timers[0].ms, 15000);
    settleLoad(env);
    await settle();
    assert.equal(instance.view, null);
    assert.deepEqual(instance.rendered, ["builtin"]);
  }
});

for (const [name, change] of [
  ["the drawer was closed", (instance) => { instance.dialog.open = false; }],
  ["another item was opened", (instance) => { instance.item = { id: 8 }; }],
]) {
  test(`nothing renders for a stale wait when ${name}`, async () => {
    const env = page();
    const item = { id: 7 };
    const { instance } = editor(env, item);
    instance._renderWhenLoaded(item);
    change(instance, env);
    env.global.LMUniverSteps = { mount() {} };
    env.scripts()[0].onload();
    await settle();
    assert.equal(instance.view, null);
    assert.deepEqual(instance.rendered, []);
  });
}
