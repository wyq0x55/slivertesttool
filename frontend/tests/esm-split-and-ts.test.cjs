const assert = require("node:assert/strict");
const { execFileSync } = require("node:child_process");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");
const zlib = require("node:zlib");

const repoRoot = path.resolve(__dirname, "../..");
const frontendDir = path.resolve(__dirname, "..");
const staticDir = path.join(repoRoot, "app/static");
const lanmatrixJsDir = path.join(staticDir, "js/lanmatrix");
const univerDir = path.join(staticDir, "vendor/univer");
const univerEsmDir = path.join(univerDir, "esm");

const settle = () => new Promise((resolve) => setImmediate(resolve));

test("ESM Univer core bundle, lazy secondary chunk, and .gz sidecars exist and shrink initial payload", () => {
  const umdPath = path.join(univerDir, "univer.full.umd.js");
  const umdCssPath = path.join(univerDir, "univer.full.umd.css");
  const corePath = path.join(univerEsmDir, "univer-core.js");
  const coreGzPath = `${corePath}.gz`;
  const chunksDir = path.join(univerEsmDir, "chunks");

  assert.ok(fs.existsSync(umdPath), "legacy UMD bundle must remain intact");
  assert.ok(fs.existsSync(umdCssPath), "UMD stylesheet must remain intact");
  assert.ok(fs.existsSync(corePath), "ESM univer-core.js must exist");
  assert.ok(fs.existsSync(coreGzPath), "ESM univer-core.js.gz sidecar must exist");
  assert.ok(fs.existsSync(chunksDir), "ESM chunks directory must exist");

  const umdStat = fs.statSync(umdPath);
  const coreStat = fs.statSync(corePath);
  const coreGzStat = fs.statSync(coreGzPath);

  assert.ok(
    coreStat.size < umdStat.size * 0.75,
    `univer-core.js (${coreStat.size} B) must be substantially smaller than univer.full.umd.js (${umdStat.size} B)`
  );
  assert.ok(coreGzStat.mtimeMs >= coreStat.mtimeMs, "univer-core.js.gz mtime must not be older than source");
  assert.equal(
    zlib.gunzipSync(fs.readFileSync(coreGzPath)).byteLength,
    coreStat.size,
    "univer-core.js.gz must decompress to exact source byte length"
  );

  const chunkFiles = fs.readdirSync(chunksDir).filter((f) => f.endsWith(".js"));
  const secondaryChunks = chunkFiles.filter((f) => f.startsWith("univer-secondary-"));
  assert.ok(secondaryChunks.length >= 1, "lazy univer-secondary chunk must exist in esm/chunks/");

  for (const chunkName of secondaryChunks) {
    const chunkPath = path.join(chunksDir, chunkName);
    const chunkGzPath = `${chunkPath}.gz`;
    assert.ok(fs.existsSync(chunkGzPath), `missing .gz sidecar for ${chunkName}`);
    const raw = fs.readFileSync(chunkPath);
    const unzipped = zlib.gunzipSync(fs.readFileSync(chunkGzPath));
    assert.equal(unzipped.byteLength, raw.byteLength);
  }

  const editorHtml = fs.readFileSync(path.join(repoRoot, "app/templates/lanmatrix/editor.html"), "utf8");
  assert.match(editorHtml, /rel="modulepreload"\s+href="\{\{\s*static_asset_url\('vendor\/univer\/esm\/univer-core\.js'\)\s*\}\}"/);
  assert.match(editorHtml, /<script\s+type="module"\s+src="\{\{\s*static_asset_url\('vendor\/univer\/esm\/univer-core\.js'\)\s*\}\}"><\/script>/);
  assert.doesNotMatch(editorHtml, /url_for\(\s*['"]static['"]/);
});

test("TypeScript sources pass tsc --noEmit and emit ut_tasks.js + editor_qualification.js", () => {
  const utTs = path.join(frontendDir, "src/lanmatrix/ut_tasks.ts");
  const qualTs = path.join(frontendDir, "src/lanmatrix/editor_qualification.ts");
  const utJs = path.join(lanmatrixJsDir, "ut_tasks.js");
  const qualJs = path.join(lanmatrixJsDir, "editor_qualification.js");

  assert.ok(fs.existsSync(utTs), "ut_tasks.ts must exist");
  assert.ok(fs.existsSync(qualTs), "editor_qualification.ts must exist");
  assert.ok(fs.existsSync(utJs) && fs.statSync(utJs).size > 1024, "generated ut_tasks.js must exist");
  assert.ok(fs.existsSync(qualJs) && fs.statSync(qualJs).size > 512, "generated editor_qualification.js must exist");

  const tscBin = require.resolve("typescript/bin/tsc", { paths: [frontendDir] });
  execFileSync(process.execPath, [tscBin, "--noEmit"], {
    cwd: frontendDir,
    stdio: "pipe",
  });
});

test("editor_qualification.js injects Clean-Room toggle and attaches clean_room only when enabled", async () => {
  const nodes = new Map();
  function makeNode(tag, id = "") {
    const node = {
      tagName: String(tag || "div").toUpperCase(),
      id,
      type: "",
      className: "",
      title: "",
      value: "",
      textContent: "",
      innerHTML: "",
      checked: false,
      hidden: false,
      disabled: false,
      dataset: {},
      style: {},
      parentNode: null,
      children: [],
      listeners: {},
      addEventListener(evt, fn) {
        this.listeners[evt] = fn;
      },
      appendChild(child) {
        child.parentNode = this;
        this.children.push(child);
        if (child.id) nodes.set(child.id, child);
        return child;
      },
      insertBefore(child, ref) {
        child.parentNode = this;
        const idx = this.children.indexOf(ref);
        if (idx >= 0) this.children.splice(idx, 0, child);
        else this.children.push(child);
        if (child.id) nodes.set(child.id, child);
        return child;
      },
    };
    if (id) nodes.set(id, node);
    return node;
  }

  const editorRoot = makeNode("div", "lm-editor-root");
  editorRoot.dataset.projectId = "9";
  const toolbarRight = makeNode("div", "lm-toolbar-right");
  const runBtn = makeNode("button", "lm-run-selected");
  toolbarRight.appendChild(runBtn);
  const body = makeNode("body");

  const runCalls = [];
  const rerunCalls = [];
  const patchProjectCalls = [];
  const LMApi = {
    async runSelectedTasks(pid, payload) {
      runCalls.push({ pid, payload: JSON.parse(JSON.stringify(payload)) });
      return { created: [] };
    },
    async rerunSelectedTasks(pid, keys, opts) {
      rerunCalls.push({ pid, keys, opts: opts ? JSON.parse(JSON.stringify(opts)) : undefined });
      return { created: [] };
    },
    async getProject() {
      return { project: { id: 9, default_signal_list: "sig_init" } };
    },
    async patchProject(pid, changes) {
      patchProjectCalls.push({ pid, changes: JSON.parse(JSON.stringify(changes)) });
      return { project: { id: pid, ...changes } };
    },
  };

  const document = {
    readyState: "complete",
    body,
    querySelector(sel) {
      return sel === ".lm-editor" ? editorRoot : null;
    },
    getElementById(id) {
      return nodes.get(id) || null;
    },
    createElement(tag) {
      const el = makeNode(tag);
      Object.defineProperty(el, "id", {
        get() { return this._id || ""; },
        set(v) {
          this._id = v;
          if (v) nodes.set(v, this);
        },
      });
      return el;
    },
    createTextNode(text) {
      return { textContent: text };
    },
  };
  const window = { LM: { projectId: 9 }, LMApi };
  const context = vm.createContext({ window, document, LMApi });
  vm.runInContext(fs.readFileSync(path.join(lanmatrixJsDir, "editor_qualification.js"), "utf8"), context);

  const toggle = nodes.get("lm-clean-room-toggle");
  assert.ok(toggle, "#lm-clean-room-toggle must be injected into the toolbar");
  assert.match(toggle.title, /ASPICE SWE\.4-SWE\.6/);
  assert.equal(window.LMEditorQualification.isCleanRoomEnabled(), false);

  // 1) Unchecked -> clean_room omitted
  await LMApi.runSelectedTasks(9, { test_ids: ["TC_01"] });
  assert.equal(runCalls.length, 1);
  assert.equal("clean_room" in runCalls[0].payload, false);

  // 2) Checked -> clean_room: true injected
  toggle.checked = true;
  toggle.listeners.change();
  assert.equal(window.LMEditorQualification.isCleanRoomEnabled(), true);

  await LMApi.runSelectedTasks(9, { test_ids: ["TC_01", "TC_02"], model: "m1" });
  assert.equal(runCalls.length, 2);
  assert.deepEqual(runCalls[1], {
    pid: 9,
    payload: { test_ids: ["TC_01", "TC_02"], model: "m1", clean_room: true },
  });

  await LMApi.rerunSelectedTasks(9, ["k1"]);
  assert.equal(rerunCalls.length, 1);
  assert.deepEqual(rerunCalls[0], { pid: 9, keys: ["k1"], opts: { clean_room: true } });

  // 3) Unchecked again -> clean_room stripped
  window.LMEditorQualification.setCleanRoomEnabled(false);
  assert.equal(toggle.checked, false);
  await LMApi.runSelectedTasks(9, { test_ids: ["TC_03"], clean_room: true });
  assert.equal(runCalls.length, 3);
  assert.equal("clean_room" in runCalls[2].payload, false);

  // 4) Default signal list persistence
  await window.LMEditorQualification.saveDefaultSignalList("  v_spd \r\n u1_lamp \n");
  assert.deepEqual(patchProjectCalls, [
    { pid: 9, changes: { default_signal_list: "v_spd\nu1_lamp" } },
  ]);
});

test("ut_tasks.js populates #ut-cfg-toolchain-profile from available_toolchains and submits toolchain_profile on save", async () => {
  const utHtml = fs.readFileSync(path.join(repoRoot, "app/templates/lanmatrix/ut_tasks.html"), "utf8");
  assert.match(utHtml, /<select\s+id="ut-cfg-toolchain-profile">/);
  assert.doesNotMatch(utHtml, /url_for\(\s*['"]static['"]/);

  const nodes = new Map();
  function get(id) {
    if (!nodes.has(id)) {
      nodes.set(id, {
        id,
        dataset: {},
        value: "",
        textContent: "",
        innerHTML: "",
        hidden: false,
        disabled: false,
        open: false,
        listeners: {},
        children: [],
        classList: { toggle() {} },
        setAttribute() {},
        querySelectorAll() { return []; },
        addEventListener(type, listener) { this.listeners[type] = listener; },
        appendChild(node) { this.children.push(node); },
        removeChild(node) { this.children.splice(this.children.indexOf(node), 1); },
        showModal() { this.open = true; },
        close() { this.open = false; },
      });
    }
    return nodes.get(id);
  }

  const root = get("root");
  root.dataset.project = "4";
  get("ut-detail").hidden = true;

  const putPayloads = [];
  const mockToolchains = [
    {
      id: "default",
      label: "默认工具链 (PATH)",
      host_clang: "clang",
      host_llvm_cov: "llvm-cov",
      host_llvm_profdata: "llvm-profdata",
      host_profile_runtime: "",
    },
    {
      id: "llvm-18-win64",
      label: "LLVM 18.1 Win64 Qualification",
      host_clang: "C:/LLVM18/bin/clang.exe",
      host_llvm_cov: "C:/LLVM18/bin/llvm-cov.exe",
      host_llvm_profdata: "C:/LLVM18/bin/llvm-profdata.exe",
      host_profile_runtime: "C:/LLVM18/lib/clang_rt.profile.lib",
    },
  ];
  let mockConfigDoc = {
    project_id: 4,
    toolchain_profile: "llvm-18-win64",
    host_clang: "clang",
    host_llvm_cov: "llvm-cov",
    host_llvm_profdata: "llvm-profdata",
    host_profile_runtime: "",
  };

  const document = {
    hidden: false,
    addEventListener() {},
    querySelector: (sel) => (sel === ".lm-ut-tasks" ? root : null),
    getElementById: get,
    createElement: () => ({ className: "", setAttribute() {}, click() {}, remove() {} }),
    body: { appendChild() {} },
    createTextNode: (v) => ({ textContent: v }),
  };
  const window = {
    LM: {
      user: { is_system_admin: false },
      projectCapabilities: { edit: true, configure: true, review: true },
    },
    LMReady: Promise.resolve({ is_system_admin: false }),
    scrollTo() {},
    setTimeout: () => 1,
    LMStepsEditor: { open() {}, close() {} },
  };
  const LMApi = {
    utJobs: async () => [],
    utBatches: async () => [],
    utGetConfig: async () => ({
      config: mockConfigDoc,
      available_toolchains: mockToolchains,
    }),
    utPutConfig: async (pid, values) => {
      putPayloads.push({ pid, values: JSON.parse(JSON.stringify(values)) });
      mockConfigDoc = { ...mockConfigDoc, ...values };
      return mockConfigDoc;
    },
    utExportUrl: () => "/export",
  };
  const context = vm.createContext({
    window,
    document,
    setTimeout: () => 1,
    clearTimeout() {},
    LMApi,
    LMUI: { alert: async () => {}, confirm: async () => true },
    LMUrl: { get: () => "", set() {}, replace() {}, onPop() {} },
  });
  window.LMUrl = context.LMUrl;

  for (const file of ["pill.js", "ut_common.js", "ut_testcsv.js", "ut_tasks.js"]) {
    vm.runInContext(fs.readFileSync(path.join(lanmatrixJsDir, file), "utf8"), context);
    context.LMPill = window.LMPill;
    context.LMUt = window.LMUt;
    context.LM = window.LM;
  }

  await settle();
  await settle();

  const selectEl = get("ut-cfg-toolchain-profile");
  assert.match(selectEl.innerHTML, /value="default"/);
  assert.match(selectEl.innerHTML, /value="llvm-18-win64"[\s\S]*LLVM 18\.1 Win64 Qualification/);
  assert.equal(selectEl.value, "llvm-18-win64");
  assert.equal(get("ut-cfg-host-clang").value, "C:/LLVM18/bin/clang.exe");
  assert.equal(get("ut-cfg-host-clang").disabled, true, "preset profile / non-admin disables raw path input");

  // Save preset selection
  get("ut-cfg-save").listeners.click();
  await settle();
  await settle();
  assert.equal(putPayloads.length, 1);
  assert.deepEqual(putPayloads[0], {
    pid: 4,
    values: { toolchain_profile: "llvm-18-win64" },
  });
  assert.match(
    selectEl.innerHTML,
    /value="llvm-18-win64"[\s\S]*LLVM 18\.1 Win64 Qualification/,
    "available_toolchains options must remain intact after PUT returns flat config document"
  );

  // Elevate to system admin and switch to default profile -> raw path inputs become editable
  window.LM.user = { is_system_admin: true };
  selectEl.value = "default";
  selectEl.listeners.change.call(selectEl);
  assert.equal(get("ut-cfg-host-clang").disabled, false, "system admin on default profile can edit raw paths");
  get("ut-cfg-host-clang").value = "D:/CustomLLVM/bin/clang.exe";

  get("ut-cfg-save").listeners.click();
  await settle();
  await settle();
  assert.equal(putPayloads.length, 2);
  assert.equal(putPayloads[1].pid, 4);
  assert.equal(putPayloads[1].values.toolchain_profile, "default");
  assert.equal(putPayloads[1].values.host_clang, "D:/CustomLLVM/bin/clang.exe");
});
