/* Workspace review queue — the reviewer's inbox.
 *
 * Why it is here and not only inside a project
 * --------------------------------------------
 * An assigned review is work the reviewer did not choose and has no reason to
 * go looking for. If the only way to find it is to open each project in turn,
 * reviews sit unclaimed and the feature is decorative. So the queue lives on
 * the page people already open first.
 *
 * Full actions, not a read-only list
 * ----------------------------------
 * Every row carries 通过 / 驳回 / 打开. All three go through LMTaskActions, the
 * same module the project page uses, so the workspace and the project view can
 * never disagree about what a button does. Links out carry `?from=workspace`
 * so returning lands back here rather than on a project the user never picked.
 *
 * The PASS / Untestable asymmetry is enforced in the UI as well as the server:
 * `Untestable` rows are visually separated, excluded from 全选's bulk action and
 * demand a written opinion. Bulk-approving a claim that a case cannot be tested
 * would defeat the reason that claim is reviewed at all.
 */
(function (global) {
  "use strict";

  const $ = (id) => document.getElementById(id);
  const PAGE_SIZE = 20;

  let rows = [];
  let projects = [];
  let pager = null;
  // Selection is keyed by row index and deliberately survives paging: a
  // reviewer who ticks four rows on page 1, checks something on page 2 and
  // comes back must still have their four rows selected.
  const selected = new Set();

  function esc(s) {
    return String(s == null ? "" : s).replace(/[&<>"]/g, (c) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
  }

  function toast(msg, good) {
    if (global.LMUI && LMUI.toast) LMUI.toast(msg, good);
  }

  // Wire the shared action module to this page's feedback affordances once.
  if (global.LMTaskActions) {
    LMTaskActions.configure({
      toast,
      confirm: (m) => global.confirm(m),
      prompt: (m, d) => global.prompt(m, d || ""),
    });
  }

  function projName(pid, projects) {
    const p = (projects || []).find((x) => x.id === pid);
    return p ? (p.code || p.name || pid) : pid;
  }

  function verdictPill(r) {
    const v = String(r.review_verdict || r.result || "");
    const cls = /untestable/i.test(v) ? "warn" : "ok";
    return `<span class="pill ${cls}">${esc(v || "—")}</span>`;
  }

  function render(data) {
    rows = (data && data.reviews) || [];
    projects = (data && data.projects) || [];
    selected.clear();

    const card = $("lm-h-rv-card");
    const empty = $("lm-h-rv-empty");
    const toolbar = $("lm-h-rv-toolbar");
    const count = $("lm-h-rv-count");
    const kpi = document.querySelector('#lm-kpi [data-v="reviews"]');
    if (kpi) kpi.textContent = rows.length;
    if (count) count.textContent = rows.length ? `${rows.length} 条` : "";
    if (global.LMHome) LMHome.setTabCount("reviews", rows.length);

    if (pager) {
      pager.reset();
      pager.setTotal(rows.length);
    }

    if (!rows.length) {
      if (card) card.hidden = true;
      if (toolbar) toolbar.hidden = true;
      if (empty) empty.hidden = false;
      $("lm-h-rv-rows").innerHTML = "";
      syncBulk();
      return;
    }
    if (empty) empty.hidden = true;
    if (card) card.hidden = false;
    if (toolbar) toolbar.hidden = false;
    paint();
  }

  // Rows carry their index in the FULL list (`data-i`), not in the page, so an
  // action fired on page 3 still resolves to the right review.
  function paint() {
    const start = pager ? pager.offset() : 0;
    const page = pager ? pager.slice(rows) : rows;
    $("lm-h-rv-rows").innerHTML = page.map((r, n) => {
      const i = start + n;
      const href = `/lanmatrix/projects/${encodeURIComponent(r.project_id)}`
        + `?row=${encodeURIComponent(r.uuid)}&from=workspace`;
      // Only bulk-approvable rows get a checkbox: offering one that silently
      // does nothing on submit is worse than not offering it.
      const box = r.bulk_approvable
        ? `<input type="checkbox" class="lm-rv-box" data-i="${i}"`
          + `${selected.has(i) ? " checked" : ""}>`
        : `<span class="muted" title="需逐条审核">—</span>`;
      return `<tr class="${r.needs_note ? "needs-note" : ""}">
        <td>${box}</td>
        <td>${esc(projName(r.project_id, projects))}</td>
        <td><a href="${href}">${esc(r.case_id || r.uuid || "")}</a>
            <div class="muted">${esc(r.title || "")}</div></td>
        <td>${verdictPill(r)}</td>
        <td class="lm-rv-reason">${esc(r.description || "")}</td>
        <td>${esc(r.executor || "")}</td>
        <td class="muted">${esc(r.exec_date || "")}</td>
        <td>
          <div class="lm-rv-acts">
            <button class="btn primary small" data-act="approve" data-i="${i}">通过</button>
            <button class="btn small" data-act="reject" data-i="${i}">驳回</button>
            <a class="btn ghost small" href="${href}">打开</a>
          </div>
        </td>
      </tr>`;
    }).join("");
    syncBulk();
  }

  function syncBulk() {
    const btn = $("lm-h-rv-approve");
    const label = $("lm-h-rv-sel");
    if (btn) btn.disabled = selected.size === 0;
    if (label) label.textContent = selected.size ? `已选 ${selected.size} 条` : "";
    const all = $("lm-h-rv-all");
    const eligible = rows.filter((r) => r.bulk_approvable).length;
    if (all) all.checked = eligible > 0 && selected.size === eligible;
  }

  async function load() {
    if (!global.LMApi || !LMApi.meReviews) return;
    try {
      render(await LMApi.meReviews({ limit: 200 }));
    } catch (ex) {
      if (ex.status === 401) return;
      const host = $("lm-h-rv-rows");
      if (host) host.innerHTML =
        `<tr><td colspan="8" class="muted">加载失败：${esc(ex.message || "")}</td></tr>`;
    }
  }

  async function act(kind, i) {
    const r = rows[i];
    if (!r) return;
    const ctx = { projectId: r.project_id, uuid: r.uuid, needsNote: r.needs_note };
    try {
      const res = kind === "approve"
        ? await LMTaskActions.approveReview(ctx)
        : await LMTaskActions.rejectReview(ctx);
      if (res) await load();
    } catch (ex) {
      toast(ex.message || "操作失败", false);
    }
  }

  document.addEventListener("DOMContentLoaded", () => {
    const host = $("lm-h-rv-rows");
    if (!host) return;

    if (global.LMPager) {
      pager = LMPager.create({
        host: $("lm-h-rv-pager"), pageSize: PAGE_SIZE, onChange: paint,
      });
    }

    // One delegated listener rather than a handler per row: the table is
    // re-rendered on every refresh, and per-row listeners would leak.
    host.addEventListener("click", (e) => {
      const btn = e.target.closest("[data-act]");
      if (btn) { act(btn.dataset.act, Number(btn.dataset.i)); return; }
      const box = e.target.closest(".lm-rv-box");
      if (box) {
        const i = Number(box.dataset.i);
        if (box.checked) selected.add(i); else selected.delete(i);
        syncBulk();
      }
    });

    // 全选 covers the whole queue, not just the visible page: the reviewer's
    // intent when ticking it is "all of my pending reviews", and a control that
    // silently means "the 20 rows you happen to be looking at" would approve a
    // different set than the label promises.
    const all = $("lm-h-rv-all");
    if (all) all.addEventListener("change", () => {
      selected.clear();
      if (all.checked) {
        rows.forEach((r, i) => { if (r.bulk_approvable) selected.add(i); });
      }
      host.querySelectorAll(".lm-rv-box").forEach((b) => {
        b.checked = selected.has(Number(b.dataset.i));
      });
      syncBulk();
    });

    const bulk = $("lm-h-rv-approve");
    if (bulk) bulk.addEventListener("click", async () => {
      const picked = Array.from(selected).map((i) => rows[i]).filter(Boolean);
      if (!picked.length) return;

      // The workspace queue spans projects, but the bulk endpoint is
      // project-scoped (it must be: permissions are checked per project). So
      // group first and issue one call per project. Sending only the first
      // project's rows would silently approve part of the selection while
      // reporting success for all of it.
      const byProject = new Map();
      picked.forEach((r) => {
        if (!byProject.has(r.project_id)) byProject.set(r.project_id, []);
        byProject.get(r.project_id).push(r);
      });

      const eligible = picked.filter((r) => r.bulk_approvable);
      if (!eligible.length) { toast("选中的用例都需要逐条审核", false); return; }
      if (!global.confirm(`确认通过 ${eligible.length} 条审核？`)) return;

      let done = 0;
      let skipped = 0;
      const failed = [];
      for (const [pid, group] of byProject) {
        const ok = group.filter((r) => r.bulk_approvable);
        if (!ok.length) { skipped += group.length; continue; }
        try {
          const res = await LMApi.reviewItemsBulk(
            pid, ok.map((r) => r.uuid), "approve", "");
          done += ((res && res.approved) || []).length;
          skipped += ((res && res.skipped) || []).length;
        } catch (ex) {
          failed.push(`${pid}：${ex.message || "失败"}`);
        }
      }
      // Report the shortfall honestly rather than a blanket "已通过".
      const parts = [`已通过 ${done} 条`];
      if (skipped) parts.push(`跳过 ${skipped} 条`);
      if (failed.length) parts.push(`${failed.length} 个项目失败`);
      toast(parts.join("，"), !failed.length);
      await load();
    });

    // The KPI tile now switches to the queue's tab instead of scrolling to a
    // section further down the same page.
    const kpi = $("lm-h-kpi-review");
    if (kpi) kpi.addEventListener("click", () => {
      if (global.LMHome) LMHome.setView("reviews", true);
    });

    const refresh = $("lm-home-refresh");
    if (refresh) refresh.addEventListener("click", load);

    // Notifications link here with ?view=reviews; home.js raises that tab on
    // load, so this module only has to fill it.
    load();
  });

  global.LMWorkspaceReviews = { reload: load };
})(window);
