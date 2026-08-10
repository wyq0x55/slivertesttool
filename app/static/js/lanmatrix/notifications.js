/* Topbar notification bell.
 *
 * Delivers the two events that happen while the user is looking somewhere else:
 * "the run you submitted finished" and "you have been assigned a review".
 * Without the second one an assigned review is only ever found by accident,
 * which makes the whole review feature unreliable.
 *
 * Design notes
 * ------------
 * * Polling, not WebSocket. On a LAN tool a 30s poll of a single COUNT is
 *   cheaper to run and to reason about than another persistent connection, and
 *   it degrades to "slightly stale" rather than "silently disconnected".
 * * The badge poll hits a dedicated endpoint that returns one integer; the
 *   dropdown payload is only fetched when the user actually opens it.
 * * Polling stops while the tab is hidden. A background tab left open all night
 *   would otherwise issue ~1000 pointless requests per user.
 * * No-ops when the bell is absent (login page), so it can load everywhere.
 */
(function (global) {
  "use strict";

  const $ = (id) => document.getElementById(id);
  const POLL_MS = (global.LM && LM.notifyPollSeconds ? LM.notifyPollSeconds : 30) * 1000;

  let timer = null;
  let open = false;
  let lastUnread = -1;
  // Which tab is showing. "unread" is work to do; "history" is the receipt.
  let scope = "unread";

  function esc(s) {
    return String(s == null ? "" : s).replace(/[&<>"]/g, (c) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
  }

  // "刚刚 / 5 分钟前 / 3 小时前 / 2 天前" — for a notification list the age is
  // what matters, and an absolute timestamp forces the reader to do the
  // subtraction themselves.
  function ago(iso) {
    if (!iso) return "";
    const t = new Date(iso.endsWith("Z") ? iso : iso + "Z").getTime();
    if (isNaN(t)) return "";
    const s = Math.max(0, (Date.now() - t) / 1000);
    if (s < 60) return "刚刚";
    if (s < 3600) return `${Math.floor(s / 60)} 分钟前`;
    if (s < 86400) return `${Math.floor(s / 3600)} 小时前`;
    return `${Math.floor(s / 86400)} 天前`;
  }

  const ICONS = {
    "task.finished": "▸",
    "review.assigned": "◆",
    "review.approved": "✓",
    "review.rejected": "✕",
  };

  function setBadge(n) {
    const dot = $("lm-notif-dot");
    if (!dot) return;
    lastUnread = n;
    dot.hidden = !n;
    dot.textContent = n > 99 ? "99+" : String(n || "");
    const btn = $("lm-notif-btn");
    if (btn) btn.setAttribute("aria-label", n ? `通知（${n} 条未读）` : "通知");
  }

  // Keep both tab counters honest from one payload. Rendering only the tab the
  // user is on would leave the other label stale until they clicked it.
  function setCounts(d) {
    const map = { unread: (d && d.unread) || 0, history: (d && d.history) || 0 };
    document.querySelectorAll("#lm-notif-tabs [data-count]").forEach((el) => {
      const n = map[el.dataset.count] || 0;
      el.textContent = n ? `(${n > 99 ? "99+" : n})` : "";
    });
    setBadge(map.unread);
  }

  function setScope(next) {
    scope = next;
    document.querySelectorAll("#lm-notif-tabs .lm-notif-tab").forEach((b) => {
      const on = b.dataset.scope === scope;
      b.classList.toggle("is-active", on);
      b.setAttribute("aria-selected", on ? "true" : "false");
    });
    const foot = $("lm-notif-foot");
    if (foot) foot.hidden = scope !== "history";
    loadList();
  }

  function render(items) {
    const host = $("lm-notif-list");
    const empty = $("lm-notif-empty");
    if (!host) return;
    if (!items || !items.length) {
      host.innerHTML = "";
      if (empty) {
        empty.hidden = false;
        empty.textContent = scope === "history" ? "暂无历史通知" : "暂无未读通知";
      }
      return;
    }
    if (empty) empty.hidden = true;
    host.innerHTML = items.map((n) => {
      const cls = "lm-notif-item" + (n.is_read ? "" : " unread");
      // Server-side collapsing is off by default (LM_NOTIFY_GROUP_SECONDS = 0),
      // so every event now gets its own row and its own link -- a "×2" row
      // could only ever open one of the two things it announced. The badge is
      // kept for deployments that re-enable the window, where it must say how
      // many events the row stands for rather than hide them silently.
      const badge = (n.count || 1) > 1
        ? `<span class="lm-notif-count" title="合并了 ${n.count} 条同类通知">×${n.count}</span>`
        : "";
      const body = n.body
        ? `<div class="lm-notif-body">${esc(n.body)}</div>` : "";
      const inner = `
        <span class="lm-notif-ico">${esc(ICONS[n.type] || "•")}</span>
        <span class="lm-notif-main">
          <span class="lm-notif-title">${esc(n.title)}${badge}</span>
          ${body}
          <span class="lm-notif-time">${esc(ago(n.created_at))}</span>
        </span>`;
      // Archived rows are already filed; offering "×" again would suggest a
      // delete this control does not perform.
      const x = n.archived ? "" :
        `<button type="button" class="lm-notif-x" data-archive="${esc(n.id)}"
                 title="移入历史" aria-label="移入历史">×</button>`;
      return n.link_url
        ? `<a class="${cls}" href="${esc(n.link_url)}" data-id="${esc(n.id)}">${inner}${x}</a>`
        : `<div class="${cls}" data-id="${esc(n.id)}">${inner}${x}</div>`;
    }).join("");
  }

  async function pollBadge() {
    try {
      const d = await LMApi.meNotificationsUnread();
      setBadge((d && d.unread) || 0);
    } catch (ex) {
      // A failed poll is not worth interrupting the user for; the next tick
      // recovers. 401 means the session died -- let the page's own guard react.
    }
  }

  async function loadList() {
    try {
      const d = await LMApi.meNotifications({ limit: 30, scope });
      render((d && d.notifications) || []);
      setCounts(d);
    } catch (ex) {
      const host = $("lm-notif-list");
      if (host) host.innerHTML =
        `<div class="lm-notif-item muted">加载失败：${esc(ex.message || "")}</div>`;
    }
  }

  function setOpen(next) {
    open = next;
    const pop = $("lm-notif-pop");
    const btn = $("lm-notif-btn");
    if (pop) pop.hidden = !open;
    if (btn) btn.setAttribute("aria-expanded", open ? "true" : "false");
    if (open) loadList();
  }

  function schedule() {
    clearInterval(timer);
    // Don't poll a tab nobody is looking at.
    if (document.hidden) return;
    timer = setInterval(pollBadge, POLL_MS);
  }

  document.addEventListener("DOMContentLoaded", () => {
    const root = $("lm-notif");
    if (!root || !global.LMApi) return;

    $("lm-notif-btn").addEventListener("click", (e) => {
      e.stopPropagation();
      setOpen(!open);
    });

    // Closing the dropdown does NOT mark anything read. Treating a glance as
    // "read" is what produced the complaint that notifications vanish when you
    // look at them and then pile straight back up: the user never got to act on
    // anything they saw. Marking read is now always an explicit act -- clicking
    // an item, "全部已读", or archiving.
    document.addEventListener("click", (e) => {
      if (open && !root.contains(e.target)) setOpen(false);
    });
    document.addEventListener("keydown", (e) => {
      if (e.key === "Escape" && open) setOpen(false);
    });

    $("lm-notif-readall").addEventListener("click", async (e) => {
      e.stopPropagation();
      try {
        await LMApi.markNotificationsRead();
        await loadList();
      } catch (ex) { /* the next poll re-syncs */ }
    });

    const tabs = $("lm-notif-tabs");
    if (tabs) {
      tabs.addEventListener("click", (e) => {
        const btn = e.target.closest(".lm-notif-tab");
        if (!btn) return;
        e.stopPropagation();
        e.preventDefault();
        if (btn.dataset.scope !== scope) setScope(btn.dataset.scope);
      });
    }

    const clearBtn = $("lm-notif-clear");
    if (clearBtn) {
      clearBtn.addEventListener("click", async (e) => {
        e.stopPropagation();
        if (!confirm("清空历史通知？未读通知不受影响。")) return;
        try {
          await LMApi.clearNotificationHistory();
          await loadList();
        } catch (ex) { /* the next open re-syncs */ }
      });
    }

    // Clicking a notification marks just that one read before navigating, so a
    // link opened in a new tab does not stay bold forever. The "×" archives
    // instead, and must not follow the row's link.
    $("lm-notif-list").addEventListener("click", (e) => {
      const x = e.target.closest("[data-archive]");
      if (x) {
        e.preventDefault();
        e.stopPropagation();
        const aid = Number(x.dataset.archive);
        if (!aid) return;
        LMApi.archiveNotifications([aid])
          .then(() => loadList())
          .catch(() => {});
        return;
      }
      const item = e.target.closest("[data-id]");
      if (!item) return;
      const id = Number(item.dataset.id);
      if (id) LMApi.markNotificationsRead([id]).catch(() => {});
    });

    document.addEventListener("visibilitychange", () => {
      schedule();
      if (!document.hidden) pollBadge();
    });

    pollBadge();
    schedule();
  });
})(window);
