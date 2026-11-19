// Guards keyboard reordering of the field list (frontend modernization).
//
// The grip used to be a non-focusable <span>, so display order could only be
// changed with a pointer. It is now a real <button> whose ArrowUp / ArrowDown /
// Home / End keys move the row, announce the new position through a persistent
// role="status" region, and persist through the existing single commitOrder()
// PATCH path. fields.js is executed against a minimal DOM shim so the
// assertions cover the real handler, not only the source text.
const vm = require("node:vm"),
  fs = require("node:fs"),
  path = require("node:path"),
  assert = require("node:assert/strict"),
  { test } = require("node:test");

const root = path.resolve(__dirname, "../..");
const fieldsJs = fs.readFileSync(path.join(root, "app/static/js/lanmatrix/fields.js"), "utf8");
const fieldsHtml = fs.readFileSync(path.join(root, "app/templates/lanmatrix/fields.html"), "utf8");
const lanmatrixCss = fs.readFileSync(path.join(root, "app/static/css/lanmatrix.css"), "utf8");

function parseSelector(selector) {
  const text = String(selector).trim();
  return {
    tag: (/^[a-zA-Z][\w-]*/.exec(text) || [""])[0].toLowerCase(),
    classes: Array.from(text.matchAll(/\.([\w-]+)/g), (m) => m[1]),
    attrs: Array.from(text.matchAll(/\[([\w-]+)(?:="([^"]*)")?\]/g), (m) => ({
      name: m[1],
      value: m[2],
    })),
  };
}

class ShimElement {
  constructor(tag, doc) {
    this.ownerDocument = doc || null;
    this.tagName = String(tag).toUpperCase();
    this.id = "";
    this.hidden = false;
    this.disabled = false;
    this.checked = false;
    this.value = "";
    this.dataset = {};
    this.children = [];
    this._parent = null;
    this._text = "";
    this._classes = new Set();
    this._draggable = false;
    this.attributes = new Map();
    this.listeners = new Map();
    const classes = this._classes;
    this.classList = {
      add(name) { classes.add(name); },
      remove(name) { classes.delete(name); },
      contains(name) { return classes.has(name); },
      toggle(name, on) {
        if (on === undefined) classes.has(name) ? classes.delete(name) : classes.add(name);
        else if (on) classes.add(name);
        else classes.delete(name);
      },
    };
  }

  get parent() { return this._parent; }
  set parent(node) { this._parent = node; }

  get className() { return Array.from(this._classes).join(" "); }
  set className(value) {
    this._classes.clear();
    for (const name of String(value).split(/\s+/)) if (name) this._classes.add(name);
  }

  get textContent() {
    if (!this.children.length) return this._text;
    return this.children.map((child) => child.textContent).join("");
  }
  set textContent(value) {
    this._text = String(value);
    for (const child of this.children) child._parent = null;
    this.children.length = 0;
  }

  // Mirrors how the browser reflects <button type>; an explicit "button" is
  // what keeps the grip from being treated as a submit control.
  get type() { return this.attributes.get("type") || "submit"; }
  set type(value) { this.setAttribute("type", value); }

  get title() { return this.attributes.get("title") || ""; }
  set title(value) { this.setAttribute("title", value); }

  get draggable() { return this._draggable; }
  set draggable(value) {
    this._draggable = !!value;
    this.setAttribute("draggable", value ? "true" : "false");
  }

  get nextElementSibling() {
    if (!this._parent) return null;
    const list = this._parent.children;
    return list[list.indexOf(this) + 1] || null;
  }

  setAttribute(name, value) { this.attributes.set(name, String(value)); }

  getAttribute(name) {
    if (name.startsWith("data-")) {
      const prop = name.slice(5).replace(/-([a-z])/g, (_, c) => c.toUpperCase());
      if (prop in this.dataset) return String(this.dataset[prop]);
    }
    return this.attributes.has(name) ? this.attributes.get(name) : null;
  }

  removeAttribute(name) { this.attributes.delete(name); }

  addEventListener(type, handler) {
    if (!this.listeners.has(type)) this.listeners.set(type, []);
    this.listeners.get(type).push(handler);
  }

  focus() {
    for (let node = this; node; node = node._parent) if (node.hidden) return;
    if (this.ownerDocument) this.ownerDocument.activeElement = this;
  }

  click() { fire(this, "click"); }

  scrollIntoView() {}
  showModal() {}
  close() {}
  getBoundingClientRect() { return { top: 0, height: 0 }; }

  contains(node) {
    for (let cur = node; cur; cur = cur._parent) if (cur === this) return true;
    return false;
  }

  closest(selector) {
    for (let node = this; node; node = node._parent) if (matches(node, selector)) return node;
    return null;
  }

  querySelector(selector) {
    return descendants(this).find((node) => matches(node, selector)) || null;
  }

  querySelectorAll(selector) {
    return descendants(this).filter((node) => matches(node, selector));
  }

  appendChild(node) {
    if (node._parent) node._parent.removeChild(node);
    node._parent = this;
    this.children.push(node);
    return node;
  }

  append(...nodes) { for (const node of nodes) this.appendChild(node); }

  insertBefore(node, reference) {
    if (!reference) return this.appendChild(node);
    if (node._parent) node._parent.removeChild(node);
    const index = this.children.indexOf(reference);
    if (index < 0) throw new Error("insertBefore: reference is not a child");
    node._parent = this;
    this.children.splice(index, 0, node);
    return node;
  }

  // The browser API fields.js prefers: it moves a node while keeping its focus
  // and state. The shim only has to preserve order.
  moveBefore(node, reference) { return this.insertBefore(node, reference); }

  removeChild(node) {
    const index = this.children.indexOf(node);
    if (index >= 0) this.children.splice(index, 1);
    node._parent = null;
    return node;
  }

  replaceChildren(...nodes) {
    for (const child of this.children) child._parent = null;
    this.children.length = 0;
    for (const node of nodes) this.appendChild(node);
  }
}

function matches(node, selector) {
  if (!node || !(node instanceof ShimElement)) return false;
  const { tag, classes, attrs } = parseSelector(selector);
  if (tag && node.tagName !== tag.toUpperCase()) return false;
  if (classes.some((name) => !node._classes.has(name))) return false;
  for (const attr of attrs) {
    const value = node.getAttribute(attr.name);
    if (value === null) return false;
    if (attr.value !== undefined && value !== attr.value) return false;
  }
  return true;
}

function descendants(node) {
  const found = [];
  const walk = (parent) => {
    for (const child of parent.children) {
      if (child instanceof ShimElement) {
        found.push(child);
        walk(child);
      }
    }
  };
  walk(node);
  return found;
}

function fire(node, type, event) {
  for (const handler of node.listeners.get(type) || []) {
    handler.call(node, Object.assign({ type, target: node }, event || {}));
  }
}

class ShimDocument {
  constructor() {
    this.body = new ShimElement("body", this);
    this.activeElement = null;
    this.byId = new Map();
    this.listeners = new Map();
  }

  createElement(tag) { return new ShimElement(tag, this); }

  createTextNode(text) {
    return { tagName: "#text", textContent: String(text), _parent: null };
  }

  add(node, id) {
    if (id) {
      node.id = id;
      this.byId.set(id, node);
    }
    this.body.appendChild(node);
    return node;
  }

  getElementById(id) { return this.byId.get(id) || null; }

  querySelector(selector) {
    return descendants(this.body).find((node) => matches(node, selector)) || null;
  }

  querySelectorAll(selector) {
    return descendants(this.body).filter((node) => matches(node, selector));
  }

  addEventListener(type, handler) {
    if (!this.listeners.has(type)) this.listeners.set(type, []);
    this.listeners.get(type).push(handler);
  }
}

const settle = async () => {
  for (let i = 0; i < 30; i += 1) await new Promise((resolve) => setImmediate(resolve));
};

function makeField(id, name, order, overrides) {
  return Object.assign({
    id,
    field_key: `f${id}`,
    display_name: name,
    display_order: order,
    is_active: true,
    is_readonly: false,
    is_required: false,
    sheet: "test",
    data_type: "text",
    options: [],
    help_text: "",
  }, overrides || {});
}

async function open(fields) {
  const doc = new ShimDocument();
  const add = (tag, id, props) => {
    const node = new ShimElement(tag, doc);
    Object.assign(node, props || {});
    return doc.add(node, id);
  };

  const page = add("div", "", { className: "lm-fields" });
  page.dataset.projectId = "7";
  add("div", "lm-toast");
  add("tbody", "lm-fields-rows");
  const status = add("span", "lm-fields-status");
  status.setAttribute("role", "status");
  status.setAttribute("aria-live", "polite");
  status.setAttribute("aria-atomic", "true");
  add("dialog", "lm-field-dialog");
  add("button", "lm-add-field");
  add("button", "lm-f-ok");
  add("input", "lm-f-key");
  add("input", "lm-f-name");
  add("input", "lm-f-sheet");
  add("input", "lm-f-type");
  add("input", "lm-f-options");
  add("input", "lm-f-help");
  add("input", "lm-f-required");
  add("div", "lm-f-error");

  const state = { fields: fields.map((field) => ({ ...field })) };
  const api = {
    listCalls: 0,
    patches: [],
    async listFields() {
      this.listCalls += 1;
      return { fields: state.fields.slice().sort((a, b) => a.display_order - b.display_order) };
    },
    async patchField(projectId, id, patch) {
      Object.assign(state.fields.find((field) => field.id === id), patch);
      this.patches.push({ id, ...patch });
      return {};
    },
    async addField() {},
    async deleteField() {},
  };

  const sandbox = {
    document: doc,
    setTimeout: (fn) => { fn(); return 0; },
    clearTimeout: () => {},
    LMApi: api,
    LMUI: { confirm: async () => false },
    console,
  };
  sandbox.window = { setTimeout: sandbox.setTimeout, clearTimeout: sandbox.clearTimeout };
  vm.runInNewContext(fieldsJs, sandbox);
  await settle();

  const rows = doc.getElementById("lm-fields-rows");
  const rowList = () => rows.querySelectorAll("tr[data-id]");
  const ids = () => rowList().map((tr) => Number(tr.dataset.id));
  const rowOf = (id) => rowList().find((tr) => Number(tr.dataset.id) === id) || null;
  const gripOf = (id) => {
    const tr = rowOf(id);
    return tr ? tr.querySelector(".lm-drag-handle") : null;
  };
  const orders = () => state.fields.slice()
    .sort((a, b) => a.display_order - b.display_order)
    .map((field) => field.id);

  return { doc, rows, status, api, state, rowList, ids, rowOf, gripOf, orders };
}

function press(grip, key, modifiers) {
  let prevented = false;
  const event = Object.assign({
    key,
    altKey: false,
    ctrlKey: false,
    metaKey: false,
    preventDefault() { prevented = true; },
  }, modifiers || {});
  for (const handler of grip.listeners.get("keydown") || []) handler(event);
  return prevented;
}

test("fields: the grip is a real button and the live region stays mounted", async () => {
  const fx = await open([makeField(1, "字段A", 0), makeField(2, "字段B", 1)]);

  const grip = fx.gripOf(1);
  assert.equal(grip.tagName, "BUTTON", "the grip must be a focusable button");
  assert.equal(grip.getAttribute("type"), "button", "a bare <button> would submit the dialog form");
  assert.equal(grip.getAttribute("aria-label"), "调整顺序：字段A，按住拖动，或用上下方向键移动");
  assert.doesNotMatch(grip.getAttribute("aria-label"), /⠿/, "the decorative glyph must not be announced");
  assert.equal(grip.getAttribute("title"), "拖拽排序，或用上下方向键移动");

  const glyph = grip.querySelector("span");
  assert.ok(glyph, "the grip still draws its glyph");
  assert.equal(glyph.getAttribute("aria-hidden"), "true");
  assert.equal(glyph.textContent, "⠿");

  // The live region is static markup: text changes, the element never unmounts.
  const tag = fieldsHtml.match(/<span id="lm-fields-status"[^>]*>/);
  assert.ok(tag, "fields.html must mount the reorder live region");
  assert.match(tag[0], /role="status"/);
  assert.match(tag[0], /aria-live="polite"/);
  assert.doesNotMatch(tag[0], /hidden/, "a hidden live region is never announced");
  assert.equal(fx.status.hidden, false);

  // The button reset keeps the shared 22px .grip box (no UA padding/border).
  assert.match(lanmatrixCss, /\.lm-fields \.grip \{[^}]*padding: 0;[^}]*border: 0;[^}]*background: none;/);
  assert.doesNotMatch(lanmatrixCss, /\.lm-fields \.grip \{[^}]*display: none/);
});

test("fields: ArrowUp/Down/Home/End reorder, announce, and persist once per key", async () => {
  const fx = await open([
    makeField(1, "字段A", 0),
    makeField(2, "字段B", 1, { is_active: false }),
    makeField(3, "字段C", 2),
  ]);

  assert.deepEqual(fx.ids(), [1, 2, 3]);
  assert.equal(fx.api.listCalls, 1, "one initial load");

  const step = async (gripId, key, modifiers) => {
    const loadsBefore = fx.api.listCalls;
    const patchesBefore = fx.api.patches.length;
    const prevented = press(fx.gripOf(gripId), key, modifiers);
    await settle();
    return {
      prevented,
      loads: fx.api.listCalls - loadsBefore,
      patches: fx.api.patches.slice(patchesBefore),
    };
  };

  // ArrowDown: one step down, announced with the 1-based position.
  const down = await step(1, "ArrowDown");
  assert.equal(down.prevented, true, "a handled reorder key must not scroll the page");
  assert.deepEqual(fx.ids(), [2, 1, 3]);
  assert.equal(fx.status.textContent, "「字段A」已移动到第 2 位");
  assert.equal(down.loads, 1, "commitOrder persists once per handled key");
  assert.deepEqual(down.patches, [
    { id: 2, display_order: 0 },
    { id: 1, display_order: 1 },
  ]);

  // Focus stays on the moved row's grip after the reload rebuilds the row.
  assert.equal(fx.doc.activeElement, fx.gripOf(1));
  assert.match(fx.gripOf(1).getAttribute("aria-label"), /字段A/);

  // Ends are no-ops: no DOM change, no announcement, no PATCH.
  const atEnd = await step(3, "ArrowDown");
  assert.equal(atEnd.prevented, true);
  assert.deepEqual(fx.ids(), [2, 1, 3]);
  assert.equal(atEnd.loads, 0);
  assert.deepEqual(atEnd.patches, []);
  assert.equal(fx.status.textContent, "「字段A」已移动到第 2 位");

  const atStart = await step(2, "ArrowUp");
  assert.equal(atStart.prevented, true);
  assert.deepEqual(fx.ids(), [2, 1, 3]);
  assert.equal(atStart.loads, 0);
  assert.deepEqual(atStart.patches, []);

  // Home / End jump to the ends.
  const home = await step(1, "Home");
  assert.equal(home.prevented, true);
  assert.deepEqual(fx.ids(), [1, 2, 3]);
  assert.equal(fx.status.textContent, "「字段A」已移动到第 1 位");
  assert.equal(home.loads, 1);

  const end = await step(1, "End");
  assert.equal(end.prevented, true);
  assert.deepEqual(fx.ids(), [2, 3, 1]);
  assert.equal(fx.status.textContent, "「字段A」已移动到第 3 位");
  assert.equal(end.loads, 1);

  // An inactive (row-off) row reorders like any other: order is table-global.
  assert.equal(fx.rowOf(2).classList.contains("row-off"), true);
  const offEnd = await step(2, "End");
  assert.equal(offEnd.prevented, true);
  assert.deepEqual(fx.ids(), [3, 1, 2]);
  assert.equal(fx.status.textContent, "「字段B」已移动到第 3 位");
  assert.equal(offEnd.loads, 1);
  assert.equal(fx.rowOf(2).classList.contains("row-off"), true, "the row keeps its inactive state");
  assert.deepEqual(fx.orders(), [3, 1, 2], "display_order on the server follows the DOM");

  // Unrelated keys and modifier combinations are left to the browser.
  const tab = await step(3, "Tab");
  assert.equal(tab.prevented, false);
  assert.equal(tab.loads, 0);
  const ctrlDown = await step(3, "ArrowDown", { ctrlKey: true });
  assert.equal(ctrlDown.prevented, false);
  assert.equal(ctrlDown.loads, 0);
  assert.deepEqual(fx.ids(), [3, 1, 2]);

  // The pointer path is untouched: mousedown on the same handle arms the row.
  const grip = fx.gripOf(3);
  for (const handler of fx.rows.listeners.get("mousedown") || []) handler({ target: grip });
  assert.equal(fx.rowOf(3).draggable, true);
});