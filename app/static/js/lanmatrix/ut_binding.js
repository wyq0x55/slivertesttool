/* Project binding draft. Manifest semantics are validated by the server. */
(function (window, document) {
  "use strict";
  var root = document.querySelector(".lm-ut-binding");
  if (!root) return;
  var pid = Number(root.dataset.project);
  var KEYS = ["index_csv", "product_root", "max_repair_rounds", "repair_model",
    "execution_backend", "stub_mode", "stub_model", "stub_max_rounds", "host_clang", "host_llvm_cov", "host_llvm_profdata", "host_profile_runtime"];
  var DEFAULTS = { execution_backend: "clang", stub_mode: "deterministic", stub_max_rounds: 2,
    host_clang: "clang", host_llvm_cov: "llvm-cov", host_llvm_profdata: "llvm-profdata", max_repair_rounds: 5 };
  var MODEL = ["stub_model", "stub_max_rounds"];
  var POLICY = ["boundary_adjacent", "c1", "mcdc", "arithmetic", "combination_group"];
  var FIELDS = ["project.id", "baseline.id", "baseline.version", "build.profile", "winams.profile",
    "interfaces.contract", "rules.project_pack", "build.exclude_directories", "profile.mcdc_enabled"]
    .concat(POLICY.map(function (key) { return "test_policy." + key; }));
  var LIMIT = 256 * 1024;
  var draft = {}, snapshots = {}, savedConfig = null, savedState = "", rawSnapshot = "";
  var busy = true, loaded = false;
  var choices = null;
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
  function parseDocument(raw) {
    var text = raw.replace(/^\uFEFF/, ""), parsed = JSON.parse(text);
    function decimal(token) {
      var parts = token.toLowerCase().split("e"), negative = parts[0][0] === "-";
      var mantissa = negative ? parts[0].slice(1) : parts[0], digits = mantissa.replace(".", "").replace(/^0+/, "");
      if (!digits) return negative ? "-0" : "0";
      var fraction = mantissa.indexOf(".") < 0 ? 0 : mantissa.length - mantissa.indexOf(".") - 1;
      var trailing = digits.length - digits.replace(/0+$/, "").length;
      return (negative ? "-" : "") + digits.slice(0, digits.length - trailing) + "e" + (Number(parts[1] || 0) - fraction + trailing);
    }
    // The JSON parser validates syntax; this scanner only checks numeric
    // tokens outside quoted strings before replacing the current draft.
    var tokens = /"(?:\\.|[^"\\])*"|(-?(?:0|[1-9]\d*)(?:\.\d+)?(?:[eE][+-]?\d+)?)/g, match;
    while ((match = tokens.exec(text))) {
      if (!match[1]) continue;
      var value = Number(match[1]);
      if (!Number.isFinite(value) || (Number.isInteger(value) && !Number.isSafeInteger(value)) || decimal(match[1]) !== decimal(String(value)))
        throw new Error("清单包含无法在浏览器中精确保留的数值，请保留原文件并通过原路径绑定。");
    }
    return parsed;
  }
  function acceptChoices(value) {
    if (!object(value) || value.contract !== "ut-agent.configuration-options/1")
      throw new Error("工具未提供可用的项目配置目录，请升级配套 ut-agent 后刷新。");
    ["build_profiles", "interface_contracts"].forEach(function (key) {
      var ids = new Set();
      if (!Array.isArray(value[key]) || (key === "build_profiles" && !value[key].length))
        throw new Error("项目配置目录不完整，请检查 ut-agent 后刷新。");
      value[key].forEach(function (item) {
        if (!object(item) || typeof item.id !== "string" || !item.id.trim() ||
            typeof item.label !== "string" || !item.label.trim() || ids.has(item.id))
          throw new Error("项目配置目录含有无效选项，请检查 ut-agent 后刷新。");
        ids.add(item.id);
      });
    });
    choices = value;
  }
  function renderSelect(path, items, emptyLabel) {
    var select = field(path), current = get(path);
    select.replaceChildren();
    function option(value, label, unavailable) {
      var node = document.createElement("option");
      node.value = value; node.textContent = label; node.disabled = !!unavailable;
      select.appendChild(node);
    }
    option("", emptyLabel);
    items.forEach(function (item) {
      var label = path === "build.profile" && item.id === "default" ? "不声明目标 ABI（仅无指针函数）" : item.label;
      if (path === "build.profile" && Number.isInteger(item.pointer_size) && item.pointer_size > 0)
        label += " · " + item.pointer_size * 8 + " 位指针";
      option(item.id, label);
    });
    if (typeof current === "string" && current && !items.some(function (item) { return item.id === current; }))
      option(current, "当前配置未在本机目录中：" + current, true);
  }
  function configurationDetails() {
    var build = choices && choices.build_profiles.find(function (item) { return item.id === get("build.profile"); });
    var contract = choices && choices.interface_contracts.find(function (item) { return item.id === get("interfaces.contract"); });
    $("ut-build-profile-description").textContent = build ?
      (Number.isInteger(build.pointer_size) && build.pointer_size > 0 ? "指针宽度：" + build.pointer_size * 8 + " 位。" :
        "此选项未声明目标指针宽度，涉及指针的函数仍会被阻断。") + " " + (build.description || "") :
      get("build.profile") ? "当前配置不在本机可选目录中，已保留原值，请检查工具版本与配置。" : "尚未选择执行配置，请在保存前明确选择。";
    $("ut-interface-contract-description").textContent = contract ?
      (contract.description || "") + " 适用函数：" + (Array.isArray(contract.functions) ? contract.functions.join("、") : "请核对契约范围") +
      "。审批：" + (contract.approved_by || "见契约记录") + " / " + (contract.approved_at || "见契约记录") :
      get("interfaces.contract") ? "当前契约不在本机已审批目录中，已保留原值，请检查工具版本与契约。" :
        "未使用接口契约；外部指针参数必须能由类型化实现证据证明，否则需要在这里选择适用契约。";
  }
  function renderFields() {
    renderSelect("build.profile", choices ? choices.build_profiles : [], "请选择执行配置");
    renderSelect("interfaces.contract", choices ? choices.interface_contracts : [], "不使用接口契约（须已有实现证据）");
    FIELDS.forEach(function (p) {
      var value = get(p);
      if (p === "build.exclude_directories") value = Array.isArray(value) ? value.join("\n") : "";
      else if (p === "profile.mcdc_enabled" || p.indexOf("test_policy.") === 0) value = typeof value === "boolean" ? String(value) : "";
      else value = typeof value === "string" ? value : "";
      field(p).value = value;
      snapshots[p] = value;
    });
    policyView();
    configurationDetails();
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
    try { parsed = parseDocument(raw); }
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
    configurationDetails();
  }
  function required() {
    if (!choices) throw error("可选配置尚未加载，请检查 ut-agent 后刷新页面。", field("build.profile"));
    var labels = { "project.id": "项目标识", "baseline.id": "测试基准 ID", "baseline.version": "基准版本" };
    var legacy = own(draft, "baseline"), explicit = own(draft, "test_policy");
    (legacy ? ["project.id", "baseline.id", "baseline.version"] : ["project.id"]).forEach(function (p) {
      if (typeof get(p) !== "string" || !get(p).trim()) throw error("请填写" + labels[p] + "。", field(p));
    });
    if (legacy && explicit) throw error("清单只能包含 Baseline 或显式测试要求之一，请在完整 JSON 中修正。", $("ut-manifest-json"));
    if (typeof get("build.profile") !== "string" || !get("build.profile").trim())
      throw error("请选择目标 ABI；仅无指针函数可以选择不声明目标 ABI。", field("build.profile"));
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
    var readonly = !LMUt.can("configure");
    var llm = $("ut-c-stub_mode").value === "llm";
    $("ut-stub-llm-controls").hidden = !llm;
    KEYS.forEach(function (k) {
      $("ut-c-" + k).disabled = locked || readonly ||
        (!llm && MODEL.indexOf(k) >= 0);
    });
    FIELDS.forEach(function (p) { field(p).disabled = locked || readonly ||
      (!choices && (p === "build.profile" || p === "interfaces.contract")); });
    $("ut-manifest-json").readOnly = readonly;
    $("ut-manifest-json").disabled = locked;
    ["ut-config-save", "ut-manifest-import", "ut-manifest-file", "ut-manifest-validate"].forEach(function (id) {
      $(id).disabled = locked || readonly || (!choices && (id === "ut-config-save" || id === "ut-manifest-validate"));
    });
    $("ut-manifest-export").disabled = locked;
    policyView();
    changed();
  }
  function populate(c) {
    c = c || {};
    KEYS.forEach(function (k) { $("ut-c-" + k).value = k === "execution_backend" ? "clang" : String(c[k] != null ? c[k] : DEFAULTS[k] != null ? DEFAULTS[k] : ""); });
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
      try { imported = parseDocument(text); }
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
      $("ut-c-execution_backend").value = "clang";
      var values = { manifest_document: draft };
      KEYS.forEach(function (k) { values[k] = $("ut-c-" + k).value.trim(); });
      values.max_repair_rounds = Number(values.max_repair_rounds);
      values.stub_max_rounds = Number(values.stub_max_rounds);
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
    var results = await Promise.allSettled([LMApi.utGetConfig(pid), LMApi.utManifestOptions(pid)]);
    if (results[0].status !== "fulfilled") { fail(results[0].reason.message); return; }
    var optionError;
    try {
      if (results[1].status !== "fulfilled") throw results[1].reason;
      acceptChoices(results[1].value);
    } catch (e) { optionError = e; }
    savedConfig = results[0].value; populate(savedConfig); lock(false);
    if (optionError) fail(optionError.message, null, false);
  });
})(window, document);
