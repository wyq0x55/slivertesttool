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
  /* Which side of the review, and which states.
     `reviewer/pending` is the inbox. `requester/rejected` is the answer to
     "where do I see what got rejected" -- a decided row leaves the reviewer's
     queue by definition, so without this scope a rejection was announced (at
     best) and then unreachable. `reviewer/decided` is the reviewer's own audit
     trail of what they signed off. */
  let scope = { role: "reviewer", status: "pending" };

  const SCOPE_TEXT = {
    "reviewer/pending": {
      title: "待我审核",
      empty: ["没有待你审核的用例", "被指派给你的审核会出现在这里。"],
    },
    "requester/rejected": {
      title: "我被驳回",
      empty: ["没有被驳回的用例", "你提交的判定被驳回时会出现在这里，附带驳回理由。"],
    },
    "reviewer/decided": {
      title: "我已处理",
      empty: ["还没有处理过的审核", "你通过或驳回的用例会留在这里。"],
    },
  };

  const scopeKey = () => `${scope.role}/${scope.status}`;
  // Actions only make sense on rows that are still open; a decided row is a
  // record, and offering 通过 on it would post a request the server refuses.
  const isPending = (r) => String((r && r.review_status) || "") === "pending";
  const actionable = () => scope.status === "pending";
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

  function isExemption(r) {
    return (r && r.kind) === "exemption";
  }

  function verdictPill(r) {
    const v = String(r.review_verdict || r.result || "");
    const cls = /untestable/i.test(v) ? "warn" : "ok";
    // Both kinds show the verdict at stake -- an exemption reports Untestable
    // because that is literally what approving it writes onto the row. The tag
    // says where the claim came from, so a reviewer can still tell a runner's
    // result apart from a hand-typed 不要 without opening the case.
    const tag = isExemption(r)
      ? ` <span class="muted">項目作成=不要</span>` : "";
    return `<span class="pill ${cls}">${esc(v || "—")}</span>${tag}`;
  }

  /* The review's own state, as a pill. In the pending scope every row is
     `pending`, so the column shows the author's 説明 instead -- the substance
     of what is being reviewed. In the decided scopes the state and, above all,
     the rejection reason are the whole point. */
  function statusCell(r) {
    if (actionable()) return esc(r.description || "");
    const zh = { pending: "待审核", approved: "已通过", rejected: "已驳回" };
    const st = String(r.review_status || "");
    const badge = global.LMPill
      ? LMPill.html(st, zh[st] || st, r.review_note || "")
      : `<span class="pill">${esc(zh[st] || st)}</span>`;
    const who = r.reviewer_name ? ` <span class="muted">${esc(r.reviewer_name)}</span>` : "";
    const note = r.review_note
      ? `<div class="lm-rv-reason">${esc(r.review_note)}</div>`
      : (r.description ? `<div class="muted">${esc(r.description)}</div>` : "");
    return badge + who + note;
  }

  function applyScopeText() {
    const text = SCOPE_TEXT[scopeKey()] || SCOPE_TEXT["reviewer/pending"];
    const title = $("lm-h-rv-title");
    if (title) title.textContent = text.title;
    const empty = $("lm-h-rv-empty");
    if (empty) {
      const h = empty.querySelector("h3");
      const p = empty.querySelector("p");
      if (h) h.textContent = text.empty[0];
      if (p) p.textContent = text.empty[1];
    }
    document.querySelectorAll(".lm-rv-scope").forEach((btn) => {
      const on = btn.dataset.role === scope.role
        && btn.dataset.status === scope.status;
      btn.classList.toggle("is-on", on);
      btn.classList.toggle("ghost", !on);
      btn.setAttribute("aria-selected", on ? "true" : "false");
    });
  }

  function renderCounts(counts) {
    const map = { pending: "lm-h-rv-n-pending", rejected: "lm-h-rv-n-rejected",
                  decided: "lm-h-rv-n-decided" };
    Object.keys(map).forEach((k) => {
      const el = $(map[k]);
      if (!el) return;
      const n = Number((counts || {})[k] || 0);
      el.textContent = n ? String(n) : "";
      el.hidden = !n;
    });
  }

  function render(data) {
    rows = (data && data.reviews) || [];
    projects = (data && data.projects) || [];
    selected.clear();
    renderCounts(data && data.queue_counts);
    applyScopeText();

    const card = $("lm-h-rv-card");
    const empty = $("lm-h-rv-empty");
    const toolbar = $("lm-h-rv-toolbar");
    const count = $("lm-h-rv-count");
    // The KPI tile and the tab badge count OUTSTANDING work, always -- never
    // the scope the user happens to be reading. A tab that reads "审核 0" while
    // eight reviews wait, just because someone opened 我已处理, is a lie.
    const pending = Number(((data && data.queue_counts) || {}).pending
      || (actionable() ? rows.length : 0));
    const kpi = document.querySelector('#lm-kpi [data-v="reviews"]');
    if (kpi) kpi.textContent = pending;
    if (count) count.textContent = rows.length ? `${rows.length} 条` : "";
    if (global.LMHome) LMHome.setTabCount("reviews", pending);

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
      const box = (actionable() && r.bulk_approvable)
        ? `<input type="checkbox" class="lm-rv-box" data-i="${i}"`
          + `${selected.has(i) ? " checked" : ""}>`
        : `<span class="muted" title="${actionable() ? "需逐条审核" : "已完成审核"}">—</span>`;
      // A decided row gets 打开 only: 通过 / 驳回 on it would be refused by the
      // server, and a button that cannot work is worse than no button.
      const acts = (actionable() && isPending(r))
        ? `<button class="btn primary small" data-act="approve" data-i="${i}">通过</button>
           <button class="btn small" data-act="reject" data-i="${i}">驳回</button>
           <a class="btn ghost small" href="${href}">打开</a>`
        : `<a class="btn ghost small" href="${href}">打开</a>`;
      // The list is read by test id, like every other list in the product; the
      // row uuid is only the fallback for a case that has none.
      const label = r.test_id || r.case_id || r.uuid || "";
      return `<tr class="${r.needs_note ? "needs-note" : ""}">
        <td>${box}</td>
        <td>${esc(projName(r.project_id, projects))}</td>
        <td><a href="${href}">${esc(label)}</a>
            <div class="muted">${esc(r.title || "")}</div></td>
        <td>${verdictPill(r)}</td>
        <td class="lm-rv-reason">${statusCell(r)}</td>
        <td>${esc(r.executor || "")}</td>
        <td class="muted">${esc(r.exec_date || "")}</td>
        <td>
          <div class="lm-rv-acts">${acts}</div>
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
    // The bulk bar belongs to the pending scope only.
    const toolbar = $("lm-h-rv-toolbar");
    if (toolbar && !actionable()) toolbar.hidden = true;
  }

  async function load() {
    if (!global.LMApi || !LMApi.meReviews) return;
    try {
      render(await LMApi.meReviews({
        limit: 200, role: scope.role, status: scope.status,
      }));
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
      // Same two buttons for both kinds of sign-off, different endpoints. The
      // reviewer is answering one question either way; which table the answer
      // lands in is our problem, not theirs.
      const res = isExemption(r)
        ? await LMTaskActions.decideExemption(ctx, kind)
        : (kind === "approve"
          ? await LMTaskActions.approveReview(ctx)
          : await LMTaskActions.rejectReview(ctx));
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

    document.querySelectorAll(".lm-rv-scope").forEach((btn) => {
      btn.addEventListener("click", () => {
        const next = { role: btn.dataset.role, status: btn.dataset.status };
        if (next.role === scope.role && next.status === scope.status) return;
        scope = next;
        selected.clear();
        applyScopeText();
        load();
      });
    });

    const refresh = $("lm-home-refresh");
    if (refresh) refresh.addEventListener("click", load);

    applyScopeText();

    // Notifications link here with ?view=reviews; home.js raises that tab on
    // load, so this module only has to fill it.
    load();
  });

  global.LMWorkspaceReviews = {
    reload: load,
    // Lets a notification deep-link land on the right scope (e.g. a rejection
    // notice pointing at 我被驳回) instead of the default inbox.
    setScope(role, status) {
      scope = { role: role || "reviewer", status: status || "pending" };
      selected.clear();
      applyScopeText();
      return load();
    },
  };
})(window);
