/*
 * Recycle bin (LAN Test Matrix).
 *
 * Deleting a row, a field or a task used to be final and unannounced. This is
 * the view that makes those deletions inspectable and undoable for 30 days.
 *
 * Two things this screen must never get wrong:
 *
 * 1. The remaining retention has to be on screen, per entry, in words. A bin
 *    that lists 40 items with no dates reads as permanent storage, and the one
 *    thing worse than no undo is an undo the user believes in until the day it
 *    silently expires. Entries close to expiry are called out, not just sorted.
 *
 * 2. Restore and purge must not look alike. Restore is the safe, common action
 *    and is a plain button. Purge is the only irreversible action on the page
 *    and goes through LMUI.confirm at "critical" level -- the user retypes the
 *    object's name, the same pattern GitHub uses for deleting a repository.
 *
 * Filters are mirrored into the query string via LMUrl so a filtered bin can be
 * pasted into a ticket ("your field is still here, look").
 */
(function (window, document) {
  "use strict";

  // Below this many days remaining an entry is called out rather than merely
  // listed. A week is about the shortest notice that still lets someone who is
  // away from the office act on it when they get back.
  var SOON_DAYS = 7;

  var KIND_LABEL = { item: "测试用例", field: "字段", task: "任务" };

  function esc(s) {
    return String(s == null ? "" : s).replace(/[&<>"]/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c];
    });
  }

  /** Render an ISO timestamp as "YYYY-MM-DD HH:MM:SS". */
  function stamp(iso) {
    return String(iso || "").replace("T", " ").replace("Z", "").split(".")[0];
  }

  /** Just the date part, for the expiry column. */
  function day(iso) {
    return String(iso || "").split("T")[0];
  }

  /**
   * The retention sentence for one entry.
   *
   * Deliberately absolute *and* relative: "还有 3 天" is what makes someone act
   * today, and the date is what they put in a calendar. Either alone is weaker.
   */
  function retentionText(entry) {
    var d = entry && entry.days_left;
    if (d == null) return "";
    if (d <= 0) return "已过保留期，下次清理时删除";
    return "还有 " + d + " 天（" + day(entry.expires_at) + " 彻底删除）";
  }

  /** Severity class for the retention cell. */
  function retentionClass(entry) {
    var d = entry && entry.days_left;
    if (d == null) return "";
    if (d <= 0) return "lm-trash-expired";
    return d <= SOON_DAYS ? "lm-trash-soon" : "";
  }

  /**
   * Count entries that expire within SOON_DAYS.
   *
   * Surfaced as a banner: per-row styling is invisible on a bin long enough to
   * scroll, which is exactly when something is most likely to lapse unnoticed.
   */
  function countExpiringSoon(entries) {
    return (entries || []).filter(function (e) {
      return e.days_left != null && e.days_left <= SOON_DAYS;
    }).length;
  }

  /**
   * The "showing N of M" line.
   *
   * A bin that shows 100 of 400 without saying so reads as "the other 300 are
   * already gone" -- the single most alarming thing this screen could imply.
   */
  function summaryText(data) {
    var total = (data && data.total) || 0;
    var shown = ((data && data.entries) || []).length;
    if (!total) return "回收站是空的";
    if (data.truncated) {
      return "共 " + total + " 项，仅显示最近 " + shown + " 项（其余仍在回收站中，可按类型筛选查看）";
    }
    return "共 " + total + " 项";
  }

  function rowHtml(e, canRestore, canPurge) {
    var actions = "";
    if (canRestore) {
      actions += '<button type="button" class="btn btn-sm" data-act="restore">还原</button> ';
    }
    if (canPurge) {
      actions += '<button type="button" class="btn btn-sm btn-danger" data-act="purge">彻底删除</button>';
    }
    if (!actions) actions = '<span class="lm-muted">无权限</span>';
    var by = e.deleted_by_name || (e.deleted_by ? "#" + e.deleted_by : "");
    return '<tr data-kind="' + esc(e.kind) + '" data-id="' + esc(e.id) + '">' +
      '<td><span class="pill st-' + esc(e.kind) + '">' +
        esc(e.kind_label || KIND_LABEL[e.kind] || e.kind) + "</span></td>" +
      '<td><span class="lm-trash-title">' + esc(e.title) + "</span>" +
        (e.subtitle ? '<br><span class="lm-muted">' + esc(e.subtitle) + "</span>" : "") +
      "</td>" +
      '<td class="lm-muted">' + esc(stamp(e.deleted_at)) + "</td>" +
      '<td class="lm-muted">' + esc(by) + "</td>" +
      '<td class="' + retentionClass(e) + '">' + esc(retentionText(e)) + "</td>" +
      '<td class="lm-trash-actions">' + actions + "</td></tr>";
  }

  function init(opts) {
    var root = opts.root;
    var api = opts.api;
    var url = opts.url;
    var ui = opts.ui || window.LMUI;
    var pid = opts.projectId;
    var canRestore = !!opts.canRestore;
    var canPurge = !!opts.canPurge;
    var form = root.querySelector("#lm-trash-filters");
    var rows = root.querySelector("#lm-trash-rows");
    var info = root.querySelector("#lm-trash-info");
    var notice = root.querySelector("#lm-trash-notice");
    var retentionNote = root.querySelector("#lm-trash-retention");
    var last = { entries: [], total: 0 };

    function setInfo(text, cls) {
      if (!info) return;
      info.textContent = text;
      info.className = cls || "lm-muted";
    }

    function currentKind() {
      var el = form && form.querySelector('[name="kind"]');
      var v = el ? String(el.value || "").trim() : "";
      return v || null;
    }

    function setNotice(entries) {
      if (!notice) return;
      var n = countExpiringSoon(entries);
      notice.hidden = !n;
      if (n) {
        notice.textContent =
          n + " 项将在 " + SOON_DAYS + " 天内被彻底删除，如仍需要请尽快还原。";
      }
    }

    function render(data) {
      var entries = (data && data.entries) || [];
      last = data || { entries: [], total: 0 };
      if (!entries.length) {
        rows.innerHTML = '<tr><td colspan="6" class="lm-muted">' +
          (currentKind() ? "该类型下没有已删除的内容" : "回收站是空的") +
          "</td></tr>";
      } else {
        rows.innerHTML = entries.map(function (e) {
          return rowHtml(e, canRestore, canPurge);
        }).join("");
      }
      setInfo(summaryText(data));
      setNotice(entries);
      if (retentionNote && data && data.retention_days) {
        retentionNote.textContent =
          "删除的内容会保留 " + data.retention_days + " 天，之后自动彻底删除。";
      }
    }

    function load() {
      setInfo("加载中…");
      var query = {};
      var kind = currentKind();
      if (kind) query.kind = kind;
      return api.listTrash(pid, query).then(function (data) {
        render(data);
        return data;
      }).catch(function (ex) {
        if (ex && ex.status === 401 && window.LM && window.LM.urls) {
          window.location = window.LM.urls.login;
          return;
        }
        rows.innerHTML = '<tr><td colspan="6" class="lm-err">' +
          esc(ex && ex.message) + "</td></tr>";
        setInfo("加载失败", "lm-err");
      });
    }

    function apply(pushUrl) {
      if (url && pushUrl !== false) url.replace({ kind: currentKind() });
      return load();
    }

    function findEntry(kind, id) {
      var all = last.entries || [];
      for (var i = 0; i < all.length; i++) {
        if (all[i].kind === kind && String(all[i].id) === String(id)) {
          return all[i];
        }
      }
      return null;
    }

    function doRestore(kind, id) {
      var e = findEntry(kind, id);
      return api.restoreFromTrash(pid, kind, id).then(function () {
        setInfo("已还原：" + ((e && e.title) || id));
        return load();
      }).catch(function (ex) {
        setInfo("还原失败：" + (ex && ex.message), "lm-err");
      });
    }

    function doPurge(kind, id) {
      var e = findEntry(kind, id);
      var title = (e && e.title) || String(id);
      var label = (e && e.kind_label) || KIND_LABEL[kind] || kind;
      var body = "此操作不可撤销，将立即永久删除该" + label + "。";
      if (kind === "field") {
        // The one consequence that is not obvious from the row itself.
        body += "该字段在所有测试用例中已填写的值也会一并删除。";
      } else if (kind === "task") {
        body += "该任务的日志与报告文件也会一并删除。";
      }
      if (e && e.days_left > 0) {
        body += "若不处理，它会在 " + e.days_left + " 天后自动删除。";
      }
      return Promise.resolve(ui.confirm({
        title: "彻底删除「" + title + "」？",
        body: body,
        level: "critical",
        requireText: title,
        confirmText: "永久删除"
      })).then(function (okd) {
        if (!okd) return null;
        return api.purgeFromTrash(pid, kind, id).then(function () {
          setInfo("已彻底删除：" + title);
          return load();
        }).catch(function (ex) {
          setInfo("删除失败：" + (ex && ex.message), "lm-err");
        });
      });
    }

    if (rows) {
      rows.addEventListener("click", function (ev) {
        var btn = ev.target.closest ? ev.target.closest("button[data-act]") : null;
        if (!btn) return;
        var tr = btn.closest("tr");
        if (!tr) return;
        ev.preventDefault();
        var kind = tr.dataset.kind;
        var id = tr.dataset.id;
        if (btn.dataset.act === "restore") doRestore(kind, id);
        else doPurge(kind, id);
      });
    }

    if (form) {
      form.addEventListener("submit", function (e) {
        e.preventDefault();
        apply();
      });
      form.addEventListener("change", function () { apply(); });
    }

    if (url) {
      var state = url.all();
      var sel = form && form.querySelector('[name="kind"]');
      if (sel && state.kind) sel.value = state.kind;
      url.onPop(function (s) {
        if (sel) sel.value = s.kind || "";
        load();
      });
    }

    load();
    return { load: load, apply: apply, restore: doRestore, purge: doPurge };
  }

  window.LMTrash = {
    init: init, esc: esc, stamp: stamp, day: day,
    retentionText: retentionText, retentionClass: retentionClass,
    countExpiringSoon: countExpiringSoon, summaryText: summaryText,
    rowHtml: rowHtml,
    SOON_DAYS: SOON_DAYS, KIND_LABEL: KIND_LABEL
  };
})(window, document);
