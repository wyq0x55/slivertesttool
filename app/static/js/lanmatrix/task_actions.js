/* Shared task + review actions — the single definition of "what you can do to
 * a task or a review", usable from any page.
 *
 * Why this module exists
 * ----------------------
 * The workspace and the project task list show the same rows and must offer the
 * same verbs. Before this module the verbs only existed inside project_tasks.js,
 * bound to that page's `pid` closure and DOM ids, so the workspace could only
 * ever be a read-only list — "look, but go somewhere else to act". Copying the
 * handlers across would have produced two implementations that drift apart on
 * the first bug fix (a failure mode docs/TECH_DEBT_REPORT.md already names).
 *
 * So every action here is a *pure* async function taking an explicit context
 * object -- `{ projectId, taskKey }` or `{ projectId, uuid }` -- and returning a
 * plain result. No DOM, no globals, no page assumptions. Confirmation and
 * feedback are injected by the caller (see `configure`), because a confirm
 * dialog belongs to a page, not to an API call.
 *
 * Every action reports partial success honestly: a batch that cancels 8 of 10
 * tasks says so, rather than claiming success and leaving two running.
 */
(function (global) {
  "use strict";

  // Page-supplied UI hooks. The defaults are deliberately usable (native
  // confirm, no-op toast) so a page that forgets to call configure() still gets
  // correct behaviour rather than a silent crash.
  let ui = {
    confirm: (msg) => global.confirm(msg),
    prompt: (msg, def) => global.prompt(msg, def || ""),
    toast: () => {},
  };

  function configure(hooks) {
    ui = Object.assign({}, ui, hooks || {});
  }

  function need(ctx, field) {
    const v = ctx && ctx[field];
    if (v === undefined || v === null || v === "") {
      throw new Error(`task_actions: missing ${field}`);
    }
    return v;
  }

  // --- task verbs ----------------------------------------------------------
  // Which states each verb makes sense in. Centralised so the workspace and the
  // project page grey out the same buttons; a page computing this itself is how
  // "Cancel" ends up enabled on a finished run.
  const RUNNING = ["queued", "running", "pending"];

  function canCancel(task) {
    return RUNNING.indexOf(String((task && task.status) || "").toLowerCase()) >= 0;
  }
  function canRetest(task) {
    return !canCancel(task) && !!(task && (task.test_id || task.task_id));
  }
  function canDownload(task) {
    const st = String((task && task.status) || "").toLowerCase();
    return st === "success" || st === "failed" || st === "error";
  }

  async function cancelTask(ctx) {
    const pid = need(ctx, "projectId");
    const key = need(ctx, "taskKey");
    await LMApi.cancelProjectTask(pid, key);
    ui.toast("已请求取消", true);
    return { cancelled: [key] };
  }

  async function cancelTasks(ctx, keys) {
    const pid = need(ctx, "projectId");
    const list = (keys || []).filter(Boolean);
    if (!list.length) return { cancelled: [], failed: [] };
    if (!ui.confirm(`确认取消选中的 ${list.length} 个任务？`)) return null;

    const cancelled = [];
    const failed = [];
    for (const k of list) {
      try { await LMApi.cancelProjectTask(pid, k); cancelled.push(k); }
      catch (ex) { failed.push({ key: k, message: ex.message || "" }); }
    }
    // Report the shortfall rather than a blanket "done": a user told "已取消"
    // will not go back to check the two that refused.
    ui.toast(failed.length
      ? `已取消 ${cancelled.length} 个，${failed.length} 个失败`
      : `已取消 ${cancelled.length} 个`, !failed.length);
    return { cancelled, failed };
  }

  async function deleteTask(ctx, opts) {
    const pid = need(ctx, "projectId");
    const key = need(ctx, "taskKey");
    if (!(opts && opts.skipConfirm)
        && !ui.confirm(`确认删除任务 ${key}？该操作不可撤销。`)) return null;
    await LMApi.deleteProjectTask(pid, key);
    ui.toast("已删除", true);
    return { deleted: [key] };
  }

  async function deleteTasks(ctx, keys) {
    const pid = need(ctx, "projectId");
    const list = (keys || []).filter(Boolean);
    if (!list.length) return { deleted: [] };
    if (!ui.confirm(`确认删除选中的 ${list.length} 个任务？该操作不可撤销。`)) return null;
    const data = await LMApi.deleteProjectTasksBatch(pid, list);
    ui.toast(`已删除 ${(data && data.deleted) || list.length} 个`, true);
    return data || { deleted: list };
  }

  async function retestTasks(ctx, keys) {
    const pid = need(ctx, "projectId");
    const list = (keys || []).filter(Boolean);
    if (!list.length) return { started: [] };
    if (!ui.confirm(`确认重新运行选中的 ${list.length} 个任务？`)) return null;
    const data = await LMApi.rerunSelectedTasks(pid, list);
    const started = (data && (data.started || data.tasks)) || [];
    ui.toast(`已提交 ${started.length || list.length} 个任务`, true);
    return data || { started: list };
  }

  function downloadTask(ctx) {
    const pid = need(ctx, "projectId");
    const key = need(ctx, "taskKey");
    global.location = LMApi.projectTaskDownloadUrl(pid, key);
  }

  function downloadTasks(ctx, keys) {
    const pid = need(ctx, "projectId");
    const list = (keys || []).filter(Boolean);
    if (!list.length) return;
    global.location = LMApi.projectTasksDownloadBatchUrl(pid, list);
  }

  // Deep link into the project task page's detail panel, preserving where the
  // user came from so "back" returns to the workspace instead of dumping them
  // on a project page they never chose. Uses an explicit ?from= parameter
  // rather than history.back(): the user may have arrived by pasted link, in
  // which case back() leaves the app entirely.
  function taskDetailUrl(ctx, from) {
    const pid = need(ctx, "projectId");
    const id = (ctx && (ctx.taskId || ctx.taskKey)) || "";
    let url = `/lanmatrix/projects/${encodeURIComponent(pid)}/tasks`
      + `?task=${encodeURIComponent(id)}`;
    if (from) url += `&from=${encodeURIComponent(from)}`;
    return url;
  }

  // `/lanmatrix/` only 302s to the workspace, so target it directly and save
  // the round trip.
  const WORKSPACE_URL = "/lanmatrix/home";

  function backUrl(defaultUrl) {
    const from = new URLSearchParams(global.location.search).get("from");
    if (from === "workspace") return WORKSPACE_URL;
    return defaultUrl || WORKSPACE_URL;
  }

  // True when the current page was reached from the personal workspace, so
  // pages can show a "back to workspace" affordance instead of guessing.
  function cameFromWorkspace() {
    return new URLSearchParams(global.location.search).get("from") === "workspace";
  }

  // --- review verbs --------------------------------------------------------
  // The asymmetry between these two is the whole point of the review feature:
  //
  //  * PASS can be approved in bulk. A regression sweep turns hundreds of cases
  //    green at once, and forcing one click each guarantees rubber-stamping --
  //    the paperwork of diligence without the diligence.
  //  * `Untestable` cannot, and demands a written note. Each one is an
  //    individual judgement that silently removes a case from the evidence base.
  async function approveReview(ctx, note) {
    const pid = need(ctx, "projectId");
    const uuid = need(ctx, "uuid");
    let text = note;
    if (ctx.needsNote && !String(text || "").trim()) {
      text = ui.prompt("审核『无法测试』的用例必须填写意见：", "");
      if (text === null) return null;
      if (!String(text).trim()) { ui.toast("必须填写审核意见", false); return null; }
    }
    const data = await LMApi.reviewItem(pid, uuid, "approve", text || "");
    ui.toast("已通过审核", true);
    return data;
  }

  async function rejectReview(ctx, note) {
    const pid = need(ctx, "projectId");
    const uuid = need(ctx, "uuid");
    let text = note;
    if (!String(text || "").trim()) {
      text = ui.prompt("请填写驳回理由：", "");
      if (text === null) return null;
      if (!String(text).trim()) { ui.toast("驳回必须填写理由", false); return null; }
    }
    const data = await LMApi.reviewItem(pid, uuid, "reject", text);
    ui.toast("已驳回", true);
    return data;
  }

  /* 項目作成=不要 sign-off. Same queue, same buttons as a verdict review, but a
     different endpoint -- and a reason is mandatory in BOTH directions, not
     just on rejection: approving permanently writes an Untestable verdict and
     shrinks the plan, so "why was this case never written?" has to stay
     answerable years later. */
  async function decideExemption(ctx, action, note) {
    const pid = need(ctx, "projectId");
    const uuid = need(ctx, "uuid");
    let text = note;
    if (!String(text || "").trim()) {
      text = ui.prompt(action === "approve"
        ? "确认无需作成的理由（将记为 Untestable）："
        : "请填写驳回理由：", "");
      if (text === null) return null;
      if (!String(text).trim()) { ui.toast("必须填写理由", false); return null; }
    }
    const data = await LMApi.decideExemption(pid, uuid, action, text);
    ui.toast(action === "approve" ? "已通过，判定记为 Untestable" : "已驳回", true);
    return data;
  }

  async function approveReviewsBulk(ctx, rows, note) {
    const pid = need(ctx, "projectId");
    const list = rows || [];
    if (!list.length) return null;
    // Refuse client-side too, so the reviewer learns *before* the round trip
    // that some of their selection needs individual attention.
    const eligible = list.filter((r) => r.bulk_approvable);
    const blocked = list.filter((r) => !r.bulk_approvable);
    if (!eligible.length) {
      ui.toast("选中的用例都需要逐条审核", false);
      return null;
    }
    const extra = blocked.length ? `（${blocked.length} 条需逐条审核，将跳过）` : "";
    if (!ui.confirm(`确认通过 ${eligible.length} 条审核？${extra}`)) return null;

    const data = await LMApi.reviewItemsBulk(
      pid, eligible.map((r) => r.uuid), "approve", note || "");
    const done = ((data && data.approved) || []).length;
    const skipped = ((data && data.skipped) || []).length + blocked.length;
    ui.toast(skipped ? `已通过 ${done} 条，跳过 ${skipped} 条`
                     : `已通过 ${done} 条`, true);
    return data;
  }

  async function assignReviewer(ctx, reviewerId) {
    const pid = need(ctx, "projectId");
    const uuid = need(ctx, "uuid");
    const data = await LMApi.assignItemReviewer(pid, uuid, reviewerId);
    ui.toast(reviewerId ? "已指派审核人" : "已清除审核人", true);
    return data;
  }

  global.LMTaskActions = {
    configure,
    // task
    canCancel, canRetest, canDownload,
    cancelTask, cancelTasks, deleteTask, deleteTasks, retestTasks,
    downloadTask, downloadTasks, taskDetailUrl, backUrl, cameFromWorkspace,
    // review
    approveReview, rejectReview, approveReviewsBulk, assignReviewer,
    decideExemption,
  };
})(window);
