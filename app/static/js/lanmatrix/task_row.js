/* Shared task-row rendering — one definition of what a task row looks like.
 *
 * Why this module exists
 * ----------------------
 * The workspace (工作台 · 最近任务) and the project task list (任务/运行) show
 * the same rows and must offer the same verbs. Before this module the row
 * markup, the status vocabulary and the "which buttons apply" rules lived
 * inside project_tasks.js, bound to that page's `pid` closure, so the workspace
 * could only ever be a read-only list with a single 查看 link.
 *
 * Copying the markup across would have produced two renderers that drift apart
 * on the first change: a status that reads 运行中 on one page and `running` on
 * the other, or a 重新测试 button that appears on a queued task in one place and
 * not the other. LMTaskActions already centralised the *verbs*; this centralises
 * the *row*. Neither touches page state: every function takes a task object plus
 * an explicit options bag and returns a string.
 *
 * The workspace passes `projectCell` (its extra first column) and `viewHref`
 * (its 查看 navigates to the project page's detail panel, because the live log
 * and judge output live there). The project page passes neither and handles 查看
 * in place. Everything else is identical by construction.
 */
(function (global) {
  "use strict";

  function esc(s) {
    return String(s == null ? "" : s).replace(/[&<>"]/g, (c) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
  }

  const STATUS_ZH = global.LMPill ? LMPill.TASK_ZH : {};
  const pill = global.LMPill ? LMPill.html : ((c, t) => esc(t));

  // States a task can no longer leave on its own.
  const FINAL = ["passed", "failed", "cancelled"];
  // "In the queue" is only queued/running; anything else can be re-queued.
  const LIVE_STATUS = ["queued", "running"];

  const canCancel = (t) => !!t && !FINAL.includes(String(t.status || ""));
  const canRetest = (t) => !!t && !LIVE_STATUS.includes(String(t.status || ""));
  const canDownload = (t) => !!(t && t.has_result);

  /* Merge the execution ``status`` with the judge ``result`` (verdict) into one
     label. A finished-but-failing run carries status ``failed``; split a genuine
     test failure (verdict FAIL) from an execution/judge error (verdict ERROR),
     because the two need completely different responses from the user. */
  function mergedVerdict(t) {
    const st = String((t && t.status) || "").toLowerCase();
    if (st === "failed") {
      const v = String((t && t.result) || "").trim().toUpperCase();
      if (v.startsWith("ERROR")) return { cls: "error", label: "error" };
      return { cls: "failed", label: "failed" };
    }
    if (st === "passed" || st === "cancelled" || st === "running" || st === "queued") {
      return { cls: st, label: st };
    }
    return { cls: st || "notask", label: st || "—" };
  }

  function mergedBadge(t) {
    const m = mergedVerdict(t);
    return pill(m.cls, STATUS_ZH[m.label] || m.label, String((t && (t.result || t.status)) || ""));
  }

  /* ``finished_at`` (ISO UTC) as ``YY/MM/DD HH:MM:SS`` in the viewer's local
     time, e.g. ``26/07/20 11:18:15``. */
  function fmtTime(iso) {
    if (!iso) return "";
    const d = new Date(iso);
    if (isNaN(d.getTime())) return "";
    const p = (n) => String(n).padStart(2, "0");
    return `${p(d.getFullYear() % 100)}/${p(d.getMonth() + 1)}/${p(d.getDate())} `
      + `${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`;
  }

  const fmtFinished = (t) => fmtTime(t && t.finished_at);

  // --- review sign-off -----------------------------------------------------
  /* A verdict is a claim; the review is the decision on it. The two live in
     different tables (tasks vs. the matrix row) joined by test_id, so before
     this column an approval or a rejection was visible only inside the
     reviewer's own queue -- the executor saw their own claim and never learnt
     what happened to it.
     `t.review` is null when the project's policy asks for no sign-off on this
     verdict, which is a real state ("—"), not missing data. */
  const REVIEW_ZH = { pending: "待审核", approved: "已通过", rejected: "已驳回" };

  function reviewBadge(t) {
    const r = t && t.review;
    if (!r || !r.status) return `<span class="muted">—</span>`;
    const label = REVIEW_ZH[r.status] || r.status;
    // The tooltip carries what the cell cannot: who decided, and above all why
    // it was rejected. A rejection without its reason is not actionable.
    const bits = [];
    if (r.reviewer_name) bits.push(`审核人：${r.reviewer_name}`);
    if (r.verdict) bits.push(`判定：${r.verdict}`);
    if (r.note) bits.push(`意见：${r.note}`);
    if (r.rows > 1) bits.push(`该 test id 匹配 ${r.rows} 行，显示最严重状态`);
    return pill(r.status, label, bits.join(" / ") || label);
  }

  /* Deep link to the matrix row under review. `from=tasks` makes the matrix
     page offer a way back, the same contract the workspace deep link uses. */
  function reviewHref(t) {
    const r = t && t.review;
    if (!r || !r.row_uuid || t.project_id == null) return "";
    return `/lanmatrix/projects/${encodeURIComponent(t.project_id)}`
      + `?row=${encodeURIComponent(r.row_uuid)}&from=tasks`;
  }

  function reviewCell(t) {
    const href = reviewHref(t);
    const badge = reviewBadge(t);
    return href ? `<a href="${esc(href)}" class="lm-review-link">${badge}</a>` : badge;
  }

  /* Sort key for the review column: rank first so 待审核 groups together, and
     never the raw object (which would stringify to [object Object]). */
  const REVIEW_RANK = { pending: 3, rejected: 2, approved: 1 };
  const reviewRank = (t) =>
    REVIEW_RANK[(t && t.review && t.review.status) || ""] || 0;

  // --- sorting -------------------------------------------------------------
  // Shared so both lists sort a column the same way; ``progress`` is numeric and
  // sorting it as text would put 100 before 20.
  function cmp(a, b, key) {
    if (key === "progress") return (+a.progress || 0) - (+b.progress || 0);
    if (key === "review") return reviewRank(a) - reviewRank(b);
    const x = String(a[key] == null ? "" : a[key]).toLowerCase();
    const y = String(b[key] == null ? "" : b[key]).toLowerCase();
    return x < y ? -1 : x > y ? 1 : 0;
  }

  // Repaint guard: rows whose signature is unchanged are left alone, so a poll
  // does not blow away the user's text selection every few seconds.
  function signature(t) {
    const r = t.review || {};
    return [t.status, t.progress || 0, t.has_result ? 1 : 0,
            t.result || "", fmtFinished(t),
            // A decision that lands while the list is open must repaint the
            // row, otherwise the poll keeps showing 待审核 on a rejected run.
            r.status || "", r.note || "", r.reviewer_name || ""].join("|");
  }

  // --- icons ---------------------------------------------------------------
  // Compact icon buttons: the action column is a narrow strip revealed on row
  // hover, so the data columns keep the width text buttons used to consume.
  const _ic = (p) => `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" `
    + `stroke-width="2" stroke-linecap="round" stroke-linejoin="round">${p}</svg>`;
  const ICO = {
    view: _ic('<path d="M2 12s3.5-7 10-7 10 7 10 7-3.5 7-10 7-10-7-10-7z"/><circle cx="12" cy="12" r="3"/>'),
    steps: _ic('<path d="M8 6h13M8 12h13M8 18h13M3 6h.01M3 12h.01M3 18h.01"/>'),
    download: _ic('<path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4M7 10l5 5 5-5M12 15V3"/>'),
    cancel: _ic('<rect x="6" y="6" width="12" height="12" rx="2"/>'),
    retest: _ic('<path d="M23 4v6h-6M1 20v-6h6"/><path d="M3.51 9a9 9 0 0 1 14.85-3.36L23 10M1 14l4.64 4.36A9 9 0 0 0 20.49 15"/>'),
    del: _ic('<path d="M3 6h18M8 6V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2m3 0v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6"/>'),
  };

  /* Render one <tr>.
   *
   * opts:
   *   projectId    number  — owner project, needed for the download URL and by
   *                          the workspace, whose rows span several projects.
   *   canDelete    bool    — per project: 删除 is project-admin only.
   *   selected     bool    — restores the tick after a repaint.
   *   projectCell  string  — optional extra leading cell (workspace only).
   *   viewHref     string  — when given, 查看 is a link instead of a button.
   */
  function rowHtml(t, opts) {
    const o = opts || {};
    const pid = o.projectId;
    const key = esc(t.task_id || "");
    const p = t.progress || 0;
    const data = `data-k="${key}"` + (pid != null ? ` data-p="${esc(pid)}"` : "");

    const check = `<td class="lm-col-check">`
      + `<input type="checkbox" class="lm-task-sel" ${data}${o.selected ? " checked" : ""}></td>`;

    const view = o.viewHref
      ? `<a class="btn small btn-icon lm-task-view" href="${esc(o.viewHref)}" ${data} title="查看">${ICO.view}</a>`
      : `<button class="btn small btn-icon lm-task-view" type="button" ${data} title="查看">${ICO.view}</button>`;
    const steps = t.test_id
      ? `<button class="btn small btn-icon lm-task-steps" type="button" ${data} data-tid="${esc(t.test_id)}" title="手顺">${ICO.steps}</button>`
      : "";
    const dl = (canDownload(t) && pid != null)
      ? `<a class="btn small btn-icon" href="${LMApi.projectTaskDownloadUrl(pid, t.task_id)}" title="下载结果">${ICO.download}</a>`
      : "";
    const cancel = canCancel(t)
      ? `<button class="btn small btn-icon lm-task-cancel" type="button" ${data} title="取消运行">${ICO.cancel}</button>`
      : "";
    const retest = canRetest(t)
      ? `<button class="btn small btn-icon lm-task-retest" type="button" ${data} title="重新测试">${ICO.retest}</button>`
      : "";
    const del = o.canDelete
      ? `<button class="btn small btn-icon danger lm-task-del" type="button" ${data} title="删除">${ICO.del}</button>`
      : "";

    /* The test id opens the detail panel, and the task key no longer has a
       column of its own.
       The task key is a synthetic queue identifier (T000123): nobody reads a
       list by it, they read it by test id, and giving it a column cost width on
       every row to show a number used only for support. It is still the handle
       every action posts (data-k), still in the row's tooltip, and still shown
       in full in the detail panel -- it stopped being a column, not data. */
    const tid = esc(t.test_id || "");
    const tidTip = `${t.test_id || ""}${t.test_id ? "  ·  " : ""}任务 ${t.task_id || ""}`;
    const tidCell = tid
      ? (o.viewHref
        ? `<a href="${esc(o.viewHref)}" title="${esc(tidTip)}">${tid}</a>`
        : `<a href="#" class="lm-task-open" ${data} title="${esc(tidTip)}">${tid}</a>`)
      // A task with no test id (legacy bundle upload) still needs an opener, so
      // it falls back to showing the key rather than rendering a dead cell.
      : (o.viewHref
        ? `<a href="${esc(o.viewHref)}"><code>${key}</code></a>`
        : `<a href="#" class="lm-task-open" ${data}><code>${key}</code></a>`);

    return `<tr ${data} title="任务 ${esc(t.task_id || "")}">
      ${check}
      ${o.projectCell || ""}
      <td class="lm-cell-testid">${tidCell}</td>
      <td class="lm-cell-ell" title="${esc(t.sil_name || "")}">${esc(t.sil_name || "")}</td>
      <td class="lm-cell-ell" title="${esc(t.submitter || "")}">${esc(t.submitter || "")}</td>
      <td class="lm-cell-status">${mergedBadge(t)}</td>
      <td class="lm-cell-review">${reviewCell(t)}</td>
      <td class="lm-cell-progress">
        <div style="display:flex;align-items:center;gap:8px">
          <div class="prog" style="width:70px;flex:0 0 auto"><i style="width:${p}%"></i></div>
          <span class="muted" style="font-size:12px">${p}%</span>
        </div>
      </td>
      <td class="lm-cell-time"><code>${esc(fmtFinished(t))}</code></td>
      <td class="lm-row-actions"><span class="row-acts" style="display:flex;gap:4px;justify-content:flex-end">${view} ${steps} ${cancel} ${retest} ${dl} ${del}</span></td>
    </tr>`;
  }

  global.LMTaskRow = {
    esc, STATUS_ZH, FINAL, LIVE_STATUS,
    canCancel, canRetest, canDownload,
    mergedVerdict, mergedBadge, fmtTime, fmtFinished,
    REVIEW_ZH, reviewBadge, reviewHref, reviewCell, reviewRank,
    cmp, signature, rowHtml, ICO,
  };
})(window);
