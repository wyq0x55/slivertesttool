/* Project binding draft. Manifest semantics are validated by the server. */
(function (window, document) {
  "use strict";
  var root = document.querySelector(".lm-ut-binding");
  if (!root) return;
  var pid = Number(root.dataset.project);
  var KEYS = ["index_csv", "product_root", "max_repair_rounds", "repair_model",
    "stub_mode", "stub_model", "stub_max_rounds", "host_clang", "host_llvm_cov", "host_llvm_profdata", "host_profile_runtime"];
  var DEFAULTS = { stub_mode: "deterministic", stub_max_rounds: 2,
    host_clang: "clang", host_llvm_cov: "llvm-cov", host_llvm_profdata: "llvm-profdata", max_repair_rounds: 5 };
  var MODEL = ["stub_model", "stub_max_rounds"];
  var POLICY = ["boundary_adjacent", "c1", "mcdc", "arithmetic", "combination_group"];
  var CONTRACT_FLAG = "execution_contract.const_volatile_single_invocation_stable";
  var FIELDS = ["project.id", "build.profile", "build.exclude_directories", CONTRACT_FLAG]
    .concat(POLICY.map(function (key) { return "test_policy." + key; }));
  var LINES = ["build.exclude_directories"];
  // Groups whose document carries a schema version next to the form-owned keys.
  var VERSIONED = { test_policy: true, execution_contract: true };
  var LIMIT = 256 * 1024;
  var draft = {}, snapshots = {}, savedConfig = null, savedState = "", rawSnapshot = "";
  var busy = true, loaded = false;
  var choices = null;
  var pickers = Array.prototype.slice.call(root.querySelectorAll("[data-path-picker]"));
  function $(id) { return document.getElementById(id); }
  function field(path) { return $("ut-m-" + path.replace(".", "-")); }
  function object(value) { return value !== null && typeof value === "object" && !Array.isArray(value); }
  function own(obj, key) { return Object.prototype.hasOwnProperty.call(obj, key); }
  function get(path) {
    var parts = path.split(".");
    return object(draft[parts[0]]) && own(draft[parts[0]], parts[1]) ? draft[parts[0]][parts[1]] : undefined;
  }
  function configValue(key) {
    var node = $("ut-c-" + key);
    return key === "stub_mode" ? (node.checked ? "llm" : "deterministic") : node.value;
  }
  function formValue(path) { return kind(path) === "boolean" ? field(path).checked : field(path).value; }
  function state() { return JSON.stringify([$("ut-manifest-json").value, KEYS.map(configValue)]); }
  function changed() {
    var dirty = loaded && state() !== savedState;
    $("ut-draft-state").textContent = !loaded ? "正在加载…" : !LMUt.can("configure") ? "只读" : dirty ? "有未保存的更改" : "已保存";
    $("ut-draft-state").classList.toggle("is-dirty", dirty);
    $("ut-draft-state").classList.toggle("is-saved", loaded && !dirty && LMUt.can("configure"));
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
      for (var parent = node.parentElement; parent; parent = parent.parentElement) {
        if (parent.tagName === "DETAILS") parent.open = true;
      }
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
    if (!object(value) || value.contract !== "ut-agent.configuration-options/2")
      throw new Error("工具未提供可用的项目配置目录，请升级配套 ut-agent 后刷新。");
    ["build_profiles", "pointer_sizes"].forEach(function (key) {
      var ids = new Set();
      if (!Array.isArray(value[key]) || !value[key].length)
        throw new Error("项目配置目录不完整，请检查 ut-agent 后刷新。");
      value[key].forEach(function (item) {
        if (!object(item) || typeof item.id !== "string" || !item.id.trim() ||
            typeof item.label !== "string" || !item.label.trim() || ids.has(item.id))
          throw new Error("项目配置目录含有无效选项，请检查 ut-agent 后刷新。");
        if (key === "pointer_sizes" && item.pointer_size !== null &&
            !(Number.isInteger(item.pointer_size) && item.pointer_size > 0))
          throw new Error("项目配置目录含有无效指针宽度，请检查 ut-agent 后刷新。");
        ids.add(item.id);
      });
    });
    choices = value;
  }
  function pointerItem(id) {
    return choices && choices.pointer_sizes.find(function (item) { return item.id === id; });
  }
  function renderSelect(path, items, emptyLabel) {
    var select = field(path), current = get(path);
    select.replaceChildren();
    function option(value, label, unavailable) {
      var node = document.createElement("option");
      node.value = value; node.textContent = label; node.disabled = !!unavailable;
      select.appendChild(node);
    }
    if (!items.some(function (item) { return item.id === "default"; })) option("", emptyLabel);
    items.forEach(function (item) {
      var label = path === "build.profile" && item.id === "default" ? "默认编译上下文" : item.label;
      if (path === "build.profile" && Number.isInteger(item.pointer_size) && item.pointer_size > 0)
        label += " · 记录了 " + item.pointer_size * 8 + " 位指针";
      option(item.id, label);
    });
    if (typeof current === "string" && current && !items.some(function (item) { return item.id === current; }))
      option(current, "当前配置未在本机目录中：" + current, true);
  }
  function abiSelection() {
    var abi = object(draft.build) && object(draft.build.abi) ? draft.build.abi : null;
    if (abi && own(abi, "pointer_size")) {
      var match = choices && choices.pointer_sizes.find(function (item) { return item.pointer_size === abi.pointer_size; });
      return match ? match.id : "custom";
    }
    return typeof get("build.profile") === "string" && get("build.profile") ? "legacy" : "";
  }
  function renderAbi() {
    var select = $("ut-m-build-abi"), current = abiSelection();
    select.replaceChildren();
    function option(value, label, unavailable) {
      var node = document.createElement("option");
      node.value = value; node.textContent = label; node.disabled = !!unavailable;
      select.appendChild(node);
    }
    option("", "请选择指针宽度");
    if (current === "legacy") option("legacy", "尚未单独声明（沿用编译兼容配置）");
    var widthLabels = { undeclared: "不声明指针宽度（仅无指针函数）", "32": "32 位指针", "64": "64 位指针" };
    (choices ? choices.pointer_sizes : []).forEach(function (item) {
      var label = widthLabels[item.id] || item.label;
      if (Number.isInteger(item.pointer_size) && item.pointer_size > 0 && !widthLabels[item.id])
        label += " · 只声明指针宽度";
      option(item.id, label);
    });
    if (current === "custom") option("custom", "当前指针宽度不在本机目录中：" + draft.build.abi.pointer_size, true);
    select.value = current;
  }
  function configurationDetails() {
    var build = choices && choices.build_profiles.find(function (item) { return item.id === get("build.profile"); });
    var abiValue = $("ut-m-build-abi").value, abi = pointerItem(abiValue);
    var abiText = "请选择指针宽度。32 位或 64 位只声明指针宽度。";
    if (abiValue === "legacy")
      abiText = "尚未单独声明指针宽度，保存时会沿用当前编译兼容配置。需要通用声明时，请改为 32 位、64 位或不声明。";
    else if (abi && Number.isInteger(abi.pointer_size))
      abiText = "指针宽度：" + abi.pointer_size * 8 + " 位。只声明指针宽度，不证明结构体布局、端序或其他整数宽度。" +
        (build && build.pointer_size === abi.pointer_size && build.id !== "default" ? " 当前编译器兼容配置与该宽度一致，将保留该配置已记录的 ABI 属性。" : "");
    else if (abi)
      abiText = "已明确不声明指针宽度。涉及指针的函数仍会被阻断。";
    else if (abiValue === "custom")
      abiText = "当前指针宽度不在本机目录中，已保留原值。";
    $("ut-build-abi-description").textContent = abiText + (abi && abi.description ? " " + abi.description : "");
    $("ut-build-profile-description").textContent = build ? (build.description || "") :
      get("build.profile") ? "当前编译器兼容配置不在本机目录中，已保留原值。" : "使用默认编译上下文，不附加特定编译器兼容项。";
  }
  function kind(p) {
    if (LINES.indexOf(p) >= 0) return "lines";
    if (p === CONTRACT_FLAG || p.indexOf("test_policy.") === 0) return "boolean";
    return "string";
  }
  function display(p, value) {
    var k = kind(p);
    if (k === "lines") return Array.isArray(value) ? value.join("\n") : "";
    if (p === "build.profile" && value === undefined && choices &&
        choices.build_profiles.some(function (item) { return item.id === "default"; })) return "default";
    return typeof value === "string" ? value : "";
  }
  function convert(p, value) {
    if (kind(p) === "lines") return value.split(/\r?\n/).map(function (v) { return v.trim(); }).filter(Boolean);
    return value;
  }
  function ensure(container, key) {
    if (!own(container, key)) Object.defineProperty(container, key, { value: VERSIONED[key] ? { version: "1" } : {}, writable: true, enumerable: true, configurable: true });
    return container[key];
  }
  function legacyFields() {
    var found = [];
    if (own(draft, "baseline")) found.push("baseline");
    if (object(draft.rules) && own(draft.rules, "project_pack") && draft.rules.project_pack !== null) found.push("rules.project_pack");
    if (object(draft.profile) && own(draft.profile, "mcdc_enabled")) found.push("profile.mcdc_enabled");
    if (object(draft.test_policy) && draft.test_policy.version !== "1") found.push("test_policy.version（须为 1）");
    return found;
  }
  function legacyView() {
    var found = legacyFields();
    $("ut-legacy-fields").hidden = !found.length;
    $("ut-legacy-list").textContent = found.join("、");
  }
  function renderFields() {
    renderAbi();
    renderSelect("build.profile", choices ? choices.build_profiles : [], "默认编译上下文");
    FIELDS.forEach(function (p) {
      var node = field(p), value = get(p);
      if (kind(p) === "boolean") {
        node.checked = value === true;
        node.indeterminate = p.indexOf("test_policy.") === 0 && typeof value !== "boolean";
      } else node.value = display(p, value);
      snapshots[p] = formValue(p);
    });
    legacyView();
    policyView();
    configurationDetails();
  }
  function policyView() {
    var combination = field("test_policy.combination_group"), mcdc = field("test_policy.mcdc").checked;
    combination.disabled = busy || !LMUt.can("configure") || (!mcdc && !combination.indeterminate);
    $("ut-policy-combination-hint").textContent = combination.indeterminate && !mcdc ?
      "此项尚未明确；点击确认不启用，或先启用 MC/DC。" : "仅改变同组用例的呈现方式；需启用 MC/DC。";
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
  function applyAbi() {
    var value = $("ut-m-build-abi").value;
    if (value === "legacy" || value === "custom") return;
    if (!value) {
      if (object(draft.build) && own(draft.build, "abi")) delete draft.build.abi;
      return;
    }
    var item = pointerItem(value);
    if (!item) throw error("请选择目标 ABI 的指针宽度。", $("ut-m-build-abi"));
    if (!object(draft.build)) draft.build = {};
    draft.build.abi = { pointer_size: item.pointer_size };
  }
  // Only changed fields are projected, preserving unknown keys, explicit nulls and absence.
  function syncForm(refreshFields) {
    var edits = FIELDS.filter(function (p) { return formValue(p) !== snapshots[p]; })
      .map(function (p) {
        if (kind(p) === "boolean") return [p, field(p).checked];
        var value = field(p).value.trim();
        return [p, value ? convert(p, value) : null];
      });
    parseRaw();
    function group(name) {
      if (own(draft, name) && !object(draft[name]))
        throw error(name + " 必须是对象。请导入格式正确的清单，或在完整 JSON 中修正。", $("ut-manifest-json"));
      return name;
    }
    edits.forEach(function (edit) {
      var parts = edit[0].split("."), name = group(parts[0]), key = parts[1];
      if (edit[1] === null) {
        if (own(draft, name)) {
          delete draft[name][key];
          var rest = Object.keys(draft[name]);
          // An execution contract with only its version declares nothing; omission means off.
          if (!rest.length || (name === "execution_contract" && rest.length === 1 && rest[0] === "version")) delete draft[name];
        }
      } else {
        ensure(draft, name)[key] = edit[1];
      }
    });
    applyAbi();
    if (refreshFields !== false) renderFields();
    else {
      FIELDS.forEach(function (p) { snapshots[p] = formValue(p); });
      legacyView();
    }
    renderRaw();
    policyView();
    configurationDetails();
  }
  function required() {
    if (!choices) throw error("可选配置尚未加载，请检查 ut-agent 后刷新页面。", $("ut-m-build-abi"));
    if (typeof get("project.id") !== "string" || !get("project.id").trim()) throw error("请填写项目标识。", field("project.id"));
    if (legacyFields().length)
      throw error("清单含有已不再支持的旧版字段。请点击「移除旧版字段」，再逐项确认测试要求。", $("ut-legacy-remove"));
    var abiValue = $("ut-m-build-abi").value, abi = pointerItem(abiValue);
    var profile = choices.build_profiles.find(function (item) { return item.id === get("build.profile"); });
    if (abiValue === "legacy") {
      if (typeof get("build.profile") !== "string" || !get("build.profile").trim())
        throw error("请选择目标 ABI 的指针宽度。", $("ut-m-build-abi"));
    } else if (!abi) {
      throw error("请选择目标 ABI 的指针宽度。32 位或 64 位只声明指针宽度；仅无指针函数可以选择不声明。", $("ut-m-build-abi"));
    } else if (profile && profile.pointer_size != null && profile.pointer_size !== abi.pointer_size) {
      throw error("指针宽度与编译器兼容配置不一致。请改成相同宽度，或改用默认编译上下文。", $("ut-m-build-abi"));
    }
    POLICY.forEach(function (key) {
      if (typeof get("test_policy." + key) !== "boolean") throw error("存在未声明或无效的测试要求，请勾选或取消该项后保存。", field("test_policy." + key));
    });
    if (get("test_policy.combination_group") && !get("test_policy.mcdc"))
      throw error("「組合せ」呈现需要启用 MC/DC。", field("test_policy.mcdc"));
  }
  function lock(locked) {
    busy = locked;
    var readonly = !LMUt.can("configure");
    var llm = $("ut-c-stub_mode").checked;
    $("ut-stub-llm-controls").hidden = !llm;
    KEYS.forEach(function (k) {
      $("ut-c-" + k).disabled = locked || readonly ||
        (!llm && MODEL.indexOf(k) >= 0);
    });
    FIELDS.forEach(function (p) { field(p).disabled = locked || readonly ||
      (!choices && p === "build.profile"); });
    $("ut-m-build-abi").disabled = locked || readonly || !choices;
    $("ut-legacy-remove").disabled = locked || readonly;
    $("ut-manifest-json").readOnly = readonly;
    $("ut-manifest-json").disabled = locked;
    ["ut-config-save", "ut-manifest-import", "ut-manifest-file", "ut-manifest-validate"].forEach(function (id) {
      $(id).disabled = locked || readonly || (!choices && (id === "ut-config-save" || id === "ut-manifest-validate"));
    });
    $("ut-manifest-export").disabled = locked;
    pickers.forEach(function (button) { button.disabled = locked || readonly; });
    policyView();
    changed();
  }
  function populate(c) {
    c = c || {};
    KEYS.forEach(function (k) {
      var value = c[k] != null ? c[k] : DEFAULTS[k] != null ? DEFAULTS[k] : "";
      if (k === "stub_mode") $("ut-c-" + k).checked = value === "llm";
      else $("ut-c-" + k).value = String(value);
    });
    draft = object(c.manifest_document) ? JSON.parse(JSON.stringify(c.manifest_document)) : {
      test_policy: POLICY.reduce(function (policy, key) { policy[key] = false; return policy; }, { version: "1" })
    };
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
  // A picked path goes through the input's own events, so dirty tracking and
  // saving treat it exactly like a typed one.
  pickers.forEach(function (button) {
    button.addEventListener("click", async function () {
      if (!editable()) return;
      var input = $(button.dataset.pathTarget);
      var path = await LMUtPathPicker.open({ pid: pid, kind: button.dataset.pathPicker,
        title: button.dataset.pathTitle, value: input.value });
      if (path == null || !editable() || path === input.value) return;
      input.value = path;
      input.dispatchEvent(new Event("input", { bubbles: true }));
    });
  });
  function edited() {
    if (!editable()) return;
    clearError();
    if (this && this.type === "checkbox") {
      this.indeterminate = false;
      var path = FIELDS.find(function (p) { return field(p) === this; }, this);
      if (path && typeof get(path) !== "boolean") snapshots[path] = null;
      if (path === "test_policy.combination_group" && !field("test_policy.mcdc").checked) this.checked = false;
      if (this === field("test_policy.mcdc") && !this.checked) {
        var combination = field("test_policy.combination_group");
        combination.checked = false;
        combination.indeterminate = false;
        if (typeof get("test_policy.combination_group") !== "boolean") snapshots["test_policy.combination_group"] = null;
      }
    }
    try { syncForm(false); } catch (e) { fail(e.message, e.node); }
    changed();
  }
  FIELDS.map(field).concat([$("ut-m-build-abi")]).forEach(function (node) {
    ["input", "change"].forEach(function (event) { node.addEventListener(event, edited); });
  });
  $("ut-legacy-remove").addEventListener("click", function () {
    if (!editable()) return;
    clearError();
    try {
      syncForm();
      delete draft.baseline;
      if (object(draft.rules)) {
        delete draft.rules.project_pack;
        if (!Object.keys(draft.rules).length) delete draft.rules;
      }
      if (object(draft.profile)) {
        delete draft.profile.mcdc_enabled;
        if (!Object.keys(draft.profile).length) delete draft.profile;
      }
      if (object(draft.test_policy)) draft.test_policy.version = "1";
      renderFields(); renderRaw();
      LMUt.say("已移除旧版字段，请逐项确认测试要求后保存。");
    } catch (e) { fail(e.message, e.node); }
    changed();
  });
  $("ut-manifest-json").addEventListener("input", function () {
    if (!editable()) return;
    clearError();
    try { parseRaw(); renderFields(); } catch (e) { fail(e.message, e.node, false); }
    changed();
  });
  $("ut-manifest-import").addEventListener("click", function () { if (editable()) $("ut-manifest-file").click(); });
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
      var values = { manifest_document: draft };
      KEYS.forEach(function (k) { values[k] = String(configValue(k)).trim(); });
      values.max_repair_rounds = Number(values.max_repair_rounds);
      if (values.stub_mode === "llm") {
        values.stub_max_rounds = Number(values.stub_max_rounds);
        if (!Number.isInteger(values.stub_max_rounds) || values.stub_max_rounds < 1 || values.stub_max_rounds > 5)
          throw error("Stub 候选轮数必须是 1 至 5 之间的整数。", $("ut-c-stub_max_rounds"));
      } else {
        delete values.stub_model;
        delete values.stub_max_rounds;
      }
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
