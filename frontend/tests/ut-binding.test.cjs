const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");
const { TextDecoder } = require("node:util");
const settle = () => new Promise(resolve => setImmediate(resolve));
const base = path.resolve(__dirname, "../..");
const clone = value => JSON.parse(JSON.stringify(value));
const manifest = () => ({ project: { id: "demo" }, baseline: { id: "baseline", version: "1" }, build: { profile: "default" } });
const catalog = () => ({
  contract: "ut-agent.configuration-options/2",
  pointer_sizes: [
    { id: "undeclared", pointer_size: null, label: "不声明指针宽度", description: "涉及指针的函数仍会被阻断" },
    { id: "32", pointer_size: 4, label: "32 位指针", description: "只声明 4 字节指针宽度" },
    { id: "64", pointer_size: 8, label: "64 位指针", description: "只声明 8 字节指针宽度" },
  ],
  interface_directions: [
    { id: "in", direction: "in", pointee_access_order: "read_only", label: "输入，只读" },
    { id: "out", direction: "out", pointee_access_order: "write_only", label: "输出，只写" },
    { id: "inout-read", direction: "inout", pointee_access_order: "read_before_write", label: "先读后写" },
    { id: "inout-write", direction: "inout", pointee_access_order: "write_before_read", label: "先写后读" },
  ],
  build_profiles: [
    { id: "default", label: "Default", pointer_size: null, description: "Compiler compatibility only" },
    { id: "typed32", label: "已验证的 32 位配置", pointer_size: 4, description: "Typed ABI evidence" },
  ],
  interface_contracts: [
    { id: "approved@2", label: "已审批的外部接口 · 版本 2", description: "Pointer direction approved",
      functions: ["external_read"], approved_by: "reviewer", approved_at: "2026-10-26" },
  ],
});
function harness(configure = true, config = { manifest_document: manifest() }, settings = {}) {
  const nodes = new Map(), writes = [], validations = [], requests = [], downloads = [], events = {};
  let focused = null, current = config, apiError = null;
  const template = fs.readFileSync(path.join(base, "app/templates/lanmatrix/ut_binding.html"), "utf8");
  function node(id) {
    const attributes = {}, classes = new Set();
    return { id, value: "", disabled: false, hidden: false, readOnly: false, textContent: "", open: false,
      parentElement: { hidden: false }, listeners: {}, dataset: { project: "4" }, children: [],
      replaceChildren() { this.children = []; }, appendChild(child) { this.children.push(child); },
      classList: { toggle(k, on) { on ? classes.add(k) : classes.delete(k); }, contains(k) { return classes.has(k); } },
      addEventListener(type, fn) { this.listeners[type] = fn; },
      setAttribute(k, v) { attributes[k] = v; }, removeAttribute(k) { delete attributes[k]; },
      getAttribute(k) { return attributes[k]; }, focus() { focused = id; },
      click() { if (this.listeners.click) return this.listeners.click.call(this); } };
  }
  for (const [, id] of template.matchAll(/\bid="([^"]+)"/g)) nodes.set(id, node(id));
  nodes.set("root", node("root"));
  function get(id) { assert.ok(nodes.has(id), `Missing template node: ${id}`); return nodes.get(id); }
  const window = { LMReady: Promise.resolve(), addEventListener(type, fn) { events[type] = fn; }, setTimeout(fn) { fn(); } };
  const context = vm.createContext({ window, TextDecoder, Blob, FormData, URLSearchParams,
    URL: { createObjectURL(blob) { downloads.push(blob); return "blob:draft"; }, revokeObjectURL() {} },
    document: { querySelector: () => get("root"), getElementById: id => id.startsWith("lm-") ? null : get(id), addEventListener() {},
      createElement(tag) {
        if (tag === "a") return { click() {} };
        const el = node(tag === "option" ? "option" : "el");
        el.tagName = tag;
        el.remove = function () {
          if (this.parentElement && Array.isArray(this.parentElement.children))
            this.parentElement.children = this.parentElement.children.filter(child => child !== this);
        };
        return el;
      },
      createTextNode(text) { return { textContent: String(text) }; } },
    LMUt: { say() {}, time: value => value, can: () => configure },
    fetch: async (url, opts) => {
      const body = opts.body ? JSON.parse(opts.body) : null;
      requests.push({ url, ...opts, body });
      let data;
      if (url.endsWith("/auth/me")) data = { csrf_token: "csrf-test", user: {} };
      else if (url.endsWith("/manifest/options")) {
        if (settings.catalogError) throw new Error(settings.catalogError);
        data = settings.catalog || catalog();
      }
      else if (opts.method === "GET") data = current;
      else if (url.endsWith("/manifest/validate")) { validations.push(body); data = { sha256: "validated-sha", baseline: "baseline" }; }
      else { writes.push(body); data = current = { ...body, manifest_path: "server/sha.json", manifest_sha256: "saved-sha" }; }
      return { status: apiError ? 422 : 200, json: async () => apiError ? { success: false, error: apiError } : { success: true, data } };
    } });
  for (const filename of ["api.js", "ut_binding.js"]) {
    vm.runInContext(fs.readFileSync(path.join(base, "app/static/js/lanmatrix", filename), "utf8"), context);
    context.LMApi = window.LMApi;
  }
  window.LMApi.setToken("csrf-test");
  function emit(id, type) { const n = get(id); return n.listeners[type].call(n); }
  function input(id, value, type = "input") { get(id).value = value; return emit(id, type); }
  return { get, writes, validations, requests, downloads, events, input, emit,
    change: (key, value) => input("ut-c-" + key, value, "change"),
    save: () => emit("ut-config-save", "click"), focused: () => focused,
    reject: message => { apiError = { code: "INVALID_MANIFEST", message }; },
    import: async (contents, name = "manifest.json", size) => {
      const bytes = typeof contents === "string" ? Buffer.from(contents, "utf8") : contents;
      get("ut-manifest-file").files = [{ name, size: size ?? bytes.length,
        arrayBuffer: async () => bytes.buffer.slice(bytes.byteOffset, bytes.byteOffset + bytes.byteLength) }];
      await emit("ut-manifest-file", "change");
    } };
}

test("import rejects lossy numbers before replacing the draft", async () => {
  const h = harness(); await settle();
  for (const literal of ["9007199254740993", "0.100000000000000000001", "1e400", "1e-400", "-0"]) {
    await h.import('{"project":{"id":"replacement"},"targets":{"value":' + literal + '}}');
    assert.match(h.get("ut-binding-error").textContent, /精确保留/);
    assert.equal(h.get("ut-m-project-id").value, "demo");
  }
  await h.save();
  assert.deepEqual(h.writes[0].manifest_document, manifest());
});

test("raw JSON refuses rounding and accepts equivalent numeric notation", async () => {
  const h = harness(); await settle();
  const raw = JSON.stringify(manifest()).replace(/}$/, ',"targets":{"n":9007199254740993}}');
  h.input("ut-manifest-json", raw);
  await h.save();
  assert.equal(h.writes.length, 0);
  assert.match(h.get("ut-binding-error").textContent, /精确保留/);
  await h.import(JSON.stringify(manifest()).replace(/}$/, ',"targets":{"n":1e0,"f":0.10,"text":"9007199254740993"}}'));
  await h.save();
  assert.deepEqual(h.writes[0].manifest_document.targets, { n: 1, f: 0.1, text: "9007199254740993" });
});

test("llvm execution controls stay visible and stub options follow the selected mode", async () => {
  const h = harness();
  await settle();
  assert.equal(h.get("ut-host-controls").hidden, false);
  assert.equal(h.get("ut-c-stub_model").disabled, true);
  assert.equal(h.get("ut-stub-llm-controls").hidden, true);
  assert.equal(h.get("ut-csv-repair-heading").parentElement.hidden, false);
  h.change("stub_mode", "llm");
  assert.equal(h.get("ut-stub-llm-controls").hidden, false);
  assert.equal(h.get("ut-c-stub_model").disabled, false);
});

test("ABI width and interface entries are always in the expanded manifest form", () => {
  const html = fs.readFileSync(path.join(base, "app/templates/lanmatrix/ut_binding.html"), "utf8");
  for (const id of ["ut-m-build-abi", "ut-interface-entries"]) {
    assert.ok(html.includes('id="' + id + '"'));
    assert.ok(html.indexOf('id="' + id + '"') < html.indexOf("<details"),
      id + " must appear before collapsed advanced settings");
  }
  assert.match(html, /指针宽度/);
  assert.doesNotMatch(html, /rh850|psd049|phd038/);
});

test("pointer width and interface entries save without a chip or project contract", async () => {
  const h = harness(); await settle();
  assert.deepEqual(h.get("ut-m-build-abi").children.map(o => o.value), ["", "legacy", "undeclared", "32", "64"]);
  h.input("ut-m-build-abi", "32", "change");
  assert.match(h.get("ut-build-abi-description").textContent, /32 位/);
  assert.match(h.get("ut-build-abi-description").textContent, /不证明结构体布局/);
  await h.emit("ut-interface-add", "click");
  const row = h.get("ut-interface-entries").children[0];
  row._name.value = "external_read";
  row._index.value = "0";
  row._direction.value = "out";
  row._direction.listeners.change.call(row._direction);
  h.input("ut-m-interfaces-contract", "approved@2", "change");
  assert.match(h.get("ut-interface-contract-description").textContent, /external_read/);
  await h.save();
  assert.equal(h.writes[0].manifest_document.build.profile, "default");
  assert.deepEqual(h.writes[0].manifest_document.build.abi, { pointer_size: 4 });
  assert.deepEqual(h.writes[0].manifest_document.interfaces.entries, [{
    function: "external_read", parameter_index: 0, direction: "out", pointee_access_order: "write_only",
  }]);
  assert.equal(h.writes[0].manifest_document.interfaces.contract, "approved@2");
});

test("missing ABI choice blocks save instead of silently relying on an undeclared default", async () => {
  const existing = manifest(); delete existing.build;
  const h = harness(true, { manifest_document: existing }); await settle();
  assert.equal(h.get("ut-m-build-abi").value, "");
  await h.save();
  assert.equal(h.writes.length, 0);
  assert.equal(h.focused(), "ut-m-build-abi");
  assert.match(h.get("ut-binding-error").textContent, /请选择目标 ABI/);
  h.input("ut-m-build-abi", "undeclared", "change");
  await h.save();
  assert.deepEqual(h.writes[0].manifest_document.build.abi, { pointer_size: null });
  assert.match(h.get("ut-build-abi-description").textContent, /涉及指针的函数仍会被阻断/);
});

test("unavailable pinned choices remain visible and roundtrip without substitution", async () => {
  const existing = { ...manifest(), build: { profile: "pinned-old" }, interfaces: { contract: "old@7" } };
  const h = harness(true, { manifest_document: existing }); await settle();
  assert.equal(h.get("ut-m-build-profile").value, "pinned-old");
  assert.equal(h.get("ut-m-build-profile").children.at(-1).disabled, true);
  assert.equal(h.get("ut-m-interfaces-contract").value, "old@7");
  await h.save();
  assert.deepEqual(h.writes[0].manifest_document, existing);
});

test("catalog failure keeps the saved document inspectable but disables save and validation", async () => {
  const h = harness(true, { manifest_document: manifest() }, { catalogError: "tool version unavailable" });
  await settle();
  assert.equal(h.get("ut-m-project-id").value, "demo");
  assert.equal(h.get("ut-config-save").disabled, true);
  assert.equal(h.get("ut-manifest-validate").disabled, true);
  assert.equal(h.get("ut-manifest-export").disabled, false);
  assert.match(h.get("ut-binding-error").textContent, /tool version unavailable/);
  await h.save();
  assert.equal(h.writes.length, 0);
});

test("malformed or duplicate catalogs are rejected without guessed choices", async () => {
  for (const options of [{}, { ...catalog(), build_profiles: [] },
    { ...catalog(), interface_contracts: [{ id: "x", label: "one" }, { id: "x", label: "duplicate" }] }]) {
    const h = harness(true, { manifest_document: manifest() }, { catalog: options });
    await settle();
    assert.equal(h.get("ut-config-save").disabled, true);
    assert.equal(h.get("ut-m-build-abi").disabled, true);
    assert.equal(h.get("ut-m-build-profile").value, "default");
  }
});

test("catalog text is displayed as text and never inserted as markup", async () => {
  const options = catalog();
  options.interface_contracts[0].label = '<img src=x onerror="alert(1)">';
  options.interface_contracts[0].description = "<script>bad()</script>";
  const h = harness(true, { manifest_document: manifest() }, { catalog: options });
  await settle();
  assert.equal(h.get("ut-m-interfaces-contract").children[1].textContent, options.interface_contracts[0].label);
  h.input("ut-m-interfaces-contract", "approved@2", "change");
  assert.match(h.get("ut-interface-contract-description").textContent, /<script>bad\(\)<\/script>/);
});

test("saving uses numeric limits, CSRF and the document contract while keeping Clang", async () => {
  const h = harness(); await settle();
  h.change("stub_mode", "llm");
  h.get("ut-c-stub_model").value = " local-stub "; h.get("ut-c-stub_max_rounds").value = "3";
  await h.save();
  assert.equal(h.writes[0].stub_max_rounds, 3);
  assert.equal(h.writes[0].stub_model, "local-stub");
  assert.equal(Object.hasOwn(h.writes[0], "execution_backend"), false);
  assert.equal(h.writes[0].host_clang, "clang");
  assert.equal(h.writes[0].max_repair_rounds, 5);
  assert.equal(Object.hasOwn(h.writes[0], "manifest_path"), false);
  assert.equal(h.requests.find(r => r.method === "PUT").headers["X-CSRF-Token"], "csrf-test");
  await h.save();
  assert.equal(h.writes[1].stub_mode, "llm");
  assert.equal(h.get("ut-manifest-sha256").textContent, "saved-sha");
  assert.equal(h.get("ut-draft-state").textContent, "已保存");
});

test("BOM UTF-8 import populates fields without persisting and roundtrips advanced keys", async () => {
  const h = harness(); await settle();
  const imported = { ...manifest(), targets: [{ name: "f", custom: [1, { nested: true }] }],
    build: { profile: "build-id", custom: { flags: ["-DVALUE=1"] } },
    winams: { profile: "winams-id", custom: "keep" }, interfaces: { contract: null }, rules: { project_pack: null },
    profile: { custom: false }, custom: { version: 7 } };
  await h.import("\uFEFF" + JSON.stringify(imported), "SOURCE.JSON");
  assert.equal(h.writes.length, 0); assert.equal(h.validations.length, 0);
  assert.equal(h.get("ut-m-build-profile").value, "build-id");
  assert.equal(h.get("ut-m-profile-mcdc_enabled").value, "");
  assert.equal(h.get("ut-draft-state").textContent, "有未保存的更改");
  h.input("ut-m-project-id", "renamed");
  h.input("ut-m-build-exclude_directories", "first\n");
  assert.equal(h.get("ut-m-build-exclude_directories").value, "first\n", "typing a new line must remain possible");
  h.input("ut-m-build-exclude_directories", "first\nsecond");
  await h.save();
  imported.project.id = "renamed"; imported.build.exclude_directories = ["first", "second"];
  assert.deepEqual(h.writes[0].manifest_document, imported);
  assert.equal(Object.hasOwn(h.writes[0].manifest_document.profile, "mcdc_enabled"), false);
});

test("a new document keeps every policy choice unselected and omits optional fields", async () => {
  const h = harness(true, null); await settle();
  assert.equal(h.get("ut-policy-section").hidden, false);
  assert.equal(h.get("ut-legacy-baseline").hidden, true);
  for (const key of ["boundary_adjacent", "c1", "mcdc", "arithmetic", "combination_group"]) {
    assert.equal(h.get("ut-m-test_policy-" + key).value, "");
  }
  h.input("ut-m-project-id", "demo");
  h.input("ut-m-build-abi", "32", "change");
  h.input("ut-m-build-profile", "default", "change");
  await h.save();
  assert.equal(h.writes.length, 0);
  assert.equal(h.focused(), "ut-m-test_policy-boundary_adjacent");
  for (const [key, value] of [["boundary_adjacent", "false"], ["c1", "true"], ["mcdc", "false"], ["arithmetic", "false"], ["combination_group", "false"]]) {
    h.input("ut-m-test_policy-" + key, value, "change");
  }
  await h.save();
  assert.deepEqual(h.writes[0].manifest_document, {
    project: { id: "demo" },
    build: { profile: "default", abi: { pointer_size: 4 } },
    test_policy: { version: "1", boundary_adjacent: false, c1: true, mcdc: false, arithmetic: false, combination_group: false },
  });
});

test("legacy import stays unchanged until conversion, which clears every policy choice", async () => {
  const h = harness(true, null); await settle();
  const imported = { ...manifest(), profile: { mcdc_enabled: true, approved_exceptions: ["keep"] }, custom: { retained: true } };
  await h.import(JSON.stringify(imported));
  assert.equal(h.get("ut-legacy-baseline").hidden, false);
  assert.equal(h.get("ut-policy-section").hidden, true);
  assert.equal(h.get("ut-m-profile-mcdc_enabled").value, "true");
  await h.save();
  assert.deepEqual(h.writes[0].manifest_document, imported);
  await h.emit("ut-policy-convert", "click");
  for (const key of ["boundary_adjacent", "c1", "mcdc", "arithmetic", "combination_group"]) {
    assert.equal(h.get("ut-m-test_policy-" + key).value, "");
  }
  await h.save();
  assert.equal(h.writes.length, 1);
  for (const [key, value] of [["boundary_adjacent", "true"], ["c1", "true"], ["mcdc", "true"], ["arithmetic", "false"], ["combination_group", "true"]]) {
    h.input("ut-m-test_policy-" + key, value, "change");
  }
  await h.save();
  assert.deepEqual(h.writes[1].manifest_document, {
    project: imported.project, custom: imported.custom, profile: { approved_exceptions: ["keep"] },
    build: imported.build,
    test_policy: { version: "1", boundary_adjacent: true, c1: true, mcdc: true, arithmetic: false, combination_group: true },
  });
  assert.equal(Object.hasOwn(h.writes[1].manifest_document, "baseline"), false);
  h.input("ut-m-test_policy-mcdc", "false", "change");
  await h.save();
  assert.equal(h.writes.length, 2);
  assert.equal(h.focused(), "ut-m-test_policy-combination_group");
});

test("last raw JSON edits win without blur; form changes retain new advanced keys", async () => {
  const h = harness(); await settle();
  h.input("ut-m-build-profile", "old");
  const raw = { ...manifest(), build: { profile: "raw", include: ["custom"] }, targets: ["new"] };
  h.get("ut-manifest-json").value = JSON.stringify(raw);
  await h.save(); assert.deepEqual(h.writes[0].manifest_document, raw);
  raw.winams = { custom: { roundtrip: true } };
  h.input("ut-manifest-json", JSON.stringify(raw));
  assert.equal(h.get("ut-m-build-profile").value, "raw");
  h.input("ut-m-build-profile", "form-last"); await h.save();
  raw.build.profile = "form-last"; assert.deepEqual(h.writes[1].manifest_document, raw);
});

test("invalid raw edits and missing required fields visibly block save with focus", async () => {
  const h = harness(); await settle();
  h.input("ut-manifest-json", "{broken"); await h.save();
  assert.equal(h.writes.length, 0); assert.equal(h.focused(), "ut-manifest-json");
  assert.equal(h.get("ut-manifest-advanced").open, true);
  assert.equal(h.get("ut-binding-error").hidden, false);
  assert.equal(h.get("ut-manifest-json").value, "{broken");
  h.input("ut-manifest-json", "{}"); await h.save();
  assert.equal(h.writes.length, 0); assert.equal(h.focused(), "ut-m-project-id");
});

test("validation posts only the current document without config persistence", async () => {
  const h = harness(); await settle();
  h.input("ut-m-baseline-version", "2");
  await h.emit("ut-manifest-validate", "click");
  assert.equal(h.writes.length, 0);
  assert.deepEqual(h.validations, [{ manifest_document: { ...manifest(), baseline: { id: "baseline", version: "2" } } }]);
  assert.match(h.get("ut-manifest-validation").textContent, /validated-sha/);
  assert.equal(h.get("ut-draft-state").textContent, "有未保存的更改");
  h.reject("Baseline resource is missing"); await h.emit("ut-manifest-validate", "click");
  assert.match(h.get("ut-binding-error").textContent, /Baseline resource is missing/);
  assert.equal(h.focused(), "ut-binding-error");
  assert.equal(h.get("ut-config-save").disabled, false);
});

test("import rejects extension, oversize, invalid UTF-8 and non-object JSON without replacing draft", async () => {
  const h = harness(); await settle(); const original = h.get("ut-manifest-json").value;
  for (const [contents, name, size, message] of [
    ["{}", "bad.txt", undefined, /\.json/], ["{}", "big.json", 256 * 1024 + 1, /256 KiB/],
    [Buffer.from([0xff]), "bad.json", undefined, /UTF-8/], ["[1]", "array.json", undefined, /对象/],
    ["{broken", "invalid.json", undefined, /无效/]]) {
    await h.import(contents, name, size);
    assert.match(h.get("ut-binding-error").textContent, message);
    assert.equal(h.get("ut-manifest-json").value, original);
    assert.equal(h.focused(), "ut-manifest-import");
  }
  assert.equal(h.writes.length, 0);
  const json = JSON.stringify(manifest());
  await h.import(json + " ".repeat(256 * 1024 - Buffer.byteLength(json)));
  assert.equal(h.get("ut-binding-error").hidden, true, "256 KiB is allowed");
});

test("readers can inspect and export but cannot import, validate or save", async () => {
  const h = harness(false, { manifest_document: manifest(), execution_backend: "clang", stub_mode: "llm" }); await settle();
  for (const id of ["ut-c-stub_mode", "ut-c-stub_model", "ut-c-stub_max_rounds", "ut-c-host_clang", "ut-m-project-id", "ut-manifest-file", "ut-manifest-import"]) assert.equal(h.get(id).disabled, true);
  assert.equal(h.get("ut-manifest-json").readOnly, true);
  assert.deepEqual(JSON.parse(h.get("ut-manifest-json").value), manifest());
  await h.import(JSON.stringify({ project: { id: "forbidden" } }));
  await h.save(); await h.emit("ut-manifest-validate", "click");
  assert.equal(h.writes.length, 0); assert.equal(h.validations.length, 0);
  await h.emit("ut-manifest-export", "click");
  assert.deepEqual(JSON.parse(await h.downloads[0].text()), manifest());
});

test("export, unsaved navigation guard and discard restore the saved draft and config", async () => {
  const h = harness(); await settle();
  h.input("ut-m-project-id", "draft"); h.input("ut-c-index_csv", "new/path.csv");
  await h.emit("ut-manifest-export", "click");
  assert.equal(JSON.parse(await h.downloads[0].text()).project.id, "draft");
  let prevented = false; h.events.beforeunload({ preventDefault() { prevented = true; } }); assert.equal(prevented, true);
  h.emit("ut-config-reset", "click");
  assert.equal(h.get("ut-m-project-id").value, "demo"); assert.equal(h.get("ut-c-index_csv").value, "");
  assert.equal(h.get("ut-draft-state").textContent, "已保存");
  prevented = false; h.events.beforeunload({ preventDefault() { prevented = true; } }); assert.equal(prevented, false);
});

test("invalid numeric limits never send updates", async () => {
  for (const [key, value] of [["stub_max_rounds", "6"], ["stub_max_rounds", "1.5"], ["max_repair_rounds", "21"], ["max_repair_rounds", "-1"]]) {
    const h = harness(); await settle(); h.get("ut-c-" + key).value = value; await h.save();
    assert.equal(h.writes.length, 0); assert.equal(h.focused(), "ut-c-" + key);
  }
});

test("server manifest errors are visible and legacy location is never sent as input", async () => {
  const h = harness(true, { manifest_document: null, manifest_path: "server/legacy.json", manifest_error: "Manifest unavailable" }); await settle();
  assert.match(h.get("ut-binding-error").textContent, /Manifest unavailable/);
  assert.equal(h.get("ut-c-manifest_path").value, "server/legacy.json");
  await h.save(); assert.equal(h.writes.length, 0);
  await h.import(JSON.stringify(manifest())); await h.save();
  assert.equal(Object.hasOwn(h.writes[0], "manifest_path"), false);
});
