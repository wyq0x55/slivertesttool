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
    error:    { cls: "error",     label: "生成失败" },
    cancelled: { cls: "cancelled", label: "已取消" }
  };
  var POLL_MS = 2500;

  var state = { scenario: "", status: "" };
  var rows = [];
  var canEdit = false;
  var listRequest = 0;
  var generating = false;
  var currentDraft = null;
  var detailId = null;
  var detailEpoch = 0;
  var detailTimer = null;
  var detailBusy = false;
  var pollAttempts = 0;

  function esc(s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;",
               "'": "&#39;" }[c];
    });
  }

  function stamp(iso) {
    return String(iso || "").replace("T", " ").replace("Z", "").split(".")[0];
  }

  function draftStatus(d) {
    if (d.status === "running") {
      var job = d.meta && d.meta.job || {};
      if (job.cancel_requested) return { cls: "running", label: "等待取消" };
      return job.state === "queued" ? { cls: "queued", label: "排队中" }
        : { cls: "running", label: "生成中" };
    }
    return STATUS[d.status] || { cls: "notask", label: d.status };
  }

  function pill(d) {
    var s = draftStatus(d);
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
    var requestId = ++listRequest;
    collectFilters();
    var query = { project_id: pid };
    if (state.scenario) query.scenario = state.scenario;
    if (state.status) query.status = state.status;
    var tbody = document.getElementById("lm-ai-rows");
    try {
      var loaded = await LMApi.listAiDrafts(query);
      if (requestId !== listRequest) return;
      rows = loaded;
    } catch (ex) {
      if (requestId !== listRequest) return;
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
        "<td>#" + esc(d.id) + "</td>" +
        "<td>" + esc(SCENARIO_ZH[d.scenario] || d.scenario) + "</td>" +
        "<td>" + pill(d) + "</td>" +
        "<td>" + (d.meta && d.meta.rounds ? esc(d.meta.rounds) : "—") + "</td>" +
        "<td>" + esc(stamp(d.created_at)) + "</td>" +
        '<td class="muted" style="max-width:340px;overflow:hidden;' +
        'text-overflow:ellipsis;white-space:nowrap" title="' +
        esc(note) + '">' + esc(note || "—") + "</td>" +
        '<td><button class="btn small" data-view="' + esc(d.id) + '">查看</button></td>' +
        "</tr>";
    }).join("");
    tbody.querySelectorAll("[data-view]").forEach(function (b) {
      b.addEventListener("click", function () { openDetail(Number(b.dataset.view)); });
    });
  }

  // ---------------------------------------------------------------- detail
  var editMode = false;

  function detailMatches(id, epoch) {
    return detailId === id && detailEpoch === epoch;
  }

  function stopDetailPoll() {
    if (detailTimer !== null) clearTimeout(detailTimer);
    detailTimer = null;
  }

  function restoreOutput() {
    var edited = document.getElementById("lm-ai-d-output-edit");
    if (!edited) return;
    var pre = document.createElement("pre");
    pre.id = "lm-ai-d-output";
    pre.className = "term";
    edited.replaceWith(pre);
  }

  function syncActions() {
    var d = currentDraft;
    var enabled = canEdit && !!d && !detailBusy;
    var actionable = enabled && (d.status === "pending" || d.status === "error");
    document.getElementById("lm-ai-d-approve").disabled = !actionable || editMode || !d.output;
    document.getElementById("lm-ai-d-reject").disabled = !actionable || editMode;
    document.getElementById("lm-ai-d-edit").disabled = !actionable;
    document.getElementById("lm-ai-d-cancel").disabled = !enabled || d.status !== "running" ||
      !!(d.meta && d.meta.job && d.meta.job.cancel_requested);
    document.getElementById("lm-ai-d-retry").disabled = !enabled ||
      (d.status !== "error" && d.status !== "cancelled") || editMode;
  }

  function scheduleDetailPoll(id, epoch) {
    stopDetailPoll();
    if (!detailMatches(id, epoch) || !currentDraft || currentDraft.status !== "running" || detailBusy) return;
    if (pollAttempts >= 240) {
      document.getElementById("lm-ai-d-action-status").textContent = "仍在生成，请点击刷新查看进度。";
      return;
    }
    detailTimer = setTimeout(async function () {
      detailTimer = null;
      if (!detailMatches(id, epoch) || detailBusy || editMode) return;
      pollAttempts++;
      try {
        var d = await LMApi.getAiDraft(id);
        if (!detailMatches(id, epoch)) return;
        renderDetail(d);
      } catch (ex) {
        if (!detailMatches(id, epoch)) return;
        document.getElementById("lm-ai-d-action-status").textContent = "进度刷新失败：" + ex.message;
      }
      scheduleDetailPoll(id, epoch);
    }, POLL_MS);
  }

  async function openDetail(id) {
    if (!Number.isSafeInteger(id) || id < 1) return;
    stopDetailPoll();
    var epoch = ++detailEpoch;
    detailId = id;
    currentDraft = null;
    detailBusy = false;
    pollAttempts = 0;
    restoreOutput();
    editMode = false;
    syncActions();
    var d;
    try { d = await LMApi.getAiDraft(id); }
    catch (ex) {
      if (detailMatches(id, epoch)) {
        document.getElementById("lm-ai-d-action-status").textContent = ex.message;
        toast(ex.message, false);
      }
      return;
    }
    if (!detailMatches(id, epoch)) return;
    if (d.id !== id || Number(d.project_id) !== Number(pid)) {
      document.getElementById("lm-ai-d-action-status").textContent = "草稿不属于当前项目。";
      toast("草稿不属于当前项目。", false);
      return;
    }
    renderDetail(d);
    scheduleDetailPoll(id, epoch);
  }

  function renderDetail(d) {
    currentDraft = d;
    document.querySelector(".lm-ai-drafts").hidden = true;
    var sec = document.getElementById("lm-ai-detail");
    sec.hidden = false;
    restoreOutput();
    editMode = false;
    document.getElementById("lm-ai-d-action-status").textContent =
      d.meta && d.meta.job && d.meta.job.cancel_requested
        ? "取消已请求；正在进行的请求完成后停止，请等待状态更新。" : "";

    document.getElementById("lm-ai-d-title").textContent =
      "草稿 #" + d.id + " · " + (SCENARIO_ZH[d.scenario] || d.scenario);
    document.getElementById("lm-ai-d-sub").textContent =
      d.status === "running"
        ? draftStatus(d).label + " · " + ((d.meta && d.meta.progress && d.meta.progress.message) || "等待状态更新…")
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
      el.className = "pill st-" + draftStatus(d).cls;
      el.textContent = draftStatus(d).label;
    });

    renderRefs(d);

    // Only pending/error drafts can still be decided; terminal states show
    // disabled verbs so the affordance matches the server's rule.
    var approve = document.getElementById("lm-ai-d-approve");
    var reject = document.getElementById("lm-ai-d-reject");
    var editBtn = document.getElementById("lm-ai-d-edit");
    syncActions();
    editBtn.textContent = "编辑输出…";
    editBtn.classList.remove("primary");
    approve.onclick = function () { return decide(d, "approve"); };
    reject.onclick = function () { return decide(d, "reject"); };
    editBtn.onclick = function () { return toggleEdit(d); };
    document.getElementById("lm-ai-d-cancel").onclick = function () { return recover(d, "cancel"); };
    document.getElementById("lm-ai-d-retry").onclick = function () { return recover(d, "retry"); };
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
  async function toggleEdit(d) {
    if (!canEdit || detailBusy || currentDraft !== d ||
        (d.status !== "pending" && d.status !== "error")) return;
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
      syncActions();
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
    detailBusy = true;
    var epoch = ++detailEpoch;
    syncActions();
    try {
      var updated = await LMApi.updateAiDraft(d.id, parsed);
      if (!detailMatches(d.id, epoch)) return;
      toast("已保存修改（审核记录中标记 edited）", true);
      renderDetail(updated);
    } catch (ex) {
      if (!detailMatches(d.id, epoch)) return;
      document.getElementById("lm-ai-d-action-status").textContent = ex.message;
      toast(ex.message, false);
    } finally {
      if (detailMatches(d.id, epoch)) { detailBusy = false; syncActions(); }
    }
  }

  function closeDetail() {
    stopDetailPoll();
    detailEpoch++;
    detailId = null;
    currentDraft = null;
    detailBusy = false;
    editMode = false;
    restoreOutput();
    document.getElementById("lm-ai-detail").hidden = true;
    document.querySelector(".lm-ai-drafts").hidden = false;
    loadUsage();
  }

  async function decide(d, action) {
    if (!canEdit || detailBusy || editMode || currentDraft !== d ||
        (d.status !== "pending" && d.status !== "error")) return;
    detailBusy = true;
    var epoch = ++detailEpoch;
    syncActions();
    var status = document.getElementById("lm-ai-d-action-status");
    status.textContent = "";
    try {
      if (action === "approve") {
      var refs = selectedRefs();
      if (refs && !refs.length) throw new Error("请至少勾选一条手顺后通过。");
      var body = refs && refs.length < (d.output.procedures || []).length
        ? "将只落库勾选的 " + refs.length + " 条手顺，其余记为「未勾选（部分通过）」。"
        : "草稿内容将经平台服务层写入（测试行 / steps / SBS revision / lib / 评论）。";
      var ok = await LMUI.confirm({
        title: "通过并落库",
        body: body,
        confirmText: "通过"
      });
      if (!ok || !detailMatches(d.id, epoch)) return;
        await LMApi.approveAiDraft(d.id, refs);
        if (!detailMatches(d.id, epoch)) return;
        toast("已通过并落库", true);
        closeDetail();
        load();
      return;
    }
    var note = await LMUI.prompt({
      title: "驳回草稿（必填原因）",
      input: { value: "" }
    });
    if (!detailMatches(d.id, epoch)) return;
    if (!note || !String(note).trim()) {
      if (note !== null) toast("驳回必须填写原因", false);
      return;
    }
      await LMApi.rejectAiDraft(d.id, String(note).trim());
      if (!detailMatches(d.id, epoch)) return;
      toast("已驳回", true);
      closeDetail();
      load();
    } catch (ex) {
      if (!detailMatches(d.id, epoch)) return;
      status.textContent = ex.message + "；如来源行已变更，请从矩阵重新生成草稿。";
      toast(ex.message, false);
    } finally {
      if (detailMatches(d.id, epoch)) { detailBusy = false; syncActions(); }
    }
  }

  async function recover(d, action) {
    if (!canEdit || detailBusy || editMode || currentDraft !== d) return;
    if (action === "cancel" && (d.status !== "running" ||
        (d.meta && d.meta.job && d.meta.job.cancel_requested))) return;
    if (action === "retry" && d.status !== "error" && d.status !== "cancelled") return;
    detailBusy = true;
    stopDetailPoll();
    var epoch = ++detailEpoch;
    syncActions();
    var status = document.getElementById("lm-ai-d-action-status");
    status.textContent = action === "cancel" ? "正在请求取消；正在进行的请求会完成后停止。"
      : "正在按原来源快照重试；来源已变更时请从矩阵新建草稿。";
    try {
      var updated = await (action === "cancel" ? LMApi.cancelAiDraft(d.id) : LMApi.retryAiDraft(d.id));
      if (!detailMatches(d.id, epoch)) return;
      renderDetail(updated);
      load();
    } catch (ex) {
      if (!detailMatches(d.id, epoch)) return;
      status.textContent = ex.message;
      toast(ex.message, false);
    } finally {
      if (detailMatches(d.id, epoch)) {
        detailBusy = false;
        syncActions();
        pollAttempts = 0;
        scheduleDetailPoll(d.id, epoch);
      }
    }
  }

  // ------------------------------------------------------------ generation
  function positiveId(raw, label) {
    if (!/^\d+$/.test(raw) || !Number.isSafeInteger(Number(raw)) || Number(raw) < 1) {
      throw new Error(label + "必须为正整数");
    }
    return Number(raw);
  }

  function collectPayload(scenario) {
    var value = function (id) { return document.getElementById(id).value.trim(); };
    var payload = {};
    var selected = scenario === "procedure" || scenario === "lib" || scenario === "failure";
    if (selected) {
      var rawIds = value("lm-ai-gen-item-ids");
      if (!rawIds) throw new Error("请选择矩阵中的已保存测试行，或填写行 ID");
      var ids = rawIds.split(/[\s,，]+/).map(function (raw) { return positiveId(raw, "行 ID "); });
      if (ids.length > 200 || new Set(ids).size !== ids.length) throw new Error("选择 1–200 条不重复的已保存行");
      if (scenario === "failure") {
        if (ids.length !== 1) throw new Error("失败分析只选择一条测试行");
        payload.item_id = ids[0];
        payload.task_key = value("lm-ai-gen-task-key");
        if (!payload.task_key) throw new Error("请填写包含归档证据的任务标识");
      } else payload.item_ids = ids;
    }
    if (scenario === "viewpoint") {
      payload.doc_text = value("lm-ai-gen-doc-text");
      payload.source_name = value("lm-ai-gen-source-name");
      payload.source_revision = value("lm-ai-gen-source-revision");
      if (!payload.doc_text || !payload.source_name || !payload.source_revision) {
        throw new Error("请填写设计文档摘录、来源名称和版本");
      }
    }
    if (scenario === "lib") {
      payload.proposal = value("lm-ai-gen-proposal");
      if (!payload.proposal) throw new Error("请填写复用提议及目的");
    }
    var modelRaw = value("lm-ai-gen-model-id");
    if (modelRaw && (scenario === "procedure" || scenario === "sbs" || scenario === "lib")) {
      payload.model_id = positiveId(modelRaw, "已保存模型 ID ");
    }
    if (scenario === "sbs" && !payload.model_id) throw new Error("请选择已保存的 bundle 模型 ID");
    var raw = value("lm-ai-gen-payload");
    if (raw) {
      var advanced;
      try { advanced = JSON.parse(raw); }
      catch (ex) { throw new Error("源码摘录 JSON 无效：" + ex.message); }
      if (!advanced || typeof advanced !== "object" || Array.isArray(advanced) ||
          Object.keys(advanced).some(function (key) { return key !== "source_files"; })) {
        throw new Error("高级 JSON 仅支持 source_files 摘录；项目上下文与版本由服务器组装");
      }
      if (advanced.source_files) {
        if (scenario !== "sbs" && scenario !== "procedure" && scenario !== "lib") {
          throw new Error("此场景不接受源码摘录");
        }
        var files = advanced.source_files;
        if (!files || typeof files !== "object" || Array.isArray(files) || Object.keys(files).length > 32 ||
            Object.keys(files).some(function (name) { return !name.trim() || typeof files[name] !== "string"; })) {
          throw new Error("source_files 必须为文件名到文本的映射，最多 32 个摘录");
        }
        if (Object.keys(files).reduce(function (length, name) { return length + files[name].length; }, 0) > 256000) {
          throw new Error("源码摘录总长度不能超过 256000 字符");
        }
        payload.source_files = files;
      }
    }
    if (scenario === "sbs" && (!payload.source_files || !Object.keys(payload.source_files).length)) {
      throw new Error("请在高级 JSON 中提供 source_files 源码摘录");
    }
    if (payload.doc_text && payload.doc_text.length > 256000) throw new Error("文档摘录不能超过 256000 字符");
    if (payload.source_files || scenario === "lib") {
      var name = value("lm-ai-gen-source-name");
      var revision = value("lm-ai-gen-source-revision");
      if (name) payload.source_name = name;
      if (revision) payload.source_revision = revision;
    }
    return payload;
  }

  function syncGenerationControls() {
    var scenario = document.getElementById("lm-ai-gen-scenario").value;
    document.getElementById("lm-ai-open-gen").disabled = !canEdit || generating;
    document.getElementById("lm-ai-gen-submit").disabled = !canEdit || generating;
    document.querySelectorAll("[data-ai-scenarios]").forEach(function (node) {
      node.hidden = node.dataset.aiScenarios.split(" ").indexOf(scenario) < 0;
    });
    document.querySelectorAll("#lm-ai-gen-panel input, #lm-ai-gen-panel textarea, #lm-ai-gen-panel select")
      .forEach(function (node) { node.disabled = !canEdit || generating; });
  }

  function bindGenerate() {
    var panel = document.getElementById("lm-ai-gen-panel");
    document.getElementById("lm-ai-open-gen").addEventListener("click", function () {
      if (!canEdit || generating) return;
      panel.hidden = !panel.hidden;
    });
    document.getElementById("lm-ai-gen-scenario").addEventListener("change", syncGenerationControls);
    document.getElementById("lm-ai-gen-submit").addEventListener("click", async function () {
      if (!canEdit || generating) return;
      var scenario = document.getElementById("lm-ai-gen-scenario").value;
      var statusEl = document.getElementById("lm-ai-gen-status");
      var payload;
      try { payload = collectPayload(scenario); }
      catch (e) { statusEl.textContent = e.message; return; }
      generating = true;
      syncGenerationControls();
      var epoch = detailEpoch;
      statusEl.textContent = "已提交，等待 worker…";
      var draft;
      try {
        await LMReady;
        draft = await LMApi.createAiDraft(scenario, Number(pid), payload);
      } catch (ex) {
        statusEl.textContent = ex.message;
        if (ex.details && Number.isSafeInteger(ex.details.draft_id) && ex.details.draft_id > 0) {
          statusEl.textContent += "（草稿 #" + ex.details.draft_id + "，可在列表查看并重试）";
          load();
        }
        toast(ex.message, false);
        return;
      } finally {
        generating = false;
        syncGenerationControls();
      }
      statusEl.textContent = draft.status === "error"
        ? (draft.error || "生成失败") + "（草稿 #" + draft.id + "）"
        : draftStatus(draft).label + " · 草稿 #" + draft.id + "，请人工审核";
      load();
      if (detailEpoch === epoch && detailId === null) {
        panel.hidden = true;
        openDetail(draft.id);
      }
    });
    syncGenerationControls();
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
        if (!canEdit) return;
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
  async function loadPermissions() {
    try {
      await LMReady;
      var project = await LMApi.getProject(Number(pid));
      canEdit = project.project.is_editable !== false &&
        (project.role === "project_admin" || project.role === "editor" ||
         project.role === "system_admin" || !!(LM.user && LM.user.is_system_admin));
      if (!canEdit) document.getElementById("lm-ai-gen-status").textContent =
        "当前项目或角色仅可查看草稿；生成和审核需要编辑权限。";
    } catch (ex) {
      canEdit = false;
      document.getElementById("lm-ai-gen-status").textContent = "权限加载失败：" + ex.message;
    }
    document.getElementById("lm-ai-signals-save").disabled = !canEdit;
    document.getElementById("lm-ai-signals-text").disabled = !canEdit;
    syncGenerationControls();
    syncActions();
  }

  document.getElementById("lm-ai-refresh").addEventListener("click", function () {
    load();
    loadUsage();
    if (detailId !== null && !editMode && !detailBusy) openDetail(detailId);
  });
  document.getElementById("lm-ai-d-refresh").addEventListener("click", function () {
    if (detailId !== null && !editMode && !detailBusy) openDetail(detailId);
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
  document.getElementById("lm-ai-signals-save").disabled = true;
  document.getElementById("lm-ai-signals-text").disabled = true;
  bindSettings();
  bindSignals();
  load();
  loadUsage();
  loadPermissions();
  var linkedDraft = new URLSearchParams(window.location.search).get("draft");
  if (linkedDraft && /^\d+$/.test(linkedDraft)) openDetail(Number(linkedDraft));
  window.addEventListener("pagehide", function () {
    stopDetailPoll();
    detailEpoch++;
    detailId = null;
  });
})(window, document);
