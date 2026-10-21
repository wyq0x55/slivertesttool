const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const settle = () => new Promise(resolve => setImmediate(resolve));
function harness(configure = true, config = null) {
  const nodes = new Map(), writes = [];
  function get(id) {
    if (!nodes.has(id)) nodes.set(id, { value: "", disabled: false, hidden: false,
      parentElement: { hidden: false }, listeners: {}, dataset: { project: "4" },
      addEventListener(type, fn) { this.listeners[type] = fn; } });
    return nodes.get(id);
  }
  const context = vm.createContext({ window: { LMReady: Promise.resolve() },
    document: { querySelector: () => get("root"), getElementById: get },
    LMUt: { say() {}, can: () => configure },
    LMApi: { utGetConfig: async () => config, utPutConfig: async (id, values) => writes.push({ id, values }) } });
  vm.runInContext(fs.readFileSync(path.resolve(__dirname, "../../app/static/js/lanmatrix/ut_binding.js"), "utf8"), context);
  return { get, writes, change(key, value) { const node = get("ut-c-" + key); node.value = value; node.listeners.change(); },
    save: () => get("ut-config-save").listeners.click() };
}

test("executor and stub controls follow selected mode while preserving WinAMS paths", async () => {
  const h = harness(true, { work_root: "W", stub_builds: "S" });
  await settle();
  assert.equal(h.get("ut-host-controls").hidden, true);
  assert.equal(h.get("ut-c-stub_model").disabled, true);
  h.change("execution_backend", "clang");
  assert.equal(h.get("ut-winams-controls").hidden, true);
  assert.equal(h.get("ut-c-work_root").disabled, true);
  assert.equal(h.get("ut-csv-repair-heading").parentElement.hidden, true);
  assert.equal(h.get("ut-host-controls").hidden, false);
  h.change("stub_mode", "llm");
  assert.equal(h.get("ut-stub-llm-controls").hidden, false);
  assert.equal(h.get("ut-c-stub_model").disabled, false);
  assert.equal(h.get("ut-c-work_root").value, "W");
  assert.equal(h.get("ut-c-stub_builds").value, "S");
});

test("saving uses numeric round limits and WinAMS normalizes stub mode", async () => {
  const h = harness();
  await settle();
  h.change("execution_backend", "clang");
  h.change("stub_mode", "llm");
  h.get("ut-c-stub_model").value = " local-stub ";
  h.get("ut-c-stub_max_rounds").value = "3";
  await h.save();
  assert.equal(h.writes[0].values.stub_max_rounds, 3);
  assert.equal(h.writes[0].values.stub_model, "local-stub");
  assert.equal(h.writes[0].values.host_clang, "clang");
  h.change("execution_backend", "winams");
  await h.save();
  assert.equal(h.writes[1].values.stub_mode, "deterministic");
});

test("a reader cannot mutate settings even by directly invoking save", async () => {
  const h = harness(false, { execution_backend: "clang", stub_mode: "llm" });
  await settle();
  for (const key of ["execution_backend", "stub_mode", "stub_model", "stub_max_rounds", "host_clang"]) {
    assert.equal(h.get("ut-c-" + key).disabled, true);
  }
  await h.save();
  assert.equal(h.writes.length, 0);
});

test("invalid stub round limit never sends an update", async () => {
  const h = harness();
  await settle();
  h.get("ut-c-stub_max_rounds").value = "6";
  await h.save();
  assert.equal(h.writes.length, 0);
});
