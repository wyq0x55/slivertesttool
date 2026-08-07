/*
 * Audit-log browser (LAN Test Matrix).
 *
 * Extracted from an inline <script> in audit.html so it can be unit-tested the
 * same way home.js / crumb.js are. The previous inline version fetched
 * page_size=200 with no pagination and no truncation notice: on a busy project
 * it silently showed a slice of the log while looking complete -- the exact
 * failure Phase 6 fixed for tasks.
 *
 * Filters are mirrored into the query string via LMUrl.replace() so a filtered
 * audit view can be pasted into a ticket, which is most of the point of having
 * an audit log at all.
 */
(function (window, document) {
  "use strict";

  var FILTER_KEYS = ["action", "result", "actor_id", "date_from", "date_to", "q"];
  var PAGE_SIZE = 50;

  function esc(s) {
    return String(s == null ? "" : s).replace(/[&<>"]/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c];
    });
  }

  /** Truncate a JSON value for the "old -> new" column. */
  function brief(v, limit) {
    if (v == null) return "";
    var s;
    try { s = JSON.stringify(v); } catch (e) { return ""; }
    if (s === undefined) return "";
    limit = limit || 120;
    return s.length > limit ? s.slice(0, limit) + "…" : s;
  }

  var KIND_LABEL = { added: "新增", removed: "移除", changed: "修改",
                     truncated: "" };

  /**
   * Render the server-computed per-field changes.
   *
   * The diff is computed once on the server (comments_service.diff_values) and
   * rendered here, rather than derived again in JavaScript: the CSV export must
   * describe an entry in exactly the same words as the table, and two
   * implementations would drift apart the first time either was touched.
   */
  function renderChanges(changes) {
    if (!changes || !changes.length) {
      return '<span class="lm-muted">—</span>';
    }
    return '<table class="lm-diff">' + changes.map(function (c) {
      if (c.kind === "truncated") {
        return '<tr><td colspan="3" class="lm-muted">' + esc(c.new) +
          "</td></tr>";
      }
      var from = c.old === "" ? '<span class="lm-muted">（空）</span>'
        : "<del>" + esc(c.old) + "</del>";
      var to = c.new === "" ? '<span class="lm-muted">（空）</span>'
        : "<ins>" + esc(c.new) + "</ins>";
      return '<tr class="lm-diff-' + esc(c.kind) + '">' +
        '<th scope="row">' + esc(c.label) +
        (KIND_LABEL[c.kind]
          ? ' <span class="pill st-' + esc(c.kind) + '">' +
            esc(KIND_LABEL[c.kind]) + "</span>"
          : "") +
        "</th><td>" + from + "</td><td>" + to + "</td></tr>";
    }).join("") + "</table>";
  }

  /**
   * Build the CSV download URL for the *current* filters.
   *
   * Server-side by design: the table pages 50 rows at a time, so exporting what
   * the browser holds would produce a file that looks complete and covers one
   * page.
   */
  function exportUrl(projectId, filters) {
    var qs = FILTER_KEYS.filter(function (k) {
      return filters[k] != null && filters[k] !== "";
    }).map(function (k) {
      return encodeURIComponent(k) + "=" + encodeURIComponent(filters[k]);
    }).join("&");
    return "/api/v1/projects/" + encodeURIComponent(projectId) +
      "/audit-logs.csv" + (qs ? "?" + qs : "");
  }

  /** Render an ISO timestamp as local-ish "YYYY-MM-DD HH:MM:SS". */
  function stamp(iso) {
    return String(iso || "").replace("T", " ").replace("Z", "").split(".")[0];
  }

  /**
   * Collect non-empty filter values from the form.
   * Empty strings are dropped rather than sent, so the server sees an absent
   * parameter and applies its default instead of validating "".
   */
  function collect(form) {
    var out = {};
    FILTER_KEYS.forEach(function (k) {
      var el = form.querySelector('[name="' + k + '"]');
      if (!el) return;
      var v = String(el.value == null ? "" : el.value).trim();
      if (v !== "") out[k] = v;
    });
    return out;
  }

  /** Push saved filter values back into the form (deep-link restore). */
  function restore(form, state) {
    FILTER_KEYS.forEach(function (k) {
      var el = form.querySelector('[name="' + k + '"]');
      if (el && state[k] != null) el.value = state[k];
    });
  }

  /** Human summary of the active filters, for the result count line. */
  function describe(filters) {
    var labels = {
      action: "操作", result: "结果", actor_id: "操作人",
      date_from: "起", date_to: "止", q: "关键字"
    };
    return Object.keys(filters).map(function (k) {
      return labels[k] + "=" + filters[k];
    }).join("，");
  }

  /**
   * Append options to a <select>, preserving any value already selected from
   * the URL -- the deep-link restore runs before this async fetch lands, so
   * clobbering the value here would silently discard the user's filter.
   */
  function fillOptions(sel, options) {
    if (!sel || !options || !options.length) return;
    var keep = sel.value;
    options.forEach(function (o) {
      var el = document.createElement("option");
      el.value = o.value;
      el.textContent = o.label;
      sel.appendChild(el);
    });
    sel.value = keep;
  }

  function init(opts) {
    var root = opts.root;
    var api = opts.api;
    var url = opts.url;
    var pid = opts.projectId;
    var form = root.querySelector("#lm-audit-filters");
    var rows = root.querySelector("#lm-audit-rows");
    var info = root.querySelector("#lm-audit-info");
    var more = root.querySelector("#lm-audit-more");
    var exportLink = root.querySelector("#lm-audit-export");
    var page = 1;
    var loaded = 0;

    /**
     * Keep the export link pointing at the filters currently in the form.
     * A stale href here would hand the reviewer a file that silently disagrees
     * with the screen they were looking at when they clicked it.
     */
    function syncExport() {
      if (!exportLink) return;
      var filters = collect(form);
      exportLink.href = exportUrl(pid, filters);
      var n = Object.keys(filters).length;
      exportLink.textContent = n ? "导出 CSV（已筛选）" : "导出 CSV";
      exportLink.title = n
        ? "按当前筛选条件导出全部匹配记录：" + describe(filters)
        : "导出本项目全部审计日志";
    }

    function setInfo(text, cls) {
      if (!info) return;
      info.textContent = text;
      info.className = cls || "lm-muted";
    }

    function render(items, append) {
      var html = items.map(function (a) {
        return '<tr><td class="lm-muted">' + esc(stamp(a.created_at)) + "</td>" +
          "<td><code>" + esc(a.action) + "</code></td>" +
          "<td>" + esc(a.object_type) + " " + esc(a.object_id || "") + "</td>" +
          "<td>" + esc(a.actor_name || a.actor_id || "") + "</td>" +
          "<td>" + renderChanges(a.changes) + "</td></tr>";
      }).join("");
      if (append) rows.insertAdjacentHTML("beforeend", html);
      else rows.innerHTML = html;
    }

    function load(append) {
      var filters = collect(form);
      var query = { page: append ? page : 1, page_size: PAGE_SIZE };
      FILTER_KEYS.forEach(function (k) {
        if (filters[k] != null) query[k] = filters[k];
      });
      if (!append) { page = 1; loaded = 0; }
      syncExport();
      setInfo("加载中…");
      return api.listAudit(pid, query).then(function (data) {
        var items = data.items || [];
        loaded += items.length;
        render(items, append);
        if (!loaded) {
          // Distinguish "nothing ever happened" from "your filters hid it".
          rows.innerHTML = '<tr><td colspan="5" class="lm-muted">' +
            (data.filtered
              ? "没有符合筛选条件的日志，试试放宽条件或清空筛选"
              : "暂无日志") + "</td></tr>";
          setInfo(data.filtered ? "0 条（已筛选：" + describe(filters) + "）" : "0 条");
        } else {
          setInfo("已显示 " + loaded + " / 共 " + data.total + " 条" +
            (Object.keys(filters).length ? "（已筛选：" + describe(filters) + "）" : ""));
        }
        if (more) more.hidden = loaded >= (data.total || 0);
        return data;
      }).catch(function (ex) {
        if (ex && ex.status === 401 && window.LM && window.LM.urls) {
          window.location = window.LM.urls.login;
          return;
        }
        rows.innerHTML = '<tr><td colspan="5" class="lm-err">' +
          esc(ex && ex.message) + "</td></tr>";
        setInfo("加载失败", "lm-err");
      });
    }

    function apply(pushUrl) {
      var filters = collect(form);
      if (url && pushUrl !== false) {
        var patch = {};
        FILTER_KEYS.forEach(function (k) { patch[k] = filters[k] || null; });
        url.replace(patch);
      }
      return load(false);
    }

    if (form) {
      form.addEventListener("submit", function (e) {
        e.preventDefault();
        apply();
      });
      var reset = root.querySelector("#lm-audit-reset");
      if (reset) {
        reset.addEventListener("click", function (e) {
          e.preventDefault();
          FILTER_KEYS.forEach(function (k) {
            var el = form.querySelector('[name="' + k + '"]');
            if (el) el.value = "";
          });
          apply();
        });
      }
    }
    if (more) {
      more.addEventListener("click", function (e) {
        e.preventDefault();
        page += 1;
        load(true);
      });
    }

    if (url) {
      restore(form, url.all());
      url.onPop(function (state) { restore(form, state); load(false); });
    }

    // Populate the dropdowns from real data, so a newly introduced action type
    // or a new team member can never become unfilterable.
    if (api.listAuditActions) {
      api.listAuditActions(pid).then(function (d) {
        if (!d || !form) return;
        fillOptions(form.querySelector('[name="action"]'), (d.actions || []).map(
          function (a) { return { value: a, label: a }; }));
        fillOptions(form.querySelector('[name="actor_id"]'), (d.actors || []).map(
          function (a) { return { value: String(a.id), label: a.name }; }));
      }).catch(function () { /* dropdowns stay at 全部; not fatal */ });
    }

    load(false);
    return { load: load, apply: apply, syncExport: syncExport,
             collect: function () { return collect(form); } };
  }

  window.LMAudit = {
    init: init, esc: esc, brief: brief, stamp: stamp,
    collect: collect, restore: restore, describe: describe,
    fillOptions: fillOptions, renderChanges: renderChanges,
    exportUrl: exportUrl,
    FILTER_KEYS: FILTER_KEYS, PAGE_SIZE: PAGE_SIZE
  };
})(window, document);
