/*
 * AI draft review page (LAN Test Matrix).
 *
 * The review surface for the agent pipeline: list drafts, inspect what the
 * model saw and what the machine validation said, then approve (applies
 * through the existing service layer) or reject with a mandatory note.
 * Humans only decide here — they never transcribe.
 *
 * Status vocabulary maps onto the platform's pill classes rather than
 * inventing new ones: pending→queued, approved→passed, rejected→cancelled,
 * error→error.
 */
(function (window, document) {
  "use strict";

  var pid = (document.querySelector(".lm-ai-drafts") || {}).dataset
    ? document.querySelector(".lm-ai-drafts").dataset.project : null;
  if (!pid) return;

  var SCENARIO_ZH = { viewpoint: "观点抽取", procedure: "手顺生成",
                      sbs: "SBS 构筑", lib: "lib 编写", failure: "失败分析" };
  var STATUS = {
    pending:  { cls: "queued",   label: "待审" },
    approved: { cls: "passed",   label: "已通过" },
    rejected: { cls: "cancelled", label: "已驳回" },
    error:    { cls: "error",    label: "生成失败" }
  };

  var state = { scenario: "", status: "" };
  var rows = [];

  function esc(s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;",
               "'": "&#39;" }[c];
    });
  }

  function stamp(iso) {
    return String(iso || "").replace("T", " ").replace("Z", "").split(".")[0];
  }

  function pill(status) {
    var s = STATUS[status] || { cls: "notask", label: status };
    return '<span class="pill st-' + s.cls + '"><span class="dot"></span>' +
      esc(s.label) + "</span>";
  }

  function toast(msg, ok) { LMUI.toast(msg, ok); }

  // ------------------------------------------------------------------ list
  function collectFilters() {
    state.scenario =
      (document.getElementById("lm-ai-f-scenario") || {}).value || "";
    document.querySelectorAll("#lm-ai-f-seg button").forEach(function (b) {
      if (b.classList.contains("on")) state.status = b.dataset.status || "";
    });
  }

  async function load() {
    collectFilters();
    var query = { project_id: pid };
    if (state.scenario) query.scenario = state.scenario;
    if (state.status) query.status = state.status;
    var tbody = document.getElementById("lm-ai-rows");
    try {
      rows = await LMApi.listAiDrafts(query);
    } catch (ex) {
      tbody.innerHTML = '<tr><td colspan="7" class="muted">' +
        esc(ex.message) + "</td></tr>";
      return;
    }
    document.getElementById("lm-ai-empty").hidden = rows.length > 0;
    if (!rows.length) {
      tbody.innerHTML = '<tr><td colspan="7" class="muted">暂无草稿</td></tr>';
      return;
    }
    tbody.innerHTML = rows.map(function (d) {
      var note = d.error || d.review_note || "";
      return "<tr>" +
        "<td>#" + d.id + "</td>" +
        "<td>" + esc(SCENARIO_ZH[d.scenario] || d.scenario) + "</td>" +
        "<td>" + pill(d.status) + "</td>" +
        "<td>" + (d.meta && d.meta.rounds ? esc(d.meta.rounds) : "—") + "</td>" +
        "<td>" + esc(stamp(d.created_at)) + "</td>" +
        '<td class="muted" style="max-width:340px;overflow:hidden;' +
        'text-overflow:ellipsis;white-space:nowrap" title="' +
        esc(note) + '">' + esc(note || "—") + "</td>" +
        '<td><button class="btn small" data-view="' + d.id + '">查看</button></td>' +
        "</tr>";
    }).join("");
    tbody.querySelectorAll("[data-view]").forEach(function (b) {
      b.addEventListener("click", function () { openDetail(Number(b.dataset.view)); });
    });
  }

  // ---------------------------------------------------------------- detail
  async function openDetail(id) {
    var d;
    try { d = await LMApi.getAiDraft(id); }
    catch (ex) { toast(ex.message, false); return; }
    document.querySelector(".lm-ai-drafts").hidden = true;
    var sec = document.getElementById("lm-ai-detail");
    sec.hidden = false;

    document.getElementById("lm-ai-d-title").textContent =
      "草稿 #" + d.id + " · " + (SCENARIO_ZH[d.scenario] || d.scenario);
    document.getElementById("lm-ai-d-sub").textContent =
      "创建于 " + stamp(d.created_at);
    document.getElementById("lm-ai-d-scenario").textContent =
      SCENARIO_ZH[d.scenario] || d.scenario;
    document.getElementById("lm-ai-d-model").textContent =
      (d.meta && d.meta.model) || "—";
    document.getElementById("lm-ai-d-rounds").textContent =
      (d.meta && d.meta.rounds) || "—";
    document.getElementById("lm-ai-d-review").textContent =
      d.reviewed_at ? stamp(d.reviewed_at) : "—";
    document.getElementById("lm-ai-d-applied").textContent = d.applied_result
      ? JSON.stringify(d.applied_result) : "—";
    document.getElementById("lm-ai-d-error").textContent = d.error || "—";
    document.getElementById("lm-ai-d-log").textContent = d.meta && d.meta.log
      ? JSON.stringify(d.meta.log, null, 1) : "（无校验日志）";
    document.getElementById("lm-ai-d-output").textContent = d.output
      ? JSON.stringify(d.output, null, 2) : (d.error || "（无输出）");
    document.getElementById("lm-ai-d-input").textContent = d.input
      ? JSON.stringify(d.input, null, 2) : "—";
    ["lm-ai-d-hstatus", "lm-ai-d-status"].forEach(function (id2) {
      var el = document.getElementById(id2);
      el.className = "pill st-" + (STATUS[d.status] || { cls: "notask" }).cls;
    });

    // Only pending/error drafts can still be decided; terminal states show
    // disabled verbs so the affordance matches the server's rule.
    var actionable = d.status === "pending" || d.status === "error";
    var approve = document.getElementById("lm-ai-d-approve");
    var reject = document.getElementById("lm-ai-d-reject");
    approve.disabled = !actionable;
    reject.disabled = !actionable && d.status !== "error";
    approve.onclick = function () { decide(d, "approve"); };
    reject.onclick = function () { decide(d, "reject"); };
  }

  function closeDetail() {
    document.getElementById("lm-ai-detail").hidden = true;
    document.querySelector(".lm-ai-drafts").hidden = false;
  }

  async function decide(d, action) {
    if (action === "approve") {
      var ok = await LMUI.confirm({
        title: "通过并落库",
        body: "草稿内容将经平台服务层写入（测试行 / steps / SBS revision / lib / 评论）。",
        confirmText: "通过"
      });
      if (!ok) return;
      try {
        var applied = await LMApi.approveAiDraft(d.id);
        toast("已通过并落库", true);
        closeDetail();
        load();
      } catch (ex) { toast(ex.message, false); }
      return;
    }
    var note = await LMUI.prompt({
      title: "驳回草稿（必填原因）",
      input: { value: "" }
    });
    if (!note || !String(note).trim()) {
      if (note !== null) toast("驳回必须填写原因", false);
      return;
    }
    try {
      await LMApi.rejectAiDraft(d.id, String(note).trim());
      toast("已驳回", true);
      closeDetail();
      load();
    } catch (ex) { toast(ex.message, false); }
  }

  // ------------------------------------------------------------ generation
  function bindGenerate() {
    var panel = document.getElementById("lm-ai-gen-panel");
    document.getElementById("lm-ai-open-gen").addEventListener("click", function () {
      panel.hidden = !panel.hidden;
    });
    document.getElementById("lm-ai-gen-submit").addEventListener("click", async function () {
      var scenario = document.getElementById("lm-ai-gen-scenario").value;
      var raw = document.getElementById("lm-ai-gen-payload").value;
      var statusEl = document.getElementById("lm-ai-gen-status");
      var payload;
      try { payload = JSON.parse(raw); }
      catch (e) { statusEl.textContent = "payload 不是合法 JSON：" + e.message; return; }
      statusEl.textContent = "生成中（含机器校验与重试，最长约 1–2 分钟）…";
      try {
        var draft = await LMApi.createAiDraft(scenario, pid, payload);
        statusEl.textContent = "已生成草稿 #" + draft.id;
        panel.hidden = true;
        state.status = "";
        document.getElementById("lm-ai-gen-payload").value = "";
        load();
        openDetail(draft.id);
      } catch (ex) {
        statusEl.textContent = "";
        toast(ex.message, false);
      }
    });
  }

  // --------------------------------------------------------------- settings
  function bindSettings() {
    var box = document.getElementById("lm-ai-settings");
    if (!box) return;  // admin-only block
    LMApi.aiGetSettings().then(function (cfg) {
      document.getElementById("lm-ai-set-base").value = cfg.ai_api_base || "";
      document.getElementById("lm-ai-set-key").value = cfg.ai_api_key || "";
      document.getElementById("lm-ai-set-model").value = cfg.ai_model || "";
      document.getElementById("lm-ai-set-timeout").value = cfg.ai_timeout || "";
    }).catch(function () { /* leave blank */ });
    document.getElementById("lm-ai-set-save").addEventListener("click", async function () {
      var statusEl = document.getElementById("lm-ai-set-status");
      try {
        var cfg2 = await LMApi.aiPutSettings({
          ai_api_base: document.getElementById("lm-ai-set-base").value,
          ai_api_key: document.getElementById("lm-ai-set-key").value,
          ai_model: document.getElementById("lm-ai-set-model").value,
          ai_timeout: document.getElementById("lm-ai-set-timeout").value
        });
        statusEl.textContent = "已保存（key：" + cfg2.ai_api_key + "）";
        toast("AI 设置已保存", true);
      } catch (ex) { statusEl.textContent = ""; toast(ex.message, false); }
    });
  }

  // ------------------------------------------------------------------- init
  document.getElementById("lm-ai-refresh").addEventListener("click", load);
  document.getElementById("lm-ai-d-close").addEventListener("click", closeDetail);
  document.getElementById("lm-ai-f-scenario").addEventListener("change", load);
  document.querySelectorAll("#lm-ai-f-seg button").forEach(function (b) {
    b.addEventListener("click", function () {
      document.querySelectorAll("#lm-ai-f-seg button").forEach(function (x) {
        x.classList.remove("on");
      });
      b.classList.add("on");
      load();
    });
  });

  bindGenerate();
  bindSettings();
  load();
})(window, document);