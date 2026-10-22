/* Project binding draft. Manifest semantics are validated by the server. */
(function (window, document) {
  "use strict";
  var root = document.querySelector(".lm-ut-binding");
  if (!root) return;
  var pid = Number(root.dataset.project);
  var KEYS = ["index_csv", "product_root", "work_root", "stub_builds", "max_repair_rounds", "repair_model",
    "execution_backend", "stub_mode", "stub_model", "stub_max_rounds", "host_clang", "host_llvm_cov", "host_llvm_profdata", "host_profile_runtime"];
  var DEFAULTS = { execution_backend: "winams", stub_mode: "deterministic", stub_max_rounds: 2,
    host_clang: "clang", host_llvm_cov: "llvm-cov", host_llvm_profdata: "llvm-profdata", max_repair_rounds: 5 };
  var HOST = ["stub_mode", "host_clang", "host_llvm_cov", "host_llvm_profdata", "host_profile_runtime"];
  var MODEL = ["stub_model", "stub_max_rounds"];
  var POLICY = ["boundary_adjacent", "c1", "mcdc", "arithmetic", "combination_group"];
  var FIELDS = ["project.id", "baseline.id", "baseline.version", "build.profile", "winams.profile",
    "interfaces.contract", "rules.project_pack", "build.exclude_directories", "profile.mcdc_enabled"]
    .concat(POLICY.map(function (key) { return "test_policy." + key; }));
  var LIMIT = 256 * 1024;
  var draft = {}, snapshots = {}, savedConfig = null, savedState = "", rawSnapshot = "";
  var busy = true, loaded = false;
  function $(id) { return document.getElementById(id); }
  function field(path) { return $("ut-m-" + path.replace(".", "-")); }
  function object(value) { return value !== null && typeof value === "object" && !Array.isArray(value); }
  function own(obj, key) { return Object.prototype.hasOwnProperty.call(obj, key); }
  function get(path) {
    var parts = path.split(".");
    return object(draft[parts[0]]) && own(draft[parts[0]], parts[1]) ? draft[parts[0]][parts[1]] : undefined;
  }
  function state() { return JSON.stringify([$("ut-manifest-json").value, KEYS.map(function (k) { return $("ut-c-" + k).value; })]); }
  function changed() {
    var dirty = loaded && state() !== savedState;
    $("ut-draft-state").textContent = !loaded ? "正在加载…" : !LMUt.can("configure") ? "只读" : dirty ? "有未保存的更改" : "已保存";
    $("ut-draft-state").classList.toggle("is-dirty", dirty);
    $("ut-config-reset").disabled = busy || !LMUt.can("configure") || !dirty;
    $("ut-manifest-validation").textContent = "";
  }
  function clearError() {
    $("ut-binding-error").hidden = true;
    FIELDS.forEach(function (p) { field(p).removeAttribute("aria-invalid"); });
    $("ut-manifest-json").removeAttribute("aria-invalid");
  }
  function fail(message, node, focus) {
    var box = $("ut-binding-error");
    box.textContent = message;
    box.hidden = false;
    if (node) {
      node.setAttribute("aria-invalid", "true");
      if (node === $("ut-manifest-json")) $("ut-manifest-advanced").open = true;
    }
    if (focus !== false) (node || box).focus();
  }
  function error(message, node) { var e = new Error(message); e.node = node; return e; }
  function renderFields() {
    FIELDS.forEach(function (p) {
      var value = get(p);
      if (p === "build.exclude_directories") value = Array.isArray(value) ? value.join("\n") : "";
      else if (p === "profile.mcdc_enabled" || p.indexOf("test_policy.") === 0) value = typeof value === "boolean" ? String(value) : "";
      else value = typeof value === "string" ? value : "";
      field(p).value = value;
      snapshots[p] = value;
    });
    policyView();
  }
  function policyView() {
    var legacy = own(draft, "baseline"), explicit = own(draft, "test_policy");
    $("ut-legacy-baseline").hidden = !legacy;
    $("ut-policy-section").hidden = legacy && !explicit;
    $("ut-policy-convert").disabled = busy || !LMUt.can("configure") || !legacy || explicit;
    ["baseline.id", "baseline.version"].forEach(function (p) { field(p).required = legacy; });
    POLICY.forEach(function (key) { field("test_policy." + key).required = !legacy || explicit; });
  }
  function renderRaw() {
    rawSnapshot = JSON.stringify(draft, null, 2);
    $("ut-manifest-json").value = rawSnapshot;
  }
  function parseRaw() {
    var raw = $("ut-manifest-json").value;
    if (raw === rawSnapshot) return;
    var parsed;
    try { parsed = JSON.parse(raw.replace(/^\uFEFF/, "")); }
    catch (e) { throw error("清单 JSON 格式无效：" + e.message, $("ut-manifest-json")); }
    if (!object(parsed)) throw error("清单 JSON 必须是对象。", $("ut-manifest-json"));
    draft = parsed;
    rawSnapshot = raw;
  }
  // Only changed fields are projected, preserving unknown keys, explicit nulls and absence.
  function syncForm(refreshFields) {
    var edits = FIELDS.filter(function (p) { return field(p).value !== snapshots[p]; })
      .map(function (p) { return [p, field(p).value]; });
    parseRaw();
    edits.forEach(function (edit) {
      var p = edit[0], value = edit[1].trim(), parts = p.split("."), group = parts[0], key = parts[1];
      if (own(draft, group) && !object(draft[group])) {
        throw error(group + " 必须是对象，请在完整 JSON 中修正。", $("ut-manifest-json"));
      }
      if (!value) {
        if (own(draft, group)) {
          delete draft[group][key];
          if (!Object.keys(draft[group]).length) delete draft[group];
        }
      } else {
        if (!own(draft, group)) Object.defineProperty(draft, group, { value: group === "test_policy" ? { version: "1" } : {}, writable: true, enumerable: true, configurable: true });
        draft[group][key] = p === "profile.mcdc_enabled" || group === "test_policy" ? value === "true" :
          p === "build.exclude_directories" ? value.split(/\r?\n/).map(function (v) { return v.trim(); }).filter(Boolean) : value;
      }
    });
    if (refreshFields !== false) renderFields();
    else FIELDS.forEach(function (p) { snapshots[p] = field(p).value; });
    renderRaw();
    policyView();
  }
  function required() {
    var labels = { "project.id": "项目标识", "baseline.id": "测试基准 ID", "baseline.version": "基准版本" };
    var legacy = own(draft, "baseline"), explicit = own(draft, "test_policy");
    (legacy ? ["project.id", "baseline.id", "baseline.version"] : ["project.id"]).forEach(function (p) {
      if (typeof get(p) !== "string" || !get(p).trim()) throw error("请填写" + labels[p] + "。", field(p));
    });
    if (legacy && explicit) throw error("清单只能包含 Baseline 或显式测试要求之一，请在完整 JSON 中修正。", $("ut-manifest-json"));
    if (legacy) return;
    POLICY.forEach(function (key) {
      if (typeof get("test_policy." + key) !== "boolean") throw error("请明确选择每项测试要求的是或否。", field("test_policy." + key));
    });
    if (get("test_policy.version") !== "1") throw error("测试要求版本必须为 1，请在完整 JSON 中修正。", $("ut-manifest-json"));
    if (get("test_policy.combination_group") && !get("test_policy.mcdc"))
      throw error("MC/DC 组合分组选择是时，MC/DC 也必须选择是。", field("test_policy.combination_group"));
    if (object(draft.profile) && own(draft.profile, "mcdc_enabled"))
      throw error("显式测试要求不能同时包含旧 profile.mcdc_enabled，请在完整 JSON 中修正。", $("ut-manifest-json"));
  }
  function lock(locked) {
    busy = locked;
    var readonly = !LMUt.can("configure"), clang = $("ut-c-execution_backend").value === "clang";
    var llm = clang && $("ut-c-stub_mode").value === "llm";
    $("ut-winams-controls").hidden = clang;
    $("ut-host-controls").hidden = !clang;
    $("ut-stub-llm-controls").hidden = !llm;
    $("ut-csv-repair-heading").parentElement.hidden = clang;
    KEYS.forEach(function (k) {
      $("ut-c-" + k).disabled = locked || readonly ||
        (clang && ["work_root", "stub_builds", "max_repair_rounds", "repair_model"].indexOf(k) >= 0) ||
        (!clang && HOST.indexOf(k) >= 0) || (!llm && MODEL.indexOf(k) >= 0);
    });
    FIELDS.forEach(function (p) { field(p).disabled = locked || readonly; });
    $("ut-manifest-json").readOnly = readonly;
    $("ut-manifest-json").disabled = locked;
    ["ut-config-save", "ut-manifest-import", "ut-manifest-file", "ut-manifest-validate"].forEach(function (id) { $(id).disabled = locked || readonly; });
    $("ut-manifest-export").disabled = locked;
    policyView();
    changed();
  }
  function populate(c) {
    c = c || {};
    KEYS.forEach(function (k) { $("ut-c-" + k).value = String(c[k] != null ? c[k] : DEFAULTS[k] != null ? DEFAULTS[k] : ""); });
    draft = object(c.manifest_document) ? JSON.parse(JSON.stringify(c.manifest_document)) : { test_policy: { version: "1" } };
    $("ut-c-manifest_path").value = c.manifest_path || "";
    $("ut-manifest-sha256").textContent = c.manifest_sha256 || "—";
    renderFields(); renderRaw(); clearError();
    loaded = true; savedState = state(); changed();
    if (c.manifest_error) fail(c.manifest_error, null, false);
  }
  function editable() { return loaded && !busy && LMUt.can("configure"); }
  KEYS.forEach(function (k) {
    ["input", "change"].forEach(function (event) {
      $("ut-c-" + k).addEventListener(event, function () { if (editable()) { lock(false); } });
    });
  });
  FIELDS.forEach(function (p) {
    ["input", "change"].forEach(function (event) {
      field(p).addEventListener(event, function () {
        if (!editable()) return;
        clearError();
        try { syncForm(false); } catch (e) { fail(e.message, e.node); }
        changed();
      });
    });
  });
  $("ut-manifest-json").addEventListener("input", function () {
    if (!editable()) return;
    clearError();
    try { parseRaw(); renderFields(); } catch (e) { fail(e.message, e.node, false); }
    changed();
  });
  $("ut-manifest-import").addEventListener("click", function () { if (editable()) $("ut-manifest-file").click(); });
  $("ut-policy-convert").addEventListener("click", function () {
    if (!editable()) return;
    clearError();
    try {
      syncForm();
      if (!own(draft, "baseline") || own(draft, "test_policy")) return;
      delete draft.baseline;
      if (object(draft.profile)) {
        delete draft.profile.mcdc_enabled;
        if (!Object.keys(draft.profile).length) delete draft.profile;
      }
      draft.test_policy = { version: "1" };
      renderFields(); renderRaw(); changed();
      field("test_policy.boundary_adjacent").focus();
    } catch (e) { fail(e.message, e.node); }
  });
  $("ut-manifest-file").addEventListener("change", async function () {
    if (!editable()) return;
    var file = this.files && this.files[0];
    if (!file) return;
    clearError();
    try {
      if (!/\.json$/i.test(file.name)) throw error("请选择 .json 清单文件。", $("ut-manifest-import"));
      if (file.size > LIMIT) throw error("清单不能超过 256 KiB。", $("ut-manifest-import"));
      lock(true);
      var bytes = await file.arrayBuffer(), text;
      if (bytes.byteLength > LIMIT) throw error("清单不能超过 256 KiB。", $("ut-manifest-import"));
      try { text = new TextDecoder("utf-8", { fatal: true }).decode(bytes); }
      catch (e) { throw error("清单文件必须使用 UTF-8 编码。", $("ut-manifest-import")); }
      var imported;
      try { imported = JSON.parse(text.replace(/^\uFEFF/, "")); }
      catch (e) { throw error("清单 JSON 格式无效：" + e.message, $("ut-manifest-import")); }
      if (!object(imported)) throw error("清单 JSON 必须是对象。", $("ut-manifest-import"));
      draft = imported; renderFields(); renderRaw();
      LMUt.say("已导入清单，检查后保存即可生效。");
    } catch (e) { lock(false); fail(e.message, e.node); }
    finally { this.value = ""; lock(false); }
  });
  $("ut-config-reset").addEventListener("click", function () {
    if (!editable()) return;
    populate(savedConfig); lock(false); LMUt.say("已放弃未保存的更改。");
  });
  $("ut-manifest-export").addEventListener("click", function () {
    if (!loaded || busy) return;
    clearError();
    try {
      if (LMUt.can("configure")) syncForm();
      var url = URL.createObjectURL(new Blob([JSON.stringify(draft, null, 2) + "\n"], { type: "application/json;charset=utf-8" }));
      var link = document.createElement("a");
      link.href = url; link.download = "project-manifest-draft.json"; link.click();
      window.setTimeout(function () { URL.revokeObjectURL(url); }, 1000);
      changed();
    } catch (e) { fail(e.message, e.node); }
  });
  $("ut-manifest-validate").addEventListener("click", async function () {
    if (!editable()) return;
    clearError();
    var result;
    try { syncForm(); required(); lock(true); result = await LMApi.utValidateManifest(pid, draft); }
    catch (e) { fail(e.message, e.node); }
    finally { lock(false); }
    if (result) $("ut-manifest-validation").textContent = "清单校验通过，请保存绑定以应用更改。SHA-256：" + result.sha256;
  });
  $("ut-config-save").addEventListener("click", async function () {
    if (!editable()) return;
    clearError();
    try {
      syncForm(); required();
      var values = { manifest_document: draft };
      KEYS.forEach(function (k) { values[k] = $("ut-c-" + k).value.trim(); });
      values.max_repair_rounds = Number(values.max_repair_rounds);
      values.stub_max_rounds = Number(values.stub_max_rounds);
      if (values.execution_backend !== "clang") values.stub_mode = "deterministic";
      if (!Number.isInteger(values.stub_max_rounds) || values.stub_max_rounds < 1 || values.stub_max_rounds > 5)
        throw error("Stub 候选轮数必须是 1 至 5 之间的整数。", $("ut-c-stub_max_rounds"));
      if (!Number.isInteger(values.max_repair_rounds) || values.max_repair_rounds < 0 || values.max_repair_rounds > 20)
        throw error("修正上限必须是 0 至 20 之间的整数。", $("ut-c-max_repair_rounds"));
      lock(true);
      var c = await LMApi.utPutConfig(pid, values);
      savedConfig = c || await LMApi.utGetConfig(pid);
      populate(savedConfig); LMUt.say("绑定已保存。");
    } catch (e) { fail(e.message, e.node); }
    finally { lock(false); }
  });
  window.addEventListener("beforeunload", function (event) {
    if (loaded && LMUt.can("configure") && state() !== savedState) { event.preventDefault(); event.returnValue = ""; }
  });
  (window.LMReady || Promise.resolve()).then(async function () {
    lock(true);
    try { savedConfig = await LMApi.utGetConfig(pid); populate(savedConfig); lock(false); }
    catch (e) { fail(e.message); }
  });
})(window, document);
