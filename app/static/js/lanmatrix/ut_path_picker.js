/* Execution-server path picker for the project binding (window.LMUtPathPicker).
 *
 * The bound paths live on the machine that runs ut-agent, so a browser file
 * dialog cannot produce them; this dialog walks the server's directories one
 * listing at a time through GET /ut/server-paths. */
(function (window, document) {
  "use strict";
  var FOLDER = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/></svg>';
  var FILE = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M14 3H7a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V8z"/><path d="M14 3v5h5M9 13h6M9 17h6"/></svg>';
  var HELP = {
    directory: "列出执行服务器上的目录。进入目标目录后点击「选择此目录」。",
    csv: "列出执行服务器上的目录和 CSV 文件。点击文件选中，双击直接选择。"
  };

  function $(id) { return document.getElementById(id); }

  function open(options) {
    var dialog = $("ut-path-dialog");
    var list = $("ut-path-list"), status = $("ut-path-status"), location = $("ut-path-location");
    var up = $("ut-path-up"), choose = $("ut-path-choose");
    var kind = options.kind === "csv" ? "csv" : "directory";
    var listing = null, picked = null, token = 0, lastFocused = document.activeElement;
    var controller = new AbortController();

    $("ut-path-title").textContent = options.title || "选择路径";
    $("ut-path-help").textContent = HELP[kind];

    function say(message, isError) {
      status.textContent = message || "";
      status.classList.toggle("is-error", !!isError);
    }
    function refresh() {
      up.disabled = !listing || listing.parent === null;
      choose.textContent = kind === "csv" ? "选择文件" : "选择此目录";
      choose.disabled = kind === "csv" ? !picked : !(listing && listing.path);
      if (kind === "csv") say(picked ? "已选中：" + picked : "请选择一个 CSV 文件。");
      else say(listing && listing.path ? "将选择：" + listing.path : "请先进入一个目录。");
    }
    function render() {
      list.replaceChildren();
      if (!listing.entries.length) {
        var empty = document.createElement("p");
        empty.className = "ut-path-empty";
        empty.textContent = kind === "csv" ? "此目录下没有子目录或 CSV 文件。" : "此目录下没有子目录。";
        list.appendChild(empty);
      }
      listing.entries.forEach(function (entry) {
        var row = document.createElement("button");
        row.type = "button";
        row.className = "ut-path-entry is-" + entry.type;
        row.setAttribute("role", "option");
        row.setAttribute("aria-selected", String(entry.path === picked));
        row.dataset.path = entry.path;
        row.innerHTML = entry.type === "file" ? FILE : FOLDER;
        var name = document.createElement("span");
        name.textContent = entry.name;
        row.appendChild(name);
        list.appendChild(row);
      });
      if (listing.truncated) {
        var more = document.createElement("p");
        more.className = "ut-path-empty";
        more.textContent = "条目过多，仅显示前一部分。可在上方直接输入路径。";
        list.appendChild(more);
      }
      list.scrollTop = 0;
    }
    // `initial`: a bound value the server rejects (e.g. a relative path) opens
    // the root list instead of an empty dialog.
    async function go(path, initial) {
      var mine = ++token;
      list.setAttribute("aria-busy", "true");
      say("正在读取…");
      try {
        var result = await LMApi.utServerPaths(options.pid, kind, path);
        if (mine !== token) return;
        listing = result;
        location.value = result.path;
        picked = kind === "csv" ? result.selected : null;
        render();
        refresh();
      } catch (e) {
        if (mine !== token) return;
        var message = e.message || "读取失败";
        if (initial && path) { await go(""); say(message, true); return; }
        say(message, true);
      } finally {
        if (mine === token) list.removeAttribute("aria-busy");
      }
    }

    return new Promise(function (resolve) {
      function done(value) {
        controller.abort();
        token++;
        if (dialog.open) dialog.close();
        try { if (lastFocused && lastFocused.focus) lastFocused.focus(); } catch (e) { /* trigger may be gone */ }
        resolve(value);
      }
      var signal = { signal: controller.signal };
      list.addEventListener("click", function (event) {
        var row = event.target.closest(".ut-path-entry");
        if (!row) return;
        if (row.classList.contains("is-directory")) { go(row.dataset.path); return; }
        picked = row.dataset.path;
        list.querySelectorAll(".ut-path-entry").forEach(function (node) {
          node.setAttribute("aria-selected", String(node === row));
        });
        refresh();
      }, signal);
      list.addEventListener("dblclick", function (event) {
        var row = event.target.closest(".ut-path-entry.is-file");
        if (row) done(row.dataset.path);
      }, signal);
      up.addEventListener("click", function () { if (listing && listing.parent !== null) go(listing.parent); }, signal);
      $("ut-path-go").addEventListener("click", function () { go(location.value.trim()); }, signal);
      location.addEventListener("keydown", function (event) {
        if (event.key === "Enter") { event.preventDefault(); go(location.value.trim()); }
      }, signal);
      choose.addEventListener("click", function () {
        if (choose.disabled) return;
        done(kind === "csv" ? picked : listing.path);
      }, signal);
      $("ut-path-cancel").addEventListener("click", function () { done(null); }, signal);
      dialog.addEventListener("cancel", function (event) { event.preventDefault(); done(null); }, signal);
      dialog.addEventListener("click", function (event) { if (event.target === dialog) done(null); }, signal);

      listing = null; picked = null;
      list.replaceChildren();
      location.value = options.value || "";
      refresh();
      dialog.showModal();
      list.focus();
      go((options.value || "").trim(), true);
    });
  }

  window.LMUtPathPicker = { open: open };
})(window, document);
