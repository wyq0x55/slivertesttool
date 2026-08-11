/* Workspace 不要 (項目作成) sign-off queue.
 *
 * Why this is a queue and not a column
 * ------------------------------------
 * Typing 不要 into 項目作成 is a claim that a case need not exist. Until this
 * screen there was nowhere to answer that claim: the cell was free text, so a
 * case could leave the plan without anyone agreeing, or -- what actually
 * happened -- never leave it and sit in 未実施 forever. The server already
 * refuses to drop an unapproved claim from the denominator; this page is where
 * the approval is given.
 *
 * Why it lives next to the verdict queue but is not merged into it
 * ---------------------------------------------------------------
 * Both are "work assigned to me", so they belong on the page people open
 * first. But they ask different questions -- "is this result correct" versus
 * "may this case be dropped" -- and a reviewer approving the first has not
 * agreed to the second. One list whose rows meant either thing would invite
 * exactly that mistake, so they are separate tabs sharing a layout.
 *
 * A reason is required, in both directions
 * ----------------------------------------
 * An approval shrinks the test plan and a rejection tells someone to go do
 * work. Neither is a fact that can stand without an author, and the server
 * rejects a blank note, so the UI must never send one: the prompt loops rather
 * than firing a request that is certain to fail.
 */
(function (global) {
  "use strict";

  const $ = (id) => document.getElementById(id);
  const PAGE_SIZE = 20;

  let rows = [];
  let projects = [];
  let pager = null;
  /* `pending` is the inbox. `decided` is the answer to "what did I already
     sign off", which without it is unreachable the moment it is answered --
     the same gap the verdict queue had before its 我已处理 scope. */
  let scope = "pending";
  let mine = true;

  const SCOPE_TEXT = {
    pending: {
      title: "待审批的『不要』",
      empty: ["没有待审批的『不要』",
              "項目作成 填「不要」的用例会出现在这里，等你决定是否可以不做。"],
    },
    decided: {
      title: "已处理的『不要』",
      empty: ["还没有处理过的『不要』", "你通过或驳回的免测申请会留在这里。"],
    },
  };

  const actionable = () => scope === "pending";
  // Selection is by index into the FULL list and survives paging, so ticking
  // rows on page 1, glancing at page 2 and coming back keeps the selection.
  const selected = new Set();

  function esc(s) {
    return String(s == null ? "" : s).replace(/[&<>"]/g, (c) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
  }

  function toast(msg, good) {
    if (global.LMUI && LMUI.toast) LMUI.toast(msg, good);
  }

  function projName(pid) {
    const p = (projects || []).find((x) => x.id === pid);
    return p ? (p.code || p.name || pid) : pid;
  }

  /* The note is mandatory server-side in both directions, so asking once and
     giving up would just surface a 400. Loop until there is text or the user
     actively cancels. */
  function askNote(verb, n) {
    const what = n > 1 ? `${n} 条` : "该用例";
    for (;;) {
      const note = global.prompt(`${verb}${what}的『不要』申请，请填写理由：`, "");
      if (note === null) return null;          // cancelled -- do nothing
      if (note.trim()) return note.trim();
      toast("必须填写理由", false);
    }
  }

  function statusCell(r) {
    const zh = { pending: "待审批", approved: "已通过", rejected: "已驳回" };
    const st = String(r.status || "");
    const badge = global.LMPill
      ? LMPill.html(st, zh[st] || st, r.note || "")
      : `<span class="pill">${esc(zh[st] || st)}</span>`;
    const who = r.reviewer_name
      ? ` <span class="muted">${esc(r.reviewer_name)}</span>` : "";
    const note = r.note ? `<div class="lm-rv-reason">${esc(r.note)}</div>` : "";
    return badge + who + note;
  }

  function applyScopeText() {
    const text = SCOPE_TEXT[scope] || SCOPE_TEXT.pending;
    const title = $("lm-h-ex-title");
    if (title) title.textContent = text.title;
    const empty = $("lm-h-ex-empty");
    if (empty) {
      const h = empty.querySelector("h3");
      const p = empty.querySelector("p");
      if (h) h.textContent = text.empty[0];
      if (p) p.textContent = text.empty[1];
    }
    document.querySelectorAll(".lm-ex-scope").forEach((btn) => {
      const on = btn.dataset.status === scope;
      btn.classList.toggle("is-on", on);
      btn.classList.toggle("ghost", !on);
      btn.setAttribute("aria-selected", on ? "true" : "false");
    });
    const box = $("lm-h-ex-mine");
    if (box) box.checked = mine;
  }

  /* The badge and the KPI tile say 待批, so they count what is waiting on THIS
     user -- always, including while 只看指派给我的 is off. Switching that
     checkbox widens the list the user is reading; it does not hand them the
     rest of the team's backlog, and a tile that jumped from 3 to 200 because
     someone unticked a filter would be reporting a workload nobody has.
     `data.counts` (project-wide) is the fallback only for an older server
     response that predates queue_counts. */
  function pendingCount(data) {
    const own = (data && data.queue_counts) || null;
    if (own) return Number(own.pending || 0);
    const counts = (data && data.counts) || {};
    return Object.keys(counts).reduce(
      (n, pid) => n + Number((counts[pid] || {}).pending || 0), 0);
  }

  function render(data) {
    rows = (data && data.exemptions) || [];
    projects = (data && data.projects) || [];
    selected.clear();
    applyScopeText();

    const pending = pendingCount(data);
    const badge = $("lm-h-ex-n-pending");
    if (badge) {
      badge.textContent = pending ? String(pending) : "";
      badge.hidden = !pending;
    }
    // The tab count and the KPI tile are OUTSTANDING work regardless of which
    // scope is on screen: a tab reading 0 while claims wait, merely because
    // the user opened 已处理, would be a lie.
    if (global.LMHome) LMHome.setTabCount("exemptions", pending);
    const kpi = document.querySelector('#lm-kpi [data-v="exemptions"]');
    if (kpi) kpi.textContent = pending;

    const count = $("lm-h-ex-count");
    if (count) count.textContent = rows.length ? `${rows.length} 条` : "";

    if (pager) { pager.reset(); pager.setTotal(rows.length); }

    const card = $("lm-h-ex-card");
    const empty = $("lm-h-ex-empty");
    const toolbar = $("lm-h-ex-toolbar");
    if (!rows.length) {
      if (card) card.hidden = true;
      if (toolbar) toolbar.hidden = true;
      if (empty) empty.hidden = false;
      const host = $("lm-h-ex-rows");
      if (host) host.innerHTML = "";
      syncBulk();
      return;
    }
    if (empty) empty.hidden = true;
    if (card) card.hidden = false;
    if (toolbar) toolbar.hidden = !actionable();
    paint();
  }

  // `data-i` is the index in the full list, not the page, so an action fired
  // on page 3 still resolves to the row the user clicked.
  function paint() {
    const start = pager ? pager.offset() : 0;
    const page = pager ? pager.slice(rows) : rows;
    const host = $("lm-h-ex-rows");
    if (!host) return;
    host.innerHTML = page.map((r, n) => {
      const i = start + n;
      const href = `/lanmatrix/projects/${encodeURIComponent(r.project_id)}`
        + `?row=${encodeURIComponent(r.uuid)}&from=workspace`;
      const box = actionable()
        ? `<input type="checkbox" class="lm-ex-box" data-i="${i}"`
          + `${selected.has(i) ? " checked" : ""}>`
        : `<span class="muted">—</span>`;
      // A decided claim gets 打开 only: 通过 / 驳回 on it would be refused by
      // the server, and a button that cannot work is worse than no button.
      const acts = actionable()
        ? `<button class="btn primary small" data-act="approve" data-i="${i}">通过</button>
           <button class="btn small" data-act="reject" data-i="${i}">驳回</button>
           <a class="btn ghost small" href="${href}">打开</a>`
        : `<a class="btn ghost small" href="${href}">打开</a>`;
      const label = r.test_id || r.case_id || r.uuid || "";
      const cat = r.category
        ? `<span class="pill">${esc(r.category)}</span>` : "";
      return `<tr>
        <td>${box}</td>
        <td>${esc(projName(r.project_id))}</td>
        <td><a href="${href}">${esc(label)}</a>
            <div class="muted">${esc(r.title || "")}</div></td>
        <td>${cat}</td>
        <td>${esc(r.item_created || "")}</td>
        <td class="lm-rv-reason">${statusCell(r)}</td>
        <td class="muted">${esc(r.requester_name || "")}</td>
        <td><div class="lm-rv-acts">${acts}</div></td>
      </tr>`;
    }).join("");
    syncBulk();
  }

  function syncBulk() {
    const label = $("lm-h-ex-sel");
    if (label) label.textContent = selected.size ? `已选 ${selected.size} 条` : "";
    ["lm-h-ex-approve", "lm-h-ex-reject"].forEach((id) => {
      const btn = $(id);
      if (btn) btn.disabled = selected.size === 0;
    });
    const all = $("lm-h-ex-all");
    if (all) all.checked = rows.length > 0 && selected.size === rows.length;
    const toolbar = $("lm-h-ex-toolbar");
    if (toolbar && !actionable()) toolbar.hidden = true;
  }

  async function load() {
    if (!global.LMApi || !LMApi.meExemptions) return;
    const query = { limit: 200, status: scope, mine: mine ? 1 : 0 };
    // Assignment notifications link here with ?project_id=, so the reviewer
    // lands on the project the notice was about instead of the whole backlog.
    const pid = global.LMUrl ? LMUrl.get("project_id", "") : "";
    if (pid) query.project_id = pid;
    try {
      render(await LMApi.meExemptions(query));
    } catch (ex) {
      if (ex.status === 401) return;
      const host = $("lm-h-ex-rows");
      if (host) host.innerHTML =
        `<tr><td colspan="8" class="muted">加载失败：${esc(ex.message || "")}</td></tr>`;
    }
  }

  async function act(kind, i) {
    const r = rows[i];
    if (!r) return;
    const note = askNote(kind === "approve" ? "通过" : "驳回", 1);
    if (note === null) return;
    try {
      await LMApi.decideExemption(r.project_id, r.uuid, kind, note);
      toast(kind === "approve" ? "已通过" : "已驳回", true);
      await load();
    } catch (ex) {
      toast(ex.message || "操作失败", false);
    }
  }

  /* The queue spans projects but the bulk endpoint is project-scoped -- it has
     to be, because permission is checked per project. So group and issue one
     call each. Posting only the first group while reporting success for the
     whole selection would quietly leave claims undecided. */
  async function bulk(kind) {
    const picked = Array.from(selected).map((i) => rows[i]).filter(Boolean);
    if (!picked.length) return;
    const verb = kind === "approve" ? "通过" : "驳回";
    if (!global.confirm(`确认${verb} ${picked.length} 条『不要』申请？`)) return;
    const note = askNote(verb, picked.length);
    if (note === null) return;

    const byProject = new Map();
    picked.forEach((r) => {
      if (!byProject.has(r.project_id)) byProject.set(r.project_id, []);
      byProject.get(r.project_id).push(r);
    });

    let done = 0;
    let skipped = 0;
    const failed = [];
    for (const [pid, group] of byProject) {
      try {
        const res = await LMApi.decideExemptionsBulk(
          pid, group.map((r) => r.uuid), kind, note);
        const key = kind === "approve" ? "approved" : "rejected";
        done += ((res && res[key]) || []).length;
        skipped += ((res && res.skipped) || []).length;
      } catch (ex) {
        failed.push(`${pid}：${ex.message || "失败"}`);
      }
    }
    // Report the shortfall honestly instead of a blanket success message.
    const parts = [`已${verb} ${done} 条`];
    if (skipped) parts.push(`跳过 ${skipped} 条`);
    if (failed.length) parts.push(`${failed.length} 个项目失败`);
    toast(parts.join("，"), !failed.length);
    await load();
  }

  document.addEventListener("DOMContentLoaded", () => {
    const host = $("lm-h-ex-rows");
    if (!host) return;

    if (global.LMPager) {
      pager = LMPager.create({
        host: $("lm-h-ex-pager"), pageSize: PAGE_SIZE, onChange: paint,
      });
    }

    // One delegated listener: the table is rebuilt on every refresh, so
    // per-row handlers would leak.
    host.addEventListener("click", (e) => {
      const btn = e.target.closest("[data-act]");
      if (btn) { act(btn.dataset.act, Number(btn.dataset.i)); return; }
      const box = e.target.closest(".lm-ex-box");
      if (box) {
        const i = Number(box.dataset.i);
        if (box.checked) selected.add(i); else selected.delete(i);
        syncBulk();
      }
    });

    // 全选 means the whole queue, not the visible page: a control that
    // silently meant "these 20 rows" would decide a different set than the
    // label promises.
    const all = $("lm-h-ex-all");
    if (all) all.addEventListener("change", () => {
      selected.clear();
      if (all.checked) rows.forEach((_, i) => selected.add(i));
      host.querySelectorAll(".lm-ex-box").forEach((b) => {
        b.checked = selected.has(Number(b.dataset.i));
      });
      syncBulk();
    });

    const approve = $("lm-h-ex-approve");
    if (approve) approve.addEventListener("click", () => bulk("approve"));
    const reject = $("lm-h-ex-reject");
    if (reject) reject.addEventListener("click", () => bulk("reject"));

    document.querySelectorAll(".lm-ex-scope").forEach((btn) => {
      btn.addEventListener("click", () => {
        if (btn.dataset.status === scope) return;
        scope = btn.dataset.status;
        selected.clear();
        applyScopeText();
        load();
      });
    });

    const mineBox = $("lm-h-ex-mine");
    if (mineBox) mineBox.addEventListener("change", () => {
      mine = mineBox.checked;
      selected.clear();
      load();
    });

    const kpi = $("lm-h-kpi-exempt");
    if (kpi) kpi.addEventListener("click", () => {
      if (global.LMHome) LMHome.setView("exemptions", true);
    });

    const refresh = $("lm-home-refresh");
    if (refresh) refresh.addEventListener("click", load);

    applyScopeText();
    load();
  });

  global.LMWorkspaceExemptions = {
    reload: load,
    setScope(status) {
      scope = status === "decided" ? "decided" : "pending";
      selected.clear();
      applyScopeText();
      return load();
    },
  };
})(window);
