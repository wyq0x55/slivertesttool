/* ECharts theme bridge — makes charts inherit the app's design system.
 *
 * Why compute the theme instead of writing one
 * --------------------------------------------
 * No charting library "just matches" a bespoke design system out of the box.
 * What actually decides the fit is whether the theme can be *derived* from the
 * tokens already in the stylesheet. Every colour below is read at runtime with
 * getComputedStyle from :root, so there is exactly one palette in the codebase.
 * A second, hand-maintained chart palette would drift out of step with the CSS
 * the first time anyone touched a token.
 *
 * Charts read --chart-* tokens, never --ok / --warn / --danger directly. Those
 * semantic tokens are tuned for small marks on a --*-soft background; a chart
 * fills large areas against --surface, and the dark theme lifts --chart-* for
 * contrast. Using the raw semantic token here would look fine in light mode and
 * regress silently in dark mode.
 *
 * Theme switching is handled by a MutationObserver on html[data-theme]: ECharts
 * bakes the theme in at init(), so each live instance is rebuilt from its own
 * current option. Without that, charts keep light-mode text on a dark card.
 *
 * Degrades quietly: if the vendored bundle is absent, ready() returns false and
 * callers show numbers instead of an empty page.
 */
(function (global) {
  "use strict";

  var THEME = "lm";
  var ec = global.echarts || null;
  var instances = [];
  var registered = false;

  function token(name, fallback) {
    try {
      var v = getComputedStyle(document.documentElement)
        .getPropertyValue(name)
        .trim();
      return v || fallback;
    } catch (e) {
      return fallback;
    }
  }

  /* Semantic colours for the dashboard, resolved from CSS.
   * The fallbacks are a safety net for a stylesheet that failed to load; in
   * normal operation every value comes from design-system.css. */
  function palette() {
    var brand = token("--chart-brand", token("--brand", "#5E6AD2"));
    return {
      brand: brand,
      ok: token("--chart-ok", "#12a150"),
      warn: token("--chart-warn", "#c4841d"),
      danger: token("--chart-danger", "#e5484d"),
      info: token("--chart-info", "#3b82c4"),
      muted: token("--chart-muted", "#a1a5ad"),
      grid: token("--chart-grid", token("--border", "#e3e5e8")),
      axis: token("--chart-axis", token("--text-3", "#71757e")),
      text: token("--text", "#17181c"),
      text2: token("--text-2", "#565a63"),
      surface: token("--surface", "#ffffff"),
      font: token("--font", "sans-serif"),
      radius: token("--radius", "12px"),
    };
  }

  function buildTheme() {
    var p = palette();
    var axis = {
      axisLine: { show: true, lineStyle: { color: p.grid } },
      axisTick: { show: false },
      axisLabel: { color: p.axis, fontFamily: p.font },
      splitLine: { lineStyle: { color: p.grid, type: "dashed" } },
    };
    return {
      // Order matters: series without an explicit colour cycle through this.
      color: [p.brand, p.ok, p.warn, p.danger, p.info, p.muted],
      backgroundColor: "transparent", // the card behind the chart owns the surface
      textStyle: { color: p.text, fontFamily: p.font },
      title: {
        textStyle: { color: p.text, fontFamily: p.font, fontSize: 14 },
        subtextStyle: { color: p.axis, fontFamily: p.font },
      },
      legend: { textStyle: { color: p.text2, fontFamily: p.font } },
      tooltip: {
        backgroundColor: p.surface,
        borderColor: p.grid,
        borderWidth: 1,
        textStyle: { color: p.text, fontFamily: p.font },
        extraCssText:
          "box-shadow:0 12px 32px -8px rgba(0,0,0,.18);border-radius:" +
          p.radius +
          ";",
      },
      categoryAxis: axis,
      valueAxis: axis,
      logAxis: axis,
      timeAxis: axis,
    };
  }

  function ensureRegistered() {
    if (!ec) return false;
    if (!registered) {
      ec.registerTheme(THEME, buildTheme());
      registered = true;
    }
    return true;
  }

  function ready() {
    return !!(global.echarts || ec);
  }

  /* Create a themed chart bound to an element. Returns null when the bundle is
   * missing so callers can fall back rather than throw. */
  function init(el) {
    ec = ec || global.echarts;
    if (!ec || !el) return null;
    ensureRegistered();
    var chart = ec.init(el, THEME, { renderer: "canvas" });
    instances.push({ chart: chart, el: el });
    return chart;
  }

  function dispose(chart) {
    if (!chart) return;
    instances = instances.filter(function (r) {
      return r.chart !== chart;
    });
    try {
      chart.dispose();
    } catch (e) {
      /* already disposed */
    }
  }

  function disposeAll() {
    instances.slice().forEach(function (r) {
      dispose(r.chart);
    });
  }

  /* Re-read tokens and rebuild every live chart under the new theme.
   * Each element gets an `lm-chart-rebuilt` CustomEvent carrying the new
   * instance, because the old handle the page holds is now disposed. */
  function refreshTheme() {
    if (!ec) return;
    registered = false;
    ensureRegistered();
    instances.slice().forEach(function (rec) {
      var option;
      try {
        option = rec.chart.getOption();
      } catch (e) {
        return;
      }
      var el = rec.el;
      dispose(rec.chart);
      var next = init(el);
      if (!next) return;
      try {
        next.setOption(option, true);
      } catch (e) {
        /* option from a disposed instance is best-effort */
      }
      el.dispatchEvent(
        new CustomEvent("lm-chart-rebuilt", { detail: { chart: next } })
      );
    });
  }

  function resizeAll() {
    instances.forEach(function (r) {
      try {
        r.chart.resize();
      } catch (e) {
        /* detached from the DOM */
      }
    });
  }

  // Follow the app's theme toggle without every page wiring it up itself.
  if (typeof MutationObserver !== "undefined" && document.documentElement) {
    new MutationObserver(function (records) {
      for (var i = 0; i < records.length; i++) {
        if (records[i].attributeName === "data-theme") {
          refreshTheme();
          return;
        }
      }
    }).observe(document.documentElement, { attributes: true });
  }

  // One shared, debounced resize listener rather than one per chart.
  var resizeTimer = null;
  global.addEventListener("resize", function () {
    clearTimeout(resizeTimer);
    resizeTimer = setTimeout(resizeAll, 120);
  });

  global.LMCharts = {
    theme: THEME,
    ready: ready,
    init: init,
    dispose: dispose,
    disposeAll: disposeAll,
    palette: palette,
    refreshTheme: refreshTheme,
    resizeAll: resizeAll,
  };
})(window);
