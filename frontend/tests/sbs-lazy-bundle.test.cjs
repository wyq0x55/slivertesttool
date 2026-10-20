const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const root = path.resolve(__dirname, "../..");
const source = fs.readFileSync(path.join(root, "app/static/js/lanmatrix/sbs_editor.js"), "utf8");
const first = source.indexOf("let bundleLoading");
const last = source.indexOf("// Editor abstraction", first);
assert.ok(first >= 0 && last > first, "production section must exist");
const LOADER = source.slice(first, last);

function page(assets) {
  const scripts = [];
  const global = {};
  const document = {
    createElement: () => ({}),
    head: { appendChild(node) { scripts.push(node); } },
  };
  const context = vm.createContext({ global, document, assets });
  vm.runInContext(`${LOADER}; global.load = loadBundle;`, context);
  return { global, scripts };
}

test("without a bundle URL nothing is requested and the textarea path proceeds", async () => {
  const env = page({});
  await env.global.load();
  assert.equal(env.scripts.length, 0);
});

test("the bundle is requested once and every open waits for the same load", async () => {
  const env = page({ bundle: "/static/vendor/sbs/sbs-editor.umd.js?v=1" });
  const opens = [env.global.load(), env.global.load()];
  assert.equal(opens[0], opens[1]);
  assert.equal(env.scripts.length, 1);
  assert.equal(env.scripts[0].src, "/static/vendor/sbs/sbs-editor.umd.js?v=1");
  env.global.LMSbsEditor = { mount() {} };
  env.scripts[0].onload();
  await Promise.all(opens);
  await env.global.load();
  assert.equal(env.scripts.length, 1);
});

test("a bundle that fails to load still lets the modal mount its textarea", async () => {
  const env = page({ bundle: "/static/vendor/sbs/sbs-editor.umd.js?v=1" });
  const loading = env.global.load();
  env.scripts[0].onerror();
  await loading;
  assert.equal(env.global.LMSbsEditor, undefined);
});
