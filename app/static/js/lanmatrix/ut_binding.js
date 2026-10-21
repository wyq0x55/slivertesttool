/*
 * 设置 · 项目绑定 of a unit-test project: the ut-agent / WinAMS binding that a
 * batch freezes when it is queued.
 */
(function (window, document) {
  "use strict";

  var root = document.querySelector(".lm-ut-binding");
  if (!root) return;
  var pid = Number(root.dataset.project);
  var KEYS = ["manifest_path", "index_csv", "product_root", "work_root", "stub_builds",
              "max_repair_rounds", "repair_model", "execution_backend", "stub_mode", "stub_model",
              "stub_max_rounds", "host_clang", "host_llvm_cov", "host_llvm_profdata", "host_profile_runtime"];
  var DEFAULTS = { execution_backend: "winams", stub_mode: "deterministic", stub_max_rounds: 2,
    host_clang: "clang", host_llvm_cov: "llvm-cov", host_llvm_profdata: "llvm-profdata", max_repair_rounds: 5 };
  var HOST = ["stub_mode", "host_clang", "host_llvm_cov", "host_llvm_profdata", "host_profile_runtime"];
  var MODEL = ["stub_model", "stub_max_rounds"];

  function $(id) { return document.getElementById(id); }
  var say = LMUt.say;
  var saving = false;

  function lock(locked) {
    var clang = $("ut-c-execution_backend").value === "clang";
    var llm = clang && $("ut-c-stub_mode").value === "llm";
    $("ut-winams-controls").hidden = clang;
    $("ut-host-controls").hidden = !clang;
    $("ut-stub-llm-controls").hidden = !llm;
    $("ut-csv-repair-heading").parentElement.hidden = clang;
    KEYS.forEach(function (k) {
      $("ut-c-" + k).disabled = locked || !LMUt.can("configure") ||
        (clang && (k === "work_root" || k === "stub_builds" || k === "max_repair_rounds" || k === "repair_model")) ||
        (!clang && HOST.indexOf(k) >= 0) || (!llm && MODEL.indexOf(k) >= 0);
    });
    $("ut-config-save").disabled = locked || !LMUt.can("configure");
  }

  async function load() {
    var c = await LMApi.utGetConfig(pid);
    KEYS.forEach(function (k) { $("ut-c-" + k).value = String(c && c[k] != null ? c[k] : DEFAULTS[k] != null ? DEFAULTS[k] : ""); });
    say(c && c.updated_at ? "最后保存于 " + LMUt.time(c.updated_at) : "尚未配置绑定");
  }

  ["execution_backend", "stub_mode"].forEach(function (key) {
    $("ut-c-" + key).addEventListener("change", function () { lock(saving); });
  });

  $("ut-config-save").addEventListener("click", async function () {
    if (saving || !LMUt.can("configure")) return;
    var values = {};
    KEYS.forEach(function (k) { values[k] = $("ut-c-" + k).value.trim(); });
    values.max_repair_rounds = Number(values.max_repair_rounds);
    values.stub_max_rounds = Number(values.stub_max_rounds);
    if (values.execution_backend !== "clang") values.stub_mode = "deterministic";
    if (!Number.isInteger(values.stub_max_rounds) || values.stub_max_rounds < 1 || values.stub_max_rounds > 5) {
      say("Stub 候选轮数必须是 1 至 5 之间的整数。", true);
      return;
    }
    if (!Number.isInteger(values.max_repair_rounds) ||
        values.max_repair_rounds < 0 || values.max_repair_rounds > 20) {
      say("修正上限必须是 0 至 20 之间的整数。", true);
      return;
    }
    saving = true;
    lock(true);
    try {
      await LMApi.utPutConfig(pid, values);
      await load();
      say("绑定已保存");
    } catch (e) { say(e.message, true); }
    finally { saving = false; lock(false); }
  });

  (window.LMReady || Promise.resolve()).then(function () {
    lock(true);
    load().then(function () { lock(false); }).catch(function (e) { say(e.message, true); });
  });
})(window, document);
