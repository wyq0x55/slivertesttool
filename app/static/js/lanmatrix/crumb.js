/* crumb.js — fill the project name into the breadcrumb.
 *
 * The six project-scoped pages render `项目 #<id>` server-side and this
 * upgrades it to `<code> · <name>` once the API answers. Two reasons it is
 * client-side: the page routes are thin shells that hold no project data, and
 * `GET /projects/<id>` already enforces membership, so no permission logic gets
 * duplicated into the page layer.
 *
 * The name is cached in sessionStorage. Navigating 编辑 → 成员 → 字段 within one
 * project is the common path, and without a cache each hop would flash the raw
 * id for the length of a round trip. sessionStorage (not localStorage) because
 * a renamed project should not stay wrong in a new session, and the cache is
 * keyed by project id so switching projects can't show the previous name.
 */
(function (window, document) {
  "use strict";

  var KEY = "lm.crumb.project";

  function readCache(id) {
    try {
      var raw = window.sessionStorage.getItem(KEY);
      if (!raw) return null;
      var hit = JSON.parse(raw);
      return hit && String(hit.id) === String(id) ? hit.text : null;
    } catch (e) {
      // Private mode / disabled storage: the cache is an optimisation, so a
      // failure here must degrade to "fetch it" rather than break the crumb.
      return null;
    }
  }

  function writeCache(id, text) {
    try {
      window.sessionStorage.setItem(KEY, JSON.stringify({ id: String(id), text: text }));
    } catch (e) { /* ignore */ }
  }

  function label(p) {
    if (!p) return "";
    if (p.code && p.name) return p.code + " · " + p.name;
    return p.name || p.code || "";
  }

  // Both call sites already reject empty text, so there is no guard here on
  // purpose: an unreachable branch can't be tested and only reads as if the
  // empty case were handled somewhere meaningful.
  function apply(el, text) {
    el.textContent = text;
    el.title = text;
  }

  function init() {
    var el = document.getElementById("lm-crumb-project");
    if (!el) return;
    var id = el.getAttribute("data-project-id");
    if (!id) return;

    var cached = readCache(id);
    if (cached) apply(el, cached);

    if (!window.LMApi || typeof window.LMApi.getProject !== "function") return;
    window.LMApi.getProject(id).then(function (data) {
      var text = label(data && data.project);
      if (!text) return;
      apply(el, text);
      writeCache(id, text);
    }).catch(function () {
      // No permission, deleted project, or offline. The server-rendered
      // `项目 #<id>` stays — a breadcrumb is navigation furniture and must not
      // raise an error dialog over whatever the user came here to do.
    });
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }

  window.LMCrumb = { init: init, _label: label };
})(window, document);
