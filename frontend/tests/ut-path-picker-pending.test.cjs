const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const source = fs.readFileSync(path.resolve(__dirname, "../../app/static/js/lanmatrix/ut_path_picker.js"), "utf8");
const settle = () => new Promise(resolve => setImmediate(resolve));
const entry = (type, path) => ({ type, path, name: path.split("/").pop() });
const listing = (path, entries = [], selected = null) => ({ path, parent: path ? "/" : null, entries, selected });

function harness() {
  let focused;
  class Element extends EventTarget {
    attributes = new Map();
    children = [];
    dataset = {};
    className = "";
    classList = {
      contains: name => this.className.split(" ").includes(name),
      toggle: (name, on) => { this.className = on ? name : ""; },
    };
    setAttribute(name, value) { this.attributes.set(name, value); }
    getAttribute(name) { return this.attributes.get(name) ?? null; }
    removeAttribute(name) { this.attributes.delete(name); }
    appendChild(child) { this.children.push(child); child.parentElement = this; }
    replaceChildren() {
      this.children.forEach(child => { child.parentElement = null; });
      this.children = [];
    }
    closest(selector) {
      if (selector.slice(1).split(".").every(name => this.classList.contains(name))) return this;
      return this.parentElement?.closest(selector) ?? null;
    }
    querySelectorAll(selector) { return this.children.filter(child => child.closest(selector) === child); }
    focus() { focused = this; }
    showModal() { this.open = true; }
    close() { this.open = false; }
    emit(type, target = this, props = {}) {
      const event = new Event(type, { cancelable: true });
      Object.defineProperty(event, "target", { value: target });
      Object.assign(event, props);
      this.dispatchEvent(event);
      return event;
    }
  }
  const nodes = Object.fromEntries(
    ["dialog", "list", "status", "location", "up", "choose", "title", "help", "go", "cancel"]
      .map(name => [name, new Element()]),
  );
  const trigger = new Element();
  trigger.focus();
  const requests = [], window = {};
  vm.runInNewContext(source, {
    window, AbortController,
    document: {
      get activeElement() { return focused; },
      getElementById: id => nodes[id.replace(/^ut-path-/, "")],
      createElement: () => new Element(),
    },
    LMApi: {
      utServerPaths(pid, kind, path) {
        return new Promise((resolve, reject) => { requests.push({ pid, kind, path, resolve, reject }); });
      },
    },
  });
  function go(path, type = "click", props = {}) {
    nodes.location.value = path;
    return type === "keydown"
      ? nodes.location.emit(type, nodes.location, { key: "Enter", ...props })
      : nodes.go.emit(type);
  }
  return { ...nodes, trigger, requests, navigate: go, focused: () => focused,
    open: (kind, value) => window.LMUtPathPicker.open({ pid: "4", kind, value }) };
}

test("Choose cannot commit the confirmed directory while another path is pending", async () => {
  const h = harness();
  const picked = h.open("directory", "/confirmed");
  assert.equal(h.focused(), h.list);
  h.requests[0].resolve(listing("/confirmed"));
  await settle();
  assert.equal(h.choose.disabled, false);

  h.navigate(" /next ");
  assert.equal(h.requests[1].path, "/next");
  assert.equal(h.list.getAttribute("aria-busy"), "true");
  h.choose.emit("click");
  assert.equal(h.dialog.open, true);
  assert.equal(h.choose.disabled, true);

  h.requests[1].resolve(listing("/next"));
  await settle();
  assert.equal(h.list.getAttribute("aria-busy"), null);
  assert.equal(h.choose.disabled, false);
  h.choose.emit("click");
  assert.equal(await picked, "/next");
  assert.equal(h.focused(), h.trigger);
});

test("stale CSV rows cannot select, double-click, or navigate during a request", async () => {
  const h = harness();
  const picked = h.open("csv", "/confirmed/selected.csv");
  h.requests[0].resolve(listing("/confirmed", [
    entry("file", "/confirmed/selected.csv"), entry("file", "/confirmed/other.csv"),
    entry("directory", "/confirmed/folder"),
  ], "/confirmed/selected.csv"));
  await settle();
  const [selected, other, folder] = h.list.children;

  h.navigate("/superseded");
  h.navigate("/missing");
  h.list.emit("dblclick", other.children[0]);
  assert.equal(h.dialog.open, true);
  h.choose.emit("click");
  assert.equal(h.dialog.open, true);
  h.list.emit("click", other);
  h.list.emit("click", folder);
  assert.equal(h.requests.length, 3);
  assert.equal(h.list.getAttribute("aria-disabled"), "true");
  assert.equal(h.status.textContent, "正在读取…");
  assert.equal(selected.getAttribute("aria-selected"), "true");
  assert.equal(other.getAttribute("aria-selected"), "false");

  h.requests[2].reject(new Error("Path unavailable"));
  await settle();
  assert.equal(h.list.getAttribute("aria-busy"), null);
  h.requests[1].resolve(listing("/superseded"));
  await settle();
  assert.equal(h.status.textContent, "Path unavailable");
  assert.equal(h.status.classList.contains("is-error"), true);
  assert.equal(h.location.value, "/confirmed");
  assert.equal(h.list.getAttribute("aria-busy"), null);
  assert.equal(h.list.getAttribute("aria-disabled"), null);
  assert.equal(h.list.children[0], selected);
  assert.equal(h.choose.disabled, false);
  h.choose.emit("click");
  assert.equal(await picked, "/confirmed/selected.csv");
});

test("Go, Enter and Up can supersede requests, and only the latest response updates the picker", async () => {
  const h = harness();
  const picked = h.open("csv", "/confirmed");
  h.requests[0].resolve(listing("/confirmed", [entry("file", "/confirmed/old.csv")], "/confirmed/old.csv"));
  await settle();

  h.navigate("/first");
  assert.equal(h.navigate("/composing", "keydown", { isComposing: true }).defaultPrevented, false);
  assert.equal(h.navigate("/composing", "keydown", { keyCode: 229 }).defaultPrevented, false);
  assert.equal(h.requests.length, 2);
  assert.equal(h.up.disabled, false);
  h.up.emit("click");
  assert.equal(h.navigate(" /last ", "keydown").defaultPrevented, true);
  assert.deepEqual(h.requests.map(request => request.path), ["/confirmed", "/first", "/", "/last"]);

  h.requests[2].resolve(listing("/"));
  await settle();
  assert.equal(h.list.getAttribute("aria-busy"), "true");
  assert.equal(h.choose.disabled, true);
  assert.equal(h.list.children[0].dataset.path, "/confirmed/old.csv");

  h.requests[3].resolve(listing("/last", [entry("file", "/last/new.csv")]));
  await settle();
  h.requests[1].reject(new Error("Stale error"));
  await settle();
  assert.equal(h.location.value, "/last");
  assert.equal(h.list.getAttribute("aria-busy"), null);
  assert.equal(h.status.textContent, "请选择一个 CSV 文件。");
  assert.equal(h.choose.disabled, true);
  h.list.emit("dblclick", h.list.children[0]);
  assert.equal(await picked, "/last/new.csv");
});

test("the rejected initial path opens the root and keeps the original error", async () => {
  const h = harness();
  const picked = h.open("directory", "relative");
  h.requests[0].reject(new Error("Use an absolute path"));
  await settle();
  assert.equal(h.requests[1].path, "");
  assert.equal(h.list.getAttribute("aria-busy"), "true");
  assert.equal(h.choose.disabled, true);

  h.requests[1].resolve(listing("", [entry("directory", "/workspace")]));
  await settle();
  assert.equal(h.status.textContent, "Use an absolute path");
  assert.equal(h.list.getAttribute("aria-busy"), null);
  assert.equal(h.choose.disabled, true);
  h.list.emit("click", h.list.children[0]);
  assert.equal(h.requests[2].path, "/workspace");
  h.requests[2].resolve(listing("/workspace"));
  await settle();
  h.choose.emit("click");
  assert.equal(await picked, "/workspace");
});

test("a superseded root fallback cannot replace a newer navigation's status", async () => {
  const h = harness();
  const picked = h.open("directory", "relative");
  h.requests[0].reject(new Error("Use an absolute path"));
  await settle();
  h.navigate("/workspace");
  h.requests[2].resolve(listing("/workspace"));
  await settle();
  h.requests[1].reject(new Error("Stale root error"));
  await settle();
  assert.equal(h.status.textContent, "将选择：/workspace");
  assert.equal(h.choose.disabled, false);
  h.choose.emit("click");
  assert.equal(await picked, "/workspace");
});

test("Escape cancels pending navigation and late replies cannot affect a reopened picker", async () => {
  const h = harness();
  const first = h.open("directory", "relative");
  h.requests[0].reject(new Error("Use an absolute path"));
  await settle();
  assert.equal(h.dialog.emit("cancel").defaultPrevented, true);
  assert.equal(await first, null);
  assert.equal(h.focused(), h.trigger);
  assert.equal(h.list.getAttribute("aria-busy"), null);

  const second = h.open("directory", "/new");
  h.requests[1].resolve(listing("", [entry("directory", "/stale")]));
  await settle();
  assert.equal(h.status.textContent, "正在读取…");
  assert.equal(h.list.getAttribute("aria-busy"), "true");
  assert.equal(h.list.children.length, 0);
  h.requests[2].resolve(listing("/new"));
  await settle();
  h.choose.emit("click");
  assert.equal(await second, "/new");
});
