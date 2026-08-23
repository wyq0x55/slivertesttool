/*
 * AI draft review page (LAN Test Matrix).
 *
 * The review surface for the agent pipeline: list drafts, inspect what the
 * model saw and what the machine validation said, then approve (optionally
 * only the checked refs of a batch, optionally after an inline edit) or
 * reject with a mandatory note. Humans only decide here — they never
 * transcribe.
 *
 * Generation is asynchronous: POST returns a draft in ``running`` while the
 * Huey worker drives the scenario, so the submit flow polls the draft and
 * surfaces meta.progress until it reaches a terminal state. With the worker
 * in immediate mode (tests) the response already carries the finished draft
 * and the poll loop exits on its first tick.
 *
 * Status vocabulary maps onto the platform's pill classes rather than
 * inventing new ones: running→running, pending→queued, approved→passed,
 * rejected→cancelled, error→error.
 */
(function (window, document) {
  "use strict";

  var pid = (document.querySelector(".lm-ai-drafts") || {}).dataset
    ? document.querySelector(".lm-ai-drafts").dataset.project : null;
  if (!pid) return;

  var SCENARIO_ZH = { viewpoint: "观点抽取", procedure: "手顺生成",
                      sbs: "SBS 构筑", lib: "lib 编写", failure: "失败分析" };
  var STATUS = {
    running:  { cls: "running",   label: "生成中" },
    pending:  { cls: "queued",    label: "待审" },
    approved: { cls: "passed",    label: "已通过" },
    rejected: { cls: "cancelled", label: "已驳回" },
    error:    { cls: "error",     label: "生成失败" }
  };
  var POLL_MS = 2500;

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

  function fmtTok(n) {
    n = Number(n || 0);
    return n >= 10000 ? (n / 1000).toFixed(1) + "k" : String(n);
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
  var editMode = false;

  async function openDetail(id) {
    var d;
    try { d = await LMApi.getAiDraft(id); }
    catch (ex) { toast(ex.message, false); return; }
    if (d.status === "running") {
      // Generation still on the worker — follow it here instead of bouncing
      // the user back to the list.
      renderDetail(d);
      setTimeout(function () { openDetail(id); }, POLL_MS);
      return;
    }
    renderDetail(d);
  }

  function renderDetail(d) {
    document.querySelector(".lm-ai-drafts").hidden = true;
    var sec = document.getElementById("lm-ai-detail");
    sec.hidden = false;
    editMode = false;

    document.getElementById("lm-ai-d-title").textContent =
      "草稿 #" + d.id + " · " + (SCENARIO_ZH[d.scenario] || d.scenario);
    document.getElementById("lm-ai-d-sub").textContent =
      d.status === "running"
        ? "生成中 · " + ((d.meta && d.meta.progress && d.meta.progress.message) || "排队等待 worker…")
        : "创建于 " + stamp(d.created_at);
    document.getElementById("lm-ai-d-scenario").textContent =
      SCENARIO_ZH[d.scenario] || d.scenario;
    document.getElementById("lm-ai-d-model").textContent =
      (d.meta && d.meta.model) || "—";
    document.getElementById("lm-ai-d-rounds").textContent =
      (d.meta && d.meta.rounds) || "—";
    var usage = (d.meta && d.meta.usage) || null;
    document.getElementById("lm-ai-d-usage").textContent = usage
      ? "in " + fmtTok(usage.input_tokens) + " · out " + fmtTok(usage.output_tokens)
      : "—";
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

    renderRefs(d);

    // Only pending/error drafts can still be decided; terminal states show
    // disabled verbs so the affordance matches the server's rule.
    var actionable = d.status === "pending" || d.status === "error";
    var approve = document.getElementById("lm-ai-d-approve");
    var reject = document.getElementById("lm-ai-d-reject");
    var editBtn = document.getElementById("lm-ai-d-edit");
    approve.disabled = !actionable;
    reject.disabled = !actionable && d.status !== "error";
    editBtn.disabled = !actionable;
    editBtn.textContent = "编辑输出…";
    approve.onclick = function () { decide(d, "approve"); };
    reject.onclick = function () { decide(d, "reject"); };
    editBtn.onclick = function () { toggleEdit(d); };
  }

  /** Partial-approval checklist for batch procedure drafts. */
  function renderRefs(d) {
    var box = document.getElementById("lm-ai-d-refs");
    var list = document.getElementById("lm-ai-refs-list");
    var procs = d.output && Array.isArray(d.output.procedures)
      ? d.output.procedures : null;
    if (!procs || d.status === "running") {
      box.hidden = true;
      list.innerHTML = "";
      return;
    }
    box.hidden = false;
    list.innerHTML = procs.map(function (p) {
      var ref = String(p.ref || "");
      var missing = (p.missing_variables || []).map(function (m) {
        return m && m.name ? m.name : "";
      }).filter(Boolean).join("、");
      return '<label class="jchk"><input type="checkbox" data-ref="' +
        esc(ref) + '" checked><span>' + esc(ref) +
        (missing ? ' <span class="muted">（缺变量：' + esc(missing) + "）</span>" : "") +
        "</span></label>";
    }).join("");
    var all = document.getElementById("lm-ai-refs-all");
    all.checked = true;
    all.onchange = function () {
      list.querySelectorAll('input[data-ref]').forEach(function (c) {
        c.checked = all.checked;
      });
    };
  }

  function selectedRefs() {
    var box = document.getElementById("lm-ai-d-refs");
    if (box.hidden) return null;
    var checked = Array.from(
      box.querySelectorAll('input[data-ref]:checked')).map(function (c) {
      return c.dataset.ref;
    });
    return checked;
  }

  /** Inline edit: swap the output <pre> for a JSON textarea and back. */
  function toggleEdit(d) {
    var pre = document.getElementById("lm-ai-d-output");
    var btn = document.getElementById("lm-ai-d-edit");
    if (!editMode) {
      var ta = document.createElement("textarea");
      ta.id = "lm-ai-d-output-edit";
      ta.className = "term";
      ta.rows = Math.min(30, Math.max(8,
        String(JSON.stringify(d.output, null, 2)).split("\n").length));
      ta.value = JSON.stringify(d.output, null, 2);
      pre.replaceWith(ta);
      btn.textContent = "保存修改";
      btn.classList.add("primary");
      editMode = true;
      return;
    }
    var ta2 = document.getElementById("lm-ai-d-output-edit");
    var parsed;
    try { parsed = JSON.parse(ta2.value); }
    catch (e) { toast("输出不是合法 JSON：" + e.message, false); return; }
    if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) {
      toast("输出必须是 JSON 对象", false);
      return;
    }
    LMApi.updateAiDraft(d.id, parsed).then(function (updated) {
      toast("已保存修改（审核记录中标记 edited）", true);
      renderDetail(updated);
    }).catch(function (ex) { toast(ex.message, false); });
  }

  function closeDetail() {
    document.getElementById("lm-ai-detail").hidden = true;
    document.querySelector(".lm-ai-drafts").hidden = false;
    loadUsage();
  }

  async function decide(d, action) {
    if (action === "approve") {
      var refs = selectedRefs();
      var body = refs && refs.length < (d.output.procedures || []).length
        ? "将只落库勾选的 " + refs.length + " 条手顺，其余记为「未勾选（部分通过）」。"
        : "草稿内容将经平台服务层写入（测试行 / steps / SBS revision / lib / 评论）。";
      var ok = await LMUI.confirm({
        title: "通过并落库",
        body: body,
        confirmText: "通过"
      });
      if (!ok) return;
      try {
        await LMApi.approveAiDraft(d.id, refs);
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
      statusEl.textContent = "已提交，等待 worker…";
      var draft;
      try {
        draft = await LMApi.createAiDraft(scenario, pid, payload);
      } catch (ex) {
        statusEl.textContent = "";
        toast(ex.message, false);
        return;
      }
      if (draft.status === "running") {
        statusEl.textContent = "生成中（草稿 #" + draft.id + "）——可离开本页，生成完成后出现在列表";
        panel.hidden = true;
        load();
        pollDraft(draft.id, statusEl);
        return;
      }
      statusEl.textContent = draft.status === "error"
        ? "生成失败（草稿 #" + draft.id + "）"
        : "已生成草稿 #" + draft.id;
      panel.hidden = true;
      state.status = "";
      document.getElementById("lm-ai-gen-payload").value = "";
      load();
      openDetail(draft.id);
    });
  }

  /** Follow an async generation until it leaves ``running``. */
  async function pollDraft(id, statusEl) {
    for (var i = 0; i < 240; i++) {  // bounded: ~10 minutes
      await new Promise(function (r) { setTimeout(r, POLL_MS); });
      var d;
      try { d = await LMApi.getAiDraft(id); }
      catch (ex) { continue; }  // transient fetch failure: keep polling
      var msg = (d.meta && d.meta.progress && d.meta.progress.message) || "";
      if (statusEl) statusEl.textContent = "草稿 #" + id + " 生成中…" + (msg ? "（" + msg + "）" : "");
      if (d.status !== "running") {
        if (statusEl) statusEl.textContent = "";
        load();
        openDetail(id);
        return;
      }
    }
    if (statusEl) statusEl.textContent = "仍在生成中（草稿 #" + id + "）——请稍后刷新查看";
  }

  // ----------------------------------------------------------------- usage
  async function loadUsage() {
    var card = document.getElementById("lm-ai-usage");
    var body = document.getElementById("lm-ai-usage-body");
    var stats;
    try { stats = await LMApi.aiUsage(pid, 3); }
    catch (ex) { card.hidden = true; return; }
    card.hidden = false;
    document.getElementById("lm-ai-usage-range").textContent =
      "· 近 " + stats.months + " 个月 · " + stats.totals.drafts + " 份草稿";
    if (!stats.totals.drafts) {
      body.textContent = "暂无用量记录。";
      return;
    }
    var byScenario = Object.keys(stats.per_scenario).map(function (s) {
      var b = stats.per_scenario[s];
      return "<tr><td>" + esc(SCENARIO_ZH[s] || s) + "</td><td>" + b.count +
        "</td><td>" + fmtTok(b.input_tokens) + "</td><td>" +
        fmtTok(b.output_tokens) + "</td></tr>";
    }).join("");
    var byMonth = Object.keys(stats.per_month).sort().reverse().map(function (m) {
      var b = stats.per_month[m];
      return "<tr><td>" + esc(m) + "</td><td>" + b.count + "</td><td>" +
        fmtTok(b.input_tokens) + "</td><td>" + fmtTok(b.output_tokens) +
        "</td></tr>";
    }).join("");
    body.innerHTML =
      '<p class="muted" style="margin:0 0 8px">合计：输入 ' +
      fmtTok(stats.totals.input_tokens) + " tokens · 输出 " +
      fmtTok(stats.totals.output_tokens) + " tokens</p>" +
      '<div class="row wrap" style="gap:24px;align-items:flex-start">' +
      '<div style="min-width:260px"><table class="dt"><thead><tr>' +
      "<th>场景</th><th>草稿数</th><th>输入</th><th>输出</th></tr></thead>" +
      "<tbody>" + byScenario + "</tbody></table></div>" +
      '<div style="min-width:220px"><table class="dt"><thead><tr>' +
      "<th>月份</th><th>草稿数</th><th>输入</th><th>输出</th></tr></thead>" +
      "<tbody>" + byMonth + "</tbody></table></div></div>";
  }

  // --------------------------------------------------------------- signals
  function bindSignals() {
    var box = document.getElementById("lm-ai-signals");
    if (!box) return;
    LMApi.aiGetSignals(pid).then(function (entries) {
      document.getElementById("lm-ai-signals-text").value =
        (entries || []).map(function (e) {
          return [e[0], e[1], e[2]].filter(function (v, i) {
            return i < 2 || v;
          }).join(", ");
        }).join("\n");
    }).catch(function () { /* leave blank */ });
    document.getElementById("lm-ai-signals-save").addEventListener(
      "click", async function () {
        var statusEl = document.getElementById("lm-ai-signals-status");
        var entries = [];
        for (var line of document.getElementById("lm-ai-signals-text").value.split("\n")) {
          line = line.trim().replace(/[,，]\s*$/, "");
          if (!line || line.startsWith("#")) continue;
          var parts = line.split(/[,，]/).map(function (s) { return s.trim(); });
          if (parts.length < 2) {
            statusEl.textContent = "每行需要至少 2 列（表示名, 路径）：" + line;
            return;
          }
          entries.push([parts[0], parts[1], parts[2] || ""]);
        }
        try {
          var saved = await LMApi.aiPutSignals(pid, entries);
          statusEl.textContent = "已保存 " + saved.count + " 条";
          toast("信号字典已保存", true);
        } catch (ex) { statusEl.textContent = ""; toast(ex.message, false); }
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
  document.getElementById("lm-ai-refresh").addEventListener("click", function () {
    load();
    loadUsage();
  });
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
  bindSignals();
  load();
  loadUsage();
})(window, document);
