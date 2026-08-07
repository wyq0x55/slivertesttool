/*
 * urlstate.js — keep view state in the URL (window.LMUrl).
 *
 * WHY
 * ---
 * Before this, no page in the console touched history at all: zero calls to
 * pushState/replaceState anywhere in first-party JS. Three consequences, all
 * of which users hit daily:
 *
 *   1. Nothing is linkable. You cannot paste "the failing task" into a chat —
 *      only "open 任务, filter 失败, find TASK-123". For a test platform, where
 *      the whole job is telling a colleague which run broke, that is the single
 *      biggest workflow gap.
 *   2. Back does the wrong thing. Task detail is an inline section toggled in
 *      place, so Back exits the entire page instead of returning to the list —
 *      losing the user's filters and scroll position.
 *   3. Refresh loses everything: selected tab, filters, open task.
 *
 * DESIGN
 * ------
 * Query parameters, not hash fragments. Hash collides with anchor navigation
 * and never reaches the server (so a page can never server-render the right
 * initial view later), and mixing `#tab` with `?task` would reintroduce exactly
 * the kind of inconsistency this refactor exists to remove. `?tab=` is what
 * GitHub and Jira use.
 *
 * Two write modes, and picking the right one is what makes Back feel correct:
 *   set()     -> pushState.    A navigation the user should be able to undo
 *                              (opening a task, switching tab).
 *   replace() -> replaceState. A refinement of the current view (typing in the
 *                              filter box). Pushing here would bury the real
 *                              history under one entry per keystroke.
 *
 * Empty/null values are DELETED rather than written as `key=`, so a default
 * view has a clean URL and the "did anything change" check below stays honest.
 */
(function (window) {
  "use strict";

  var loc = window.location;
  var history = window.history;
  var SUPPORTED = !!(history && typeof history.pushState === "function");

  function params() {
    return new URLSearchParams(loc.search);
  }

  /* Build the full "path?query" string for a given patch, without applying it. */
  function urlFor(patch) {
    var p = params();
    Object.keys(patch || {}).forEach(function (k) {
      var v = patch[k];
      if (v === null || v === undefined || v === "" || v === false) p.delete(k);
      else p.set(k, String(v));
    });
    var qs = p.toString();
    return loc.pathname + (qs ? "?" + qs : "");
  }

  function write(patch, replace) {
    if (!SUPPORTED) return false;
    var next = urlFor(patch);
    var current = loc.pathname + (loc.search || "");
    // No-op guard. Without this, re-selecting the already-active tab would push
    // a duplicate entry and Back would appear frozen (one press = no visible
    // change). Cheap to check, and it keeps the history stack honest.
    if (next === current) return false;
    try {
      history[replace ? "replaceState" : "pushState"]({ lm: true }, "", next);
    } catch (e) {
      return false;   // file:// or a sandboxed iframe — degrade, never throw
    }
    return true;
  }

  var LMUrl = {
    supported: SUPPORTED,

    /** Current value of one param, or `fallback` when absent/empty. */
    get: function (key, fallback) {
      var v = params().get(key);
      return v === null || v === "" ? (fallback === undefined ? "" : fallback) : v;
    },

    /** Whole query as a plain object (useful for restoring several at once). */
    all: function () {
      var out = {};
      params().forEach(function (v, k) { out[k] = v; });
      return out;
    },

    /** Push a new history entry. Use for user-initiated navigation. */
    set: function (patch) { return write(patch, false); },

    /** Rewrite the current entry. Use for filter/sort refinements. */
    replace: function (patch) { return write(patch, true); },

    /**
     * Subscribe to Back/Forward. The callback receives the post-navigation
     * params object, so callers never have to re-parse location themselves.
     */
    onPop: function (fn) {
      if (!SUPPORTED) return;
      window.addEventListener("popstate", function () { fn(LMUrl.all()); });
    },
  };

  window.LMUrl = LMUrl;
})(window);
