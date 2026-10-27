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
  var FIELDS = ["project.id", "build.profile", "build.config", "winams.profile",
    "winams.stub_memory_limit_bytes", "winams.stub_memory_headroom_ratio", "rules.project_pack",
    "interfaces.contract", "build.exclude_directories", "profile.approved_exceptions", CONTRACT_FLAG]
    .concat(POLICY.map(function (key) { return "test_policy." + key; }));
  var LINES = ["build.exclude_directories", "profile.approved_exceptions"];
  // Groups whose document carries a schema version next to the form-owned keys.
  var VERSIONED = { test_policy: true, execution_contract: true };
  // The only direction/access-order pairs ut-agent accepts for a pointer parameter.
  var ACCESS = [["in", "read_only", "输入 · 只读"], ["out", "write_only", "输出 · 只写"],
    ["inout", "read_before_write", "输入输出 · 先读后写"], ["inout", "write_before_read", "输入输出 · 先写后读"]];
  var LIMIT = 256 * 1024;
  var draft = {}, snapshots = {}, savedConfig = null, savedState = "", rawSnapshot = "";
  var busy = true, loaded = false;
  var choices = null;
  var rows = [], entriesSnapshot = "", rowSeq = 0;
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
    rows.forEach(function (row) { row.index.removeAttribute("aria-invalid"); });
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
    option("", emptyLabel);
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
    if (p === "winams.stub_memory_limit_bytes") return "bytes";
    if (p === "winams.stub_memory_headroom_ratio") return "ratio";
    return "string";
  }
  function display(p, value) {
    var k = kind(p);
    if (k === "lines") return Array.isArray(value) ? value.join("\n") : "";
    if (k === "boolean") return typeof value === "boolean" ? String(value) : "";
    if (k === "bytes" || k === "ratio") return typeof value === "number" ? String(value) : "";
    return typeof value === "string" ? value : "";
  }
  function convert(p, value) {
    var k = kind(p), number = Number(value);
    if (k === "boolean") return value === "true";
    if (k === "lines") return value.split(/\r?\n/).map(function (v) { return v.trim(); }).filter(Boolean);
    if (k === "bytes") {
      if (!/^\d+$/.test(value) || !Number.isSafeInteger(number) || number < 1)
        throw error("Stub 内存上限必须是大于 0 的整数（字节）。", field(p));
      return number;
    }
    if (k === "ratio") {
      if (!/^\d+(\.\d+)?$/.test(value) || number > 1)
        throw error("Stub 内存余量比例必须是 0 至 1 之间的数。", field(p));
      return number;
    }
    return value;
  }
  function ensure(container, key) {
    if (!own(container, key)) Object.defineProperty(container, key, { value: VERSIONED[key] ? { version: "1" } : {}, writable: true, enumerable: true, configurable: true });
    return container[key];
  }
  function entryList() {
    return object(draft.interfaces) && Array.isArray(draft.interfaces.entries) ? draft.interfaces.entries : null;
  }
  function accessValue(entry) {
    if (!object(entry) || (!own(entry, "direction") && !own(entry, "pointee_access_order"))) return "";
    var order = own(entry, "pointee_access_order") ? entry.pointee_access_order :
      entry.direction === "in" ? "read_only" : entry.direction === "out" ? "write_only" : null;
    var index = ACCESS.findIndex(function (a) { return a[0] === entry.direction && a[1] === order; });
    return index >= 0 ? String(index) : "custom";
  }
  function rowState(row) { return [row.fn.value, row.index.value, row.name.value, row.access.value]; }
  function entryState() { return JSON.stringify(rows.map(rowState)); }
  function entryControl(row, key, label, tag) {
    var wrap = document.createElement("div"), caption = document.createElement("label"), input = document.createElement(tag);
    wrap.className = "field";
    input.id = "ut-entry-" + row.id + "-" + key;
    caption.htmlFor = input.id; caption.textContent = label;
    input.autocomplete = "off";
    wrap.appendChild(caption); wrap.appendChild(input); row.node.appendChild(wrap);
    return input;
  }
  function addRow(entry, fresh) {
    var row = { id: ++rowSeq, source: entry, node: document.createElement("div") };
    var values = object(entry) ? entry : {};
    row.node.className = "ut-entry-row"; row.node.setAttribute("role", "listitem");
    row.fn = entryControl(row, "function", "函数名", "input");
    row.fn.value = typeof values.function === "string" ? values.function : "";
    row.index = entryControl(row, "index", "参数序号", "input");
    row.index.type = "number"; row.index.min = "0"; row.index.step = "1";
    row.index.value = Number.isInteger(values.parameter_index) ? String(values.parameter_index) : "";
    row.name = entryControl(row, "name", "参数名（可选）", "input");
    row.name.value = typeof values.parameter_name === "string" ? values.parameter_name : "";
    row.access = entryControl(row, "access", "读写方向", "select");
    [["", "请选择"]].concat(ACCESS.map(function (a, i) { return [String(i), a[2]]; })).forEach(function (pair) {
      var option = document.createElement("option");
      option.value = pair[0]; option.textContent = pair[1];
      row.access.appendChild(option);
    });
    var access = accessValue(entry);
    if (access === "custom") {
      var kept = document.createElement("option");
      kept.value = "custom"; kept.disabled = true;
      kept.textContent = "当前值不受支持：" + String(values.direction) + " / " + String(values.pointee_access_order);
      row.access.appendChild(kept);
    }
    row.access.value = access;
    row.remove = document.createElement("button");
    row.remove.type = "button"; row.remove.className = "btn"; row.remove.textContent = "删除";
    row.remove.setAttribute("aria-label", "删除这条指针参数声明");
    row.node.appendChild(row.remove);
    // A fresh row has no saved form, so it is always projected.
    row.snapshot = fresh ? null : JSON.stringify(rowState(row));
    [row.fn, row.index, row.name, row.access].forEach(function (control) {
      ["input", "change"].forEach(function (event) { control.addEventListener(event, edited); });
    });
    row.remove.addEventListener("click", function () {
      if (!editable()) return;
      row.node.remove();
      rows = rows.filter(function (other) { return other !== row; });
      edited();
    });
    [row.fn, row.index, row.name, row.access, row.remove].forEach(function (control) {
      control.disabled = busy || !LMUt.can("configure");
    });
    $("ut-interface-entries").appendChild(row.node);
    rows.push(row);
    return row;
  }
  // Untouched rows keep their original value; edited rows merge onto it so unknown keys survive.
  function rowValue(row) {
    if (row.snapshot !== null && JSON.stringify(rowState(row)) === row.snapshot) return row.source;
    var out = object(row.source) ? Object.assign({}, row.source) : {};
    var fn = row.fn.value.trim(), index = row.index.value.trim(), name = row.name.value.trim(), access = row.access.value;
    if (fn) out.function = fn; else delete out.function;
    if (!index) delete out.parameter_index;
    else if (!/^\d+$/.test(index) || !Number.isSafeInteger(Number(index)))
      throw error("参数序号必须是 0 或更大的整数。", row.index);
    else out.parameter_index = Number(index);
    if (name) out.parameter_name = name; else delete out.parameter_name;
    if (access && access !== "custom") {
      var pair = ACCESS[Number(access)];
      out.direction = pair[0];
      if (pair[0] === "inout" || own(out, "pointee_access_order")) out.pointee_access_order = pair[1];
    } else if (!access) {
      delete out.direction; delete out.pointee_access_order;
    }
    return out;
  }
  function renderEntries() {
    $("ut-interface-entries").replaceChildren();
    rows = [];
    (entryList() || []).forEach(function (entry) { addRow(entry, false); });
    entriesSnapshot = entryState();
  }
  function legacyFields() {
    var found = [];
    if (own(draft, "baseline")) found.push("baseline");
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
      var value = display(p, get(p));
      field(p).value = value;
      snapshots[p] = value;
    });
    renderEntries();
    legacyView();
    policyView();
    configurationDetails();
  }
  function policyView() {
    POLICY.forEach(function (key) { field("test_policy." + key).required = true; });
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
    var edits = FIELDS.filter(function (p) { return field(p).value !== snapshots[p]; })
      .map(function (p) { var value = field(p).value.trim(); return [p, value ? convert(p, value) : null]; });
    var entries = entryState() !== entriesSnapshot ? rows.map(rowValue) : null;
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
    if (entries) {
      group("interfaces");
      if (entries.length) ensure(draft, "interfaces").entries = entries;
      else if (own(draft, "interfaces")) {
        delete draft.interfaces.entries;
        if (!Object.keys(draft.interfaces).length) delete draft.interfaces;
      }
    }
    applyAbi();
    if (refreshFields !== false) renderFields();
    else {
      FIELDS.forEach(function (p) { snapshots[p] = field(p).value; });
      if (entries) rows.forEach(function (row, i) { row.source = entries[i]; row.snapshot = JSON.stringify(rowState(row)); });
      entriesSnapshot = entryState();
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
      if (typeof get("test_policy." + key) !== "boolean") throw error("请明确选择每项测试要求的是或否。", field("test_policy." + key));
    });
    if (get("test_policy.combination_group") && !get("test_policy.mcdc"))
      throw error("MC/DC 组合分组选择是时，MC/DC 也必须选择是。", field("test_policy.combination_group"));
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
      (!choices && p === "build.profile"); });
    $("ut-m-build-abi").disabled = locked || readonly || !choices;
    rows.forEach(function (row) {
      [row.fn, row.index, row.name, row.access, row.remove].forEach(function (control) { control.disabled = locked || readonly; });
    });
    ["ut-interface-add", "ut-legacy-remove"].forEach(function (id) { $(id).disabled = locked || readonly; });
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
  function edited() {
    if (!editable()) return;
    clearError();
    try { syncForm(false); } catch (e) { fail(e.message, e.node); }
    changed();
  }
  FIELDS.map(field).concat([$("ut-m-build-abi")]).forEach(function (node) {
    ["input", "change"].forEach(function (event) { node.addEventListener(event, edited); });
  });
  $("ut-interface-add").addEventListener("click", function () {
    if (!editable()) return;
    var row = addRow(null, true);
    edited();
    row.fn.focus();
  });
  $("ut-legacy-remove").addEventListener("click", function () {
    if (!editable()) return;
    clearError();
    try {
      syncForm();
      delete draft.baseline;
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
