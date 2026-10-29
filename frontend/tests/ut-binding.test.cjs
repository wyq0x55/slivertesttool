const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");
const { TextDecoder } = require("node:util");
const settle = () => new Promise(resolve => setImmediate(resolve));
const base = path.resolve(__dirname, "../..");
const clone = value => JSON.parse(JSON.stringify(value));
const policy = () => ({ version: "1", boundary_adjacent: true, c1: true, mcdc: true, arithmetic: false, combination_group: false });
const manifest = () => ({ project: { id: "demo" }, build: { profile: "default" }, test_policy: policy() });
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
    return { id, value: "", checked: false, indeterminate: false, type: "text", disabled: false, hidden: false, readOnly: false, textContent: "", open: false,
      parentElement: { hidden: false }, listeners: {}, dataset: { project: "4" }, children: [],
      replaceChildren() { this.children = []; }, appendChild(child) { child.parentElement = this; this.children.push(child); },
      classList: { toggle(k, on) { on ? classes.add(k) : classes.delete(k); }, contains(k) { return classes.has(k); } },
      addEventListener(type, fn) { this.listeners[type] = fn; },
      dispatchEvent(event) { if (this.listeners[event.type]) this.listeners[event.type].call(this, event); },
      setAttribute(k, v) { attributes[k] = v; }, removeAttribute(k) { delete attributes[k]; },
      getAttribute(k) { return attributes[k]; }, focus() { focused = id; },
      click() { if (this.listeners.click) return this.listeners.click.call(this); } };
  }
  for (const [, id] of template.matchAll(/\bid="([^"]+)"/g)) nodes.set(id, node(id));
  for (const [, attrs] of template.matchAll(/<input\b([^>]+)>/g)) {
    const id = attrs.match(/\bid="([^"]+)"/), type = attrs.match(/\btype="([^"]+)"/);
    if (id && type) nodes.get(id[1]).type = type[1];
  }
  nodes.get("ut-manifest-json").parentElement = nodes.get("ut-manifest-advanced");
  nodes.get("ut-manifest-advanced").tagName = "DETAILS";
  nodes.get("ut-manifest-advanced").parentElement = nodes.get("ut-settings-advanced");
  nodes.get("ut-settings-advanced").tagName = "DETAILS";
  nodes.set("root", node("root"));
  const pickers = [...template.matchAll(/data-path-picker="([^"]+)" data-path-target="([^"]+)" data-path-title="([^"]+)"/g)]
    .map(([, kind, target, title]) => Object.assign(node("picker-" + kind), { dataset: { pathPicker: kind, pathTarget: target, pathTitle: title } }));
  pickers.forEach(picker => nodes.set(picker.id, picker));
  nodes.get("root").querySelectorAll = selector => selector === "[data-path-picker]" ? pickers : [];
  const picked = [];
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
    LMUtPathPicker: { open: async options => { picked.push(options); return settings.pick === undefined ? null : settings.pick; } },
    Event: class { constructor(type) { this.type = type; } },
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
  function input(id, value, type = "input") {
    const el = get(id);
    if (el.type === "checkbox") el.checked = value === true || value === "true" || value === "llm";
    else el.value = value;
    return emit(id, type);
  }
  return { get, writes, validations, requests, downloads, events, input, emit, picked,
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
  assert.equal(h.get("ut-settings-advanced").open, false);
  h.change("stub_mode", "llm");
  assert.equal(h.get("ut-stub-llm-controls").hidden, false);
  assert.equal(h.get("ut-c-stub_model").disabled, false);
});

test("routine settings use checkboxes and omit retired or specialized form fields", () => {
  const html = fs.readFileSync(path.join(base, "app/templates/lanmatrix/ut_binding.html"), "utf8");
  for (const key of ["boundary_adjacent", "c1", "mcdc", "arithmetic", "combination_group"]) {
    assert.match(html, new RegExp('id="ut-m-test_policy-' + key + '" type="checkbox"'));
  }
  assert.match(html, /id="ut-c-stub_mode" type="checkbox"/);
  assert.match(html, /id="ut-m-execution_contract-const_volatile_single_invocation_stable" type="checkbox"/);
  assert.doesNotMatch(html, /id="ut-m-(rules-project_pack|build-config|profile-approved_exceptions|winams-[^"]+|interfaces-contract)"/);
  assert.doesNotMatch(html, /ut-interface-add|ut-interface-entries/);
  assert.doesNotMatch(html, /rh850|psd049|phd038/);
  const advanced = html.indexOf('id="ut-settings-advanced"');
  assert.ok(html.indexOf('id="ut-m-build-abi"') < advanced);
  assert.ok(html.indexOf('id="ut-m-build-profile"') > advanced);
  assert.ok(html.indexOf('id="ut-c-host_clang"') > advanced);
});

test("the execution contract checkbox declares and withdraws the assumption", async () => {
  const h = harness(); await settle();
  const id = "ut-m-execution_contract-const_volatile_single_invocation_stable";
  assert.equal(h.get(id).checked, false);
  h.input(id, true, "change");
  await h.save();
  assert.deepEqual(h.writes[0].manifest_document.execution_contract,
    { version: "1", const_volatile_single_invocation_stable: true });
  h.input(id, false, "change");
  await h.save();
  assert.equal(h.writes[1].manifest_document.execution_contract.const_volatile_single_invocation_stable, false);
});

test("specialized imported settings survive checkbox edits without form duplicates", async () => {
  const imported = { ...manifest(), build: { profile: "default", config: "release" },
    winams: { profile: "custom", stub_memory_limit_bytes: 32768, stub_memory_headroom_ratio: 0.25 },
    profile: { approved_exceptions: ["EX-1"] },
    interfaces: { contract: "approved@2", entries: [{ function: "dep", parameter_index: 0,
      direction: "in", custom: { retained: true } }] } };
  const h = harness(true, { manifest_document: imported }); await settle();
  h.input("ut-m-test_policy-c1", false, "change");
  await h.save();
  assert.deepEqual(h.writes[0].manifest_document, { ...imported, test_policy: { ...policy(), c1: false } });
});

test("pointer width saves with default automated stubs and no interface declaration", async () => {
  const h = harness(); await settle();
  assert.deepEqual(h.get("ut-m-build-abi").children.map(o => o.value), ["", "legacy", "undeclared", "32", "64"]);
  h.input("ut-m-build-abi", "32", "change");
  assert.match(h.get("ut-build-abi-description").textContent, /32 位/);
  assert.match(h.get("ut-build-abi-description").textContent, /不证明结构体布局/);
  await h.emit("ut-manifest-validate", "click");
  assert.equal(Object.hasOwn(h.validations[0].manifest_document, "interfaces"), false);
  await h.save();
  assert.equal(h.writes[0].manifest_document.build.profile, "default");
  assert.deepEqual(h.writes[0].manifest_document.build.abi, { pointer_size: 4 });
  assert.equal(h.writes[0].stub_mode, "deterministic");
  assert.equal(Object.hasOwn(h.writes[0].manifest_document, "interfaces"), false);
});

test("raw optional contracts and entries survive import, normal edits, validation and export", async () => {
  const h = harness(); await settle();
  const imported = { ...manifest(), interfaces: {
    contract: "unlisted@7",
    entries: [{ function: "dep", parameter_index: 0, direction: "inout",
      pointee_access_order: "read_before_write", custom: { retained: true } }],
    custom: { retained: ["raw"] },
  } };
  await h.import(JSON.stringify(imported));
  h.input("ut-m-project-id", "edited");
  imported.project.id = "edited";
  await h.emit("ut-manifest-validate", "click");
  assert.deepEqual(h.validations[0].manifest_document, imported);
  await h.save();
  assert.deepEqual(h.writes[0].manifest_document, imported);
  imported.interfaces.entries[0].direction = "out";
  imported.interfaces.entries[0].pointee_access_order = "write_only";
  h.get("ut-manifest-json").value = JSON.stringify(imported);
  await h.save();
  assert.deepEqual(h.writes[1].manifest_document, imported);
  await h.emit("ut-manifest-export", "click");
  assert.deepEqual(JSON.parse(await h.downloads[0].text()), imported);
  delete imported.interfaces;
  h.input("ut-manifest-json", JSON.stringify(imported));
  await h.save();
  assert.deepEqual(h.writes[2].manifest_document, imported);
});

test("optional raw interface fields go to authoritative validation without local direction rules", async () => {
  const h = harness(); await settle();
  const raw = { ...manifest(), interfaces: { entries: [{ custom: "backend-owned" }] } };
  h.input("ut-manifest-json", JSON.stringify(raw));
  h.reject("Unsupported interface entry");
  await h.emit("ut-manifest-validate", "click");
  assert.deepEqual(h.validations[0].manifest_document, raw);
  assert.match(h.get("ut-binding-error").textContent, /Unsupported interface entry/);
  assert.equal(h.writes.length, 0);
});

test("unused interface catalogs are not prerequisites for automated stubs", async () => {
  for (const value of [undefined, [], null]) {
    const options = catalog();
    delete options.interface_contracts;
    delete options.interface_directions;
    if (value !== undefined) {
      options.interface_contracts = value;
      options.interface_directions = value;
    }
    const h = harness(true, { manifest_document: manifest() }, { catalog: options });
    await settle();
    assert.equal(h.get("ut-config-save").disabled, false);
    await h.save();
    assert.equal(h.writes[0].stub_mode, "deterministic");
    assert.deepEqual(h.writes[0].manifest_document, manifest());
  }
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
  assert.equal(JSON.parse(h.get("ut-manifest-json").value).interfaces.contract, "old@7");
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
    { ...catalog(), build_profiles: [{ id: "x", label: "one" }, { id: "x", label: "duplicate" }] }]) {
    const h = harness(true, { manifest_document: manifest() }, { catalog: options });
    await settle();
    assert.equal(h.get("ut-config-save").disabled, true);
    assert.equal(h.get("ut-m-build-abi").disabled, true);
    assert.equal(h.get("ut-m-build-profile").value, "default");
  }
});

test("catalog text is displayed as text and never inserted as markup", async () => {
  const options = catalog();
  options.build_profiles[1].label = '<img src=x onerror="alert(1)">';
  options.build_profiles[1].description = "<script>bad()</script>";
  const h = harness(true, { manifest_document: manifest() }, { catalog: options });
  await settle();
  assert.match(h.get("ut-m-build-profile").children[1].textContent, /<img src=x onerror="alert\(1\)">/);
  h.input("ut-m-build-profile", "typed32", "change");
  assert.match(h.get("ut-build-profile-description").textContent, /<script>bad\(\)<\/script>/);
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

test("new checkbox policies save explicit booleans without requiring every option to be clicked", async () => {
  const h = harness(true, null); await settle();
  for (const key of ["boundary_adjacent", "c1", "mcdc", "arithmetic", "combination_group"]) {
    assert.equal(h.get("ut-m-test_policy-" + key).checked, false);
    assert.equal(h.get("ut-m-test_policy-" + key).indeterminate, false);
  }
  assert.equal(h.get("ut-m-test_policy-combination_group").disabled, true);
  h.input("ut-m-project-id", "demo");
  h.input("ut-m-build-abi", "32", "change");
  h.input("ut-m-build-profile", "default", "change");
  h.input("ut-m-test_policy-c1", true, "change");
  await h.save();
  assert.deepEqual(h.writes[0].manifest_document, {
    project: { id: "demo" }, build: { abi: { pointer_size: 4 } },
    test_policy: { version: "1", boundary_adjacent: false, c1: true, mcdc: false, arithmetic: false, combination_group: false },
  });
});

test("turning MC/DC off clears and disables its presentation option without restoring it on reenable", async () => {
  const h = harness(true, { manifest_document: { ...manifest(), test_policy: { ...policy(), combination_group: true } } });
  await settle();
  h.input("ut-m-test_policy-mcdc", false, "change");
  assert.equal(h.get("ut-m-test_policy-combination_group").checked, false);
  assert.equal(h.get("ut-m-test_policy-combination_group").disabled, true);
  await h.save();
  assert.equal(h.writes[0].manifest_document.test_policy.combination_group, false);
  assert.equal(h.writes[0].manifest_document.test_policy.mcdc, false);
  h.input("ut-m-test_policy-mcdc", true, "change");
  assert.equal(h.get("ut-m-test_policy-combination_group").disabled, false);
  assert.equal(h.get("ut-m-test_policy-combination_group").checked, false);
});

test("incomplete imported policies are not silently converted into unchecked requirements", async () => {
  const imported = manifest(); delete imported.test_policy.c1;
  const h = harness(true, { manifest_document: imported }); await settle();
  assert.equal(h.get("ut-m-test_policy-c1").indeterminate, true);
  h.input("ut-m-project-id", "edited"); await h.save();
  assert.equal(h.writes.length, 0);
  assert.equal(h.focused(), "ut-m-test_policy-c1");
  h.input("ut-m-test_policy-c1", false, "change"); await h.save();
  assert.equal(h.writes[0].manifest_document.test_policy.c1, false);
});

test("removed non-null project packs require explicit removal rather than silent data loss", async () => {
  const imported = { ...manifest(), rules: { project_pack: "old@1", universal: "universal-test-rules@2" } };
  const h = harness(true, { manifest_document: imported }); await settle();
  assert.match(h.get("ut-legacy-list").textContent, /rules.project_pack/);
  await h.save(); assert.equal(h.writes.length, 0);
  await h.emit("ut-legacy-remove", "click"); await h.save();
  assert.deepEqual(h.writes[0].manifest_document.rules, { universal: "universal-test-rules@2" });
});

test("an undeclared grouping option can be explicitly confirmed off while MC/DC stays off", async () => {
  for (const value of [undefined, null, "false"]) {
    const imported = { ...manifest(), test_policy: { ...policy(), mcdc: false } };
    if (value === undefined) delete imported.test_policy.combination_group;
    else imported.test_policy.combination_group = value;
    const h = harness(true, { manifest_document: imported }); await settle();
    const id = "ut-m-test_policy-combination_group";
    assert.equal(h.get(id).indeterminate, true);
    assert.equal(h.get(id).disabled, false);
    h.input("ut-m-project-id", "edited"); await h.save();
    assert.equal(h.writes.length, 0);
    assert.equal(h.focused(), id);
    h.input(id, true, "change"); await h.save();
    assert.equal(h.get(id).checked, false);
    assert.equal(h.get(id).indeterminate, false);
    assert.equal(h.get(id).disabled, true);
    assert.equal(h.writes[0].manifest_document.test_policy.combination_group, false);
    assert.equal(h.writes[0].manifest_document.test_policy.mcdc, false);
  }
});

test("explicitly disabling MC/DC also confirms an indeterminate grouping option off", async () => {
  const imported = manifest(); delete imported.test_policy.combination_group;
  const h = harness(true, { manifest_document: imported }); await settle();
  h.input("ut-m-test_policy-mcdc", false, "change"); await h.save();
  assert.equal(h.get("ut-m-test_policy-combination_group").indeterminate, false);
  assert.equal(h.writes[0].manifest_document.test_policy.combination_group, false);
});

test("legacy fields block save until removed from the form, keeping other settings", async () => {
  const h = harness(true, null); await settle();
  const imported = { ...manifest(), baseline: { id: "baseline", version: "1" },
    profile: { mcdc_enabled: true, approved_exceptions: ["keep"] }, test_policy: { ...policy(), version: "0" }, custom: { retained: true } };
  await h.import(JSON.stringify(imported));
  assert.equal(h.get("ut-legacy-fields").hidden, false);
  assert.match(h.get("ut-legacy-list").textContent, /baseline.*profile\.mcdc_enabled.*test_policy\.version/);
  await h.save();
  assert.equal(h.writes.length, 0);
  assert.equal(h.focused(), "ut-legacy-remove");
  await h.emit("ut-legacy-remove", "click");
  assert.equal(h.get("ut-legacy-fields").hidden, true);
  await h.save();
  assert.deepEqual(h.writes[0].manifest_document, {
    project: imported.project, build: imported.build, test_policy: policy(),
    profile: { approved_exceptions: ["keep"] }, custom: imported.custom,
  });
  h.input("ut-m-test_policy-combination_group", "true", "change");
  h.input("ut-m-test_policy-mcdc", "false", "change");
  await h.save();
  assert.equal(h.writes.length, 2);
  assert.equal(h.writes[1].manifest_document.test_policy.combination_group, false);
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
  h.input("ut-manifest-json", JSON.stringify({ ...manifest(), winams: { profile: "standard" } }));
  await h.emit("ut-manifest-validate", "click");
  assert.equal(h.writes.length, 0);
  assert.deepEqual(h.validations, [{ manifest_document: { ...manifest(), winams: { profile: "standard" } } }]);
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
    const h = harness(); await settle();
    if (key === "stub_max_rounds") h.change("stub_mode", "llm");
    h.get("ut-c-" + key).value = value; await h.save();
    assert.equal(h.writes.length, 0); assert.equal(h.focused(), "ut-c-" + key);
  }
});

test("inactive Stub model settings are omitted rather than blocking deterministic saves", async () => {
  const h = harness(true, { manifest_document: manifest(), stub_mode: "deterministic",
    stub_model: "saved-model", stub_max_rounds: 3 });
  await settle();
  h.get("ut-c-stub_max_rounds").value = "invalid";
  await h.save();
  assert.equal(h.writes[0].stub_mode, "deterministic");
  assert.equal(Object.hasOwn(h.writes[0], "stub_max_rounds"), false);
  assert.equal(Object.hasOwn(h.writes[0], "stub_model"), false);
});

test("server manifest errors are visible and legacy location is never sent as input", async () => {
  const h = harness(true, { manifest_document: null, manifest_path: "server/legacy.json", manifest_error: "Manifest unavailable" }); await settle();
  assert.match(h.get("ut-binding-error").textContent, /Manifest unavailable/);
  assert.equal(h.get("ut-c-manifest_path").value, "server/legacy.json");
  await h.save(); assert.equal(h.writes.length, 0);
  await h.import(JSON.stringify(manifest())); await h.save();
  assert.equal(Object.hasOwn(h.writes[0], "manifest_path"), false);
});

test("server path pickers fill the bound paths like typed input", async () => {
  const h = harness(true, { manifest_document: manifest(), product_root: "D:\\Old", index_csv: "D:\\Old\\i.csv" },
    { pick: "D:\\Project\\Soft" });
  await settle();
  assert.equal(h.get("picker-directory").disabled, false);
  await h.emit("picker-directory", "click");
  assert.deepEqual(JSON.parse(JSON.stringify(h.picked)), [{ pid: 4, kind: "directory", title: "选择源码根目录", value: "D:\\Old" }]);
  assert.equal(h.get("ut-c-product_root").value, "D:\\Project\\Soft");
  assert.equal(h.get("ut-draft-state").textContent, "有未保存的更改");
  await h.emit("picker-csv", "click");
  assert.equal(h.picked[1].kind, "csv");
  assert.equal(h.picked[1].value, "D:\\Old\\i.csv");
  await h.save();
  assert.equal(h.writes[0].product_root, "D:\\Project\\Soft");
});

test("a cancelled pick leaves the binding unchanged and read-only users get no picker", async () => {
  const h = harness(true, { manifest_document: manifest(), product_root: "D:\\Old" });
  await settle();
  await h.emit("picker-directory", "click");
  assert.equal(h.get("ut-c-product_root").value, "D:\\Old");
  assert.equal(h.get("ut-draft-state").textContent, "已保存");
  const reader = harness(false); await settle();
  assert.equal(reader.get("picker-directory").disabled, true);
  await reader.emit("picker-directory", "click");
  assert.equal(reader.picked.length, 0);
});
