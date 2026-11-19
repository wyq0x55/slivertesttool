/* The pager's page indicator is a live region (role="status"), which only
 * announces text changes made to a node that is already in the DOM. This test
 * loads the real pager.js through a hand-written DOM shim (the same approach as
 * workspace-recent.test.cjs, which builds its fake elements in a vm context) and
 * records live-region updates so the two behaviours that matter are asserted
 * directly: one persistent node survives every render, and the first render
 * produces no live update.
 */
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const pagerPath = path.resolve(__dirname, "../../app/static/js/lanmatrix/pager.js");

function parseAttrs(source) {
  const attrs = {};
  const re = /([\w:-]+)(?:="([^"]*)")?/g;
  let m;
  while ((m = re.exec(source))) attrs[m[1]] = m[2] === undefined ? "" : m[2];
  return attrs;
}

function parseHtml(html, ownerDocument) {
  const root = [];
  const stack = [{ children: root }];
  const re = /<(\/?)([a-zA-Z0-9-]+)([^>]*?)(\/?)>|([^<]+)/g;
  let m;
  while ((m = re.exec(html))) {
    if (m[5] !== undefined) {
      if (m[5]) stack[stack.length - 1].children.push(new TextNode(ownerDocument, m[5]));
      continue;
    }
    if (m[1] === "/") { stack.pop(); continue; }
    const el = new El(ownerDocument, m[2].toLowerCase(), parseAttrs(m[3]));
    stack[stack.length - 1].children.push(el);
    if (!m[4]) stack.push(el);
  }
  return root;
}

class TextNode {
  constructor(ownerDocument, value) {
    this.ownerDocument = ownerDocument;
    this.value = value;
    this.parent = null;
  }
  get textContent() { return this.value; }
  connect(parent) { this.parent = parent; }
}

class El {
  constructor(ownerDocument, tagName, attrs) {
    this.ownerDocument = ownerDocument;
    this.tagName = tagName.toUpperCase();
    this.attrs = attrs || {};
    this.children = [];
    this.parent = null;
    this.hidden = false;
    this._disabled = Object.prototype.hasOwnProperty.call(this.attrs, "disabled");
    this._listeners = {};
  }
  get classList() {
    const el = this;
    return {
      add(name) {
        const names = new Set((el.attrs.class || "").split(/\s+/).filter(Boolean));
        names.add(name);
        el.attrs.class = Array.from(names).join(" ");
      },
      contains(name) {
        return (el.attrs.class || "").split(/\s+/).filter(Boolean).includes(name);
      },
    };
  }
  getAttribute(name) { return Object.prototype.hasOwnProperty.call(this.attrs, name) ? this.attrs[name] : null; }
  get disabled() { return this._disabled; }
  set disabled(value) {
    this._disabled = Boolean(value);
    if (this._disabled) this.attrs.disabled = "";
    else delete this.attrs.disabled;
  }
  get dataset() {
    const data = {};
    for (const [key, value] of Object.entries(this.attrs)) {
      if (!key.startsWith("data-")) continue;
      const name = key.slice(5).replace(/-([a-z])/g, (_, c) => c.toUpperCase());
      data[name] = value;
    }
    return data;
  }
  get textContent() { return this.children.map((child) => child.textContent).join(""); }
  set textContent(value) {
    this.children = [];
    if (value) {
      const text = new TextNode(this.ownerDocument, String(value));
      text.connect(this);
      this.children.push(text);
    }
    if (this.isConnected()) {
      const region = this.liveRegion();
      if (region) this.ownerDocument.announce(region, this.textContent);
    }
  }
  liveRegion() {
    let node = this;
    while (node) {
      if (node.attrs && (node.attrs["aria-live"] || node.attrs.role === "status")) return node;
      node = node.parent;
    }
    return null;
  }
  isConnected() {
    let node = this;
    while (node) {
      if (node === this.ownerDocument.host) return true;
      node = node.parent;
    }
    return false;
  }
  connect(parent) {
    this.parent = parent;
    for (const child of this.children) child.connect(this);
  }
  set innerHTML(html) {
    this.children = parseHtml(String(html), this.ownerDocument);
    for (const child of this.children) child.connect(this);
  }
  get innerHTML() {
    return this.children.map((child) => {
      if (child instanceof TextNode) return child.textContent;
      const attrs = Object.entries(child.attrs)
        .map(([k, v]) => (v === "" ? ` ${k}` : ` ${k}="${v}"`))
        .join("");
      return `<${child.tagName.toLowerCase()}${attrs}>${child.innerHTML}</${child.tagName.toLowerCase()}>`;
    }).join("");
  }
  replaceChildren() { this.children = []; }
  querySelector(selector) {
    let matches;
    const attr = /^\[([\w-]+)(?:="([^"]*)")?\]$/.exec(selector);
    if (attr) {
      matches = (el) => (attr[2] === undefined
        ? Object.prototype.hasOwnProperty.call(el.attrs, attr[1])
        : el.attrs[attr[1]] === attr[2]);
    } else if (selector.startsWith(".")) {
      const cls = selector.slice(1);
      matches = (el) => (el.attrs.class || "").split(/\s+/).filter(Boolean).includes(cls);
    } else {
      throw new Error(`pager test shim cannot query ${selector}`);
    }
    const walk = (el) => {
      for (const child of el.children) {
        if (!(child instanceof El)) continue;
        if (matches(child)) return child;
        const found = walk(child);
        if (found) return found;
      }
      return null;
    };
    return walk(this);
  }
  closest(selector) {
    const attr = /^\[([\w-]+)\]$/.exec(selector);
    if (!attr) throw new Error(`pager test shim cannot match ${selector}`);
    let node = this;
    while (node) {
      if (node.attrs && Object.prototype.hasOwnProperty.call(node.attrs, attr[1])) return node;
      node = node.parent;
    }
    return null;
  }
  addEventListener(type, fn) {
    (this._listeners[type] = this._listeners[type] || []).push(fn);
  }
  contains(node) {
    for (let cur = node; cur; cur = cur.parent) if (cur === this) return true;
    return false;
  }
  focus() { this.ownerDocument.activeElement = this; }
  blur() { if (this.ownerDocument.activeElement === this) this.ownerDocument.activeElement = null; }
}

function setup() {
  const ownerDocument = {
    host: null,
    activeElement: null,
    announcements: [],
    announce(region, text) { this.announcements.push({ region, text }); },
  };
  const host = new El(ownerDocument, "div", {});
  ownerDocument.host = host;
  const window = {};
  vm.runInNewContext(fs.readFileSync(pagerPath, "utf8"), { window, document: ownerDocument });
  return {
    window,
    host,
    document: ownerDocument,
    announcements: ownerDocument.announcements,
    click(target) {
      for (const fn of host._listeners.click || []) fn({ type: "click", target });
    },
  };
}

function textOf(node) {
  return node.textContent.replace(/\s+/g, " ").trim();
}

test("prev/next announce the new page through one persistent live region", () => {
  const dom = setup();
  const changes = [];
  const pager = dom.window.LMPager.create({
    host: dom.host, pageSize: 20, onChange: (page) => changes.push(page),
  });

  pager.setTotal(61);
  assert.equal(dom.host.hidden, false);
  assert.equal(textOf(dom.host.querySelector(".lm-pager-info")), "第 1 / 4 页 · 1–20 / 61 条");

  const status = dom.host.querySelector("[data-pager-status]");
  assert.ok(status, "the page number is a live status node");
  assert.equal(status.getAttribute("role"), "status");
  assert.equal(status.getAttribute("aria-live"), "polite");
  assert.equal(status.getAttribute("aria-atomic"), "true");
  assert.equal(textOf(status), "第 1 / 4 页");
  assert.equal(dom.announcements.length, 0, "the first render is silent");

  dom.click(dom.host.querySelector('[data-pg="next"]'));
  assert.equal(pager.page(), 2);
  assert.equal(pager.offset(), 20);
  assert.equal(dom.host.querySelector("[data-pager-status]"), status, "same node, not a replacement");
  assert.equal(textOf(status), "第 2 / 4 页");
  assert.deepEqual(dom.announcements.map((a) => a.text), ["第 2 / 4 页"]);
  assert.equal(changes.length, 1);
  assert.equal(changes[0], 2);

  dom.click(dom.host.querySelector('[data-pg="next"]'));
  assert.equal(dom.host.querySelector("[data-pager-status]"), status);
  assert.equal(textOf(status), "第 3 / 4 页");
  assert.deepEqual(dom.announcements.map((a) => a.text), ["第 2 / 4 页", "第 3 / 4 页"]);

  dom.click(dom.host.querySelector('[data-pg="prev"]'));
  assert.equal(dom.host.querySelector("[data-pager-status]"), status);
  assert.equal(textOf(status), "第 2 / 4 页");
  assert.deepEqual(dom.announcements.map((a) => a.text),
    ["第 2 / 4 页", "第 3 / 4 页", "第 2 / 4 页"]);
});

test("reset and setTotal update the same live node only when the page moves", () => {
  const dom = setup();
  const pager = dom.window.LMPager.create({ host: dom.host, pageSize: 20 });
  pager.setTotal(41);
  const status = dom.host.querySelector("[data-pager-status]");

  dom.click(dom.host.querySelector('[data-pg="next"]'));
  dom.click(dom.host.querySelector('[data-pg="next"]'));
  assert.equal(pager.page(), 3);
  assert.equal(dom.announcements.length, 2);

  pager.reset();
  assert.equal(pager.page(), 1);
  assert.equal(dom.host.querySelector("[data-pager-status]"), status);
  assert.equal(textOf(status), "第 1 / 3 页");
  assert.equal(dom.announcements.length, 3);

  pager.reset();
  assert.equal(dom.announcements.length, 3, "resetting on page 1 changes nothing");

  pager.setTotal(45);
  assert.equal(pager.page(), 1);
  assert.equal(dom.announcements.length, 3, "the range changed, not the page");
  assert.equal(textOf(dom.host.querySelector(".lm-pager-info")), "第 1 / 3 页 · 1–20 / 45 条");

  pager.setTotal(45);
  assert.equal(dom.announcements.length, 3, "a poll with unchanged totals stays quiet");

  dom.click(dom.host.querySelector('[data-pg="next"]'));
  assert.equal(pager.page(), 2);
  assert.equal(dom.host.querySelector("[data-pager-status]"), status);
  assert.equal(textOf(status), "第 2 / 3 页");
  assert.equal(dom.host.querySelector('[data-pg="next"]').disabled, false);
  assert.equal(dom.host.querySelector('[data-pg="prev"]').disabled, false);
  assert.equal(dom.announcements.at(-1).text, "第 2 / 3 页");
});

test("a one-page list hides the control, stays silent, and slice/offset keep their contract", () => {
  const dom = setup();
  const pager = dom.window.LMPager.create({ host: dom.host, pageSize: 20 });

  pager.setTotal(20);
  assert.equal(dom.host.hidden, true);
  assert.equal(dom.announcements.length, 0);

  pager.setTotal(60);
  assert.equal(dom.host.hidden, false);
  const status = dom.host.querySelector("[data-pager-status]");
  assert.equal(dom.announcements.length, 0, "the first visible render is still initial content");
  assert.equal(textOf(status), "第 1 / 3 页");

  const rows = Array.from({ length: 60 }, (_, i) => i);
  assert.deepEqual(pager.slice(rows), rows.slice(0, 20));
  assert.equal(pager.offset(), 0);
  dom.click(dom.host.querySelector('[data-pg="next"]'));
  assert.deepEqual(pager.slice(rows), rows.slice(20, 40));
  assert.equal(pager.offset(), 20);

  pager.setTotal(20);
  assert.equal(dom.host.hidden, true);
  assert.equal(dom.host.querySelector("[data-pager-status]"), status, "the live node survives hiding");
  assert.equal(pager.pageSize(), 20);
});

test("collapsing to one page moves focus out of the hidden control", () => {
  const dom = setup();
  const pager = dom.window.LMPager.create({ host: dom.host, pageSize: 20 });
  pager.setTotal(60);
  const next = dom.host.querySelector('[data-pg="next"]');
  next.focus();
  assert.equal(dom.document.activeElement, next);

  pager.setTotal(20);

  assert.equal(dom.host.hidden, true);
  assert.equal(dom.document.activeElement, null, "focus does not stay in hidden content");
});