const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const THEME_PATH = path.join(__dirname, "../../app/static/js/lanmatrix/charts_theme.js");
const BUNDLE_PATH = path.join(__dirname, "../../app/static/vendor/charts/echarts.min.js");
const THEME_SOURCE = fs.readFileSync(THEME_PATH, "utf8");

// The bridge is a browser IIFE: it reads window/document/getComputedStyle off
// globals and publishes window.LMCharts. A vm context supplies only those.
function loadBridge(echarts) {
  const observers = [];
  const windowListeners = [];
  const window = {
    echarts,
    addEventListener(type) {
      windowListeners.push(type);
    },
  };
  const document = { documentElement: {} };
  const sandbox = {
    window,
    document,
    getComputedStyle: () => ({ getPropertyValue: () => "" }),
    MutationObserver: class {
      constructor(callback) {
        this.callback = callback;
        observers.push(this);
      }
      observe(target, options) {
        this.target = target;
        this.options = options;
      }
      disconnect() {}
    },
    CustomEvent: class {
      constructor(type, init) {
        this.type = type;
        this.detail = init && init.detail;
      }
    },
    setTimeout,
    clearTimeout,
    console,
  };
  vm.runInContext(THEME_SOURCE, vm.createContext(sandbox), { filename: "charts_theme.js" });
  return { LMCharts: window.LMCharts, observers, windowListeners };
}

function stubEcharts() {
  const registrations = [];
  const inits = [];
  return {
    registrations,
    inits,
    registerTheme(name, theme) {
      registrations.push({ name, theme });
    },
    init(el, theme, options) {
      const chart = {
        disposed: false,
        setOption() {},
        getOption() {
          return {};
        },
        resize() {},
        dispose() {
          this.disposed = true;
        },
      };
      inits.push({ el, theme, options, chart });
      return chart;
    },
  };
}

test("the shared theme turns on ECharts aria and init pins the ZH locale", () => {
  const echarts = stubEcharts();
  const { LMCharts, observers, windowListeners } = loadBridge(echarts);

  const el = {};
  const chart = LMCharts.init(el);
  assert.ok(chart, "init returns a chart when the bundle is present");

  assert.equal(echarts.registrations.length, 1, "theme is registered once");
  assert.equal(echarts.registrations[0].name, LMCharts.theme);
  const theme = echarts.registrations[0].theme;

  // Palette still comes from CSS tokens; these are the stylesheet-less fallbacks.
  assert.deepEqual(Array.from(theme.color), [
    "#5E6AD2",
    "#12a150",
    "#c4841d",
    "#e5484d",
    "#3b82c4",
    "#a1a5ad",
  ]);
  assert.equal(theme.aria.enabled, true, "canvas charts need the aria module on");

  assert.equal(echarts.inits.length, 1);
  assert.equal(echarts.inits[0].el, el);
  assert.equal(echarts.inits[0].theme, LMCharts.theme);
  assert.equal(echarts.inits[0].options.renderer, "canvas");
  assert.equal(echarts.inits[0].options.locale, "ZH");

  // The rebuild and resize plumbing must stay intact.
  assert.equal(observers.length, 1, "theme observer is still wired");
  assert.equal(observers[0].options.attributes, true);
  assert.ok(windowListeners.includes("resize"), "shared resize listener is still wired");
});

test("without the vendored bundle the bridge still degrades quietly", () => {
  const { LMCharts } = loadBridge(undefined);
  assert.equal(LMCharts.theme, "lm");
  assert.equal(LMCharts.ready(), false);
  assert.equal(LMCharts.init({}), null);
  assert.deepEqual(Object.keys(LMCharts).sort(), [
    "dispose",
    "disposeAll",
    "init",
    "palette",
    "ready",
    "refreshTheme",
    "resizeAll",
    "theme",
  ]);
});

test("the real bundle keeps theme aria after a notMerge setOption", () => {
  const echarts = stubEcharts();
  const { LMCharts } = loadBridge(echarts);
  LMCharts.init({});
  const theme = echarts.registrations[0].theme;

  const context = vm.createContext({ console, setTimeout, clearTimeout });
  vm.runInContext(fs.readFileSync(BUNDLE_PATH, "utf8"), context, {
    filename: "echarts.min.js",
  });
  const real = context.echarts;
  assert.equal(real.version, "5.6.0");

  real.registerTheme("lm-probe", theme);
  const chart = real.init(null, "lm-probe", {
    renderer: "svg",
    ssr: true,
    width: 480,
    height: 320,
    locale: "ZH",
  });
  try {
    // dashboard.js:84 replaces the whole option (notMerge=true); the theme-level
    // aria setting has to survive that.
    chart.setOption(
      {
        title: { text: "UT 通过率" },
        xAxis: { type: "category", data: ["一", "二"] },
        yAxis: { type: "value" },
        series: [{ type: "bar", data: [1, 2] }],
      },
      true
    );
    const option = chart.getOption();
    assert.equal(option.aria.enabled, true);
    assert.equal(option.aria.label.enabled, true);
    // Resolved from the bundled ZH locale, proving init's locale reached the
    // aria text rather than falling back to English.
    assert.equal(
      option.aria.label.general.withTitle,
      "这是一个关于“{title}”的图表。"
    );
  } finally {
    chart.dispose();
  }
});