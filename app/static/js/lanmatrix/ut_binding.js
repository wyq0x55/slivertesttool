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
              "max_repair_rounds", "repair_model"];

  function $(id) { return document.getElementById(id); }
  var say = LMUt.say;
  var saving = false;

  function lock(locked) {
    KEYS.forEach(function (k) { $("ut-c-" + k).disabled = locked || !LMUt.can("configure"); });
    $("ut-config-save").disabled = locked || !LMUt.can("configure");
  }

  async function load() {
    var c = await LMApi.utGetConfig(pid);
    KEYS.forEach(function (k) { $("ut-c-" + k).value = c && c[k] != null ? c[k] : ""; });
    say(c && c.updated_at ? "最后保存于 " + LMUt.time(c.updated_at) : "尚未配置绑定");
  }

  $("ut-config-save").addEventListener("click", async function () {
    if (saving || !LMUt.can("configure")) return;
    var values = {};
    KEYS.forEach(function (k) { values[k] = $("ut-c-" + k).value.trim(); });
    values.max_repair_rounds = Number(values.max_repair_rounds);
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
