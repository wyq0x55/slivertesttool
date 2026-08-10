/* pager.js — client-side pagination for the workspace lists (window.LMPager).
 *
 * WHY
 * ---
 * The workspace used to render every row it had fetched in one flat wall:
 * 200 tasks, every pending review and every project, stacked on a single
 * scroll. The page that is supposed to answer "what needs me now" turned into
 * the longest page in the product, and the section below was only reachable by
 * scrolling past the section above.
 *
 * The data is already capped and already fetched (the server ships at most a
 * few hundred rows per list), so paging is a pure view concern: no extra round
 * trip, instant page switches, and the caller keeps owning its data array.
 *
 * CONTRACT
 * --------
 *   var p = LMPager.create({ host, pageSize, onChange });
 *   p.setTotal(n)   -> clamp the current page and repaint the control
 *   p.slice(rows)   -> the rows belonging to the current page
 *   p.reset()       -> back to page 1 (call when the filter changes)
 *   p.offset()      -> index of the first visible row (for stable row numbers)
 *
 * `host` is any element; the control is rendered into it and hidden entirely
 * when there is only one page — a pager on a 3-row list is noise.
 */
(function (window) {
  "use strict";

  var DEFAULT_PAGE_SIZE = 20;

  function create(opts) {
    var options = opts || {};
    var host = options.host || null;
    var pageSize = Math.max(1, options.pageSize || DEFAULT_PAGE_SIZE);
    var onChange = typeof options.onChange === "function" ? options.onChange : null;
    var page = 1;
    var total = 0;

    function pages() {
      return Math.max(1, Math.ceil(total / pageSize));
    }

    function clamp(n) {
      return Math.min(pages(), Math.max(1, n || 1));
    }

    function render() {
      if (!host) return;
      var last = pages();
      // One page of results needs no controls.
      host.hidden = total <= pageSize;
      if (host.hidden) { host.innerHTML = ""; return; }
      var from = (page - 1) * pageSize + 1;
      var to = Math.min(total, page * pageSize);
      host.innerHTML =
        '<button type="button" class="btn small" data-pg="prev"' +
        (page <= 1 ? " disabled" : "") + ">上一页</button>" +
        '<span class="lm-pager-info">第 ' + page + " / " + last +
        " 页 · " + from + "–" + to + " / " + total + " 条</span>" +
        '<button type="button" class="btn small" data-pg="next"' +
        (page >= last ? " disabled" : "") + ">下一页</button>";
    }

    function goto(next, silent) {
      var want = clamp(next);
      if (want === page) { render(); return; }
      page = want;
      render();
      if (onChange && !silent) onChange(page);
    }

    if (host) {
      host.classList.add("lm-pager");
      host.hidden = true;
      host.addEventListener("click", function (e) {
        var btn = e.target.closest("[data-pg]");
        if (!btn || btn.disabled) return;
        goto(btn.dataset.pg === "prev" ? page - 1 : page + 1);
      });
    }

    return {
      /** Total row count; clamps the page so deleting rows cannot strand it. */
      setTotal: function (n) {
        total = Math.max(0, Number(n) || 0);
        page = clamp(page);
        render();
      },
      /** Rows for the current page. */
      slice: function (rows) {
        var all = rows || [];
        var start = (page - 1) * pageSize;
        return all.slice(start, start + pageSize);
      },
      offset: function () { return (page - 1) * pageSize; },
      /** Back to page 1 without firing onChange (the caller is re-rendering). */
      reset: function () { page = 1; render(); },
      page: function () { return page; },
      pages: pages,
      pageSize: function () { return pageSize; },
      render: render,
    };
  }

  window.LMPager = { create: create, DEFAULT_PAGE_SIZE: DEFAULT_PAGE_SIZE };
})(window);
