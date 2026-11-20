const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const rootDir = path.resolve(__dirname, "../..");
const adminDbPath = path.join(rootDir, "app/static/js/lanmatrix/admin_db.js");
const cssPath = path.join(rootDir, "app/static/css/lanmatrix.css");

// --- Minimal DOM stub -------------------------------------------------------
// The console script only needs elements, attributes, class lists, listeners
// and innerHTML made of <th>/<button> cells. Parsing just those tags exercises
// the real sort/refresh code without adding a DOM dependency to frontend/.
const allElements = new Set();
const byId = new Map();
let documentApi = null;

function hasClass(el, name) {
  return (el.getAttribute("class") || "").split(/\s+/).filter(Boolean).includes(name);
}

function register(el) {
  allElements.add(el);
  if (el.id) byId.set(el.id, el);
}

function unregisterTree(el) {
  allElements.delete(el);
  if (el.id && byId.get(el.id) === el) byId.delete(el.id);
  el.children.forEach(unregisterTree);
}

function attrPairs(source) {
  const attrs = {};
  const re = /([\w-]+)(?:=(?:"([^"]*)"|'([^']*)'))?/g;
  let match;
  while ((match = re.exec(source))) {
    attrs[match[1]] = match[2] !== undefined ? match[2]
      : (match[3] !== undefined ? match[3] : "");
  }
  return attrs;
}

function matches(el, selector) {
  const s = selector.trim();
  if (s.startsWith(".")) return hasClass(el, s.slice(1));
  if (s.startsWith("#")) return el.id === s.slice(1);
  let m = s.match(/^\[([\w-]+)(?:="([^"]*)")?\]$/);
  if (m) {
    return m[2] === undefined ? el.hasAttribute(m[1]) : el.getAttribute(m[1]) === m[2];
  }
  m = s.match(/^([a-z][\w-]*)\[([\w-]+)(?:="([^"]*)")?\]$/i);
  if (m) {
    return el.tagName === m[1].toUpperCase()
      && (m[3] === undefined ? el.hasAttribute(m[2]) : el.getAttribute(m[2]) === m[3]);
  }
  return el.tagName === s.toUpperCase();
}

class StubElement {
  constructor(tag, attrs = {}) {
    this.tagName = String(tag).toUpperCase();
    this.attrs = new Map(Object.entries(attrs).map(([k, v]) => [k, String(v)]));
    this.children = [];
    this.parentElement = null;
    this.listeners = new Map();
    this.textContent = "";
    this.hidden = false;
    this.disabled = false;
    this.value = "";
    this.checked = false;
    this._innerHTML = "";
  }

  get id() { return this.attrs.get("id") || ""; }
  get className() { return this.attrs.get("class") || ""; }
  set className(value) { this.attrs.set("class", String(value)); }
  get firstElementChild() { return this.children[0] || null; }

  get classList() {
    const el = this;
    const list = () => (el.getAttribute("class") || "").split(/\s+/).filter(Boolean);
    return {
      contains: (name) => list().includes(name),
      add(name) {
        const names = list();
        if (!names.includes(name)) names.push(name);
        el.attrs.set("class", names.join(" "));
      },
      remove(name) { el.attrs.set("class", list().filter((n) => n !== name).join(" ")); },
      toggle(name, force) {
        const on = force === undefined ? !list().includes(name) : !!force;
        if (on) this.add(name); else this.remove(name);
        return on;
      },
    };
  }

  get dataset() {
    const out = {};
    for (const [key, value] of this.attrs) {
      if (key.startsWith("data-")) {
        out[key.slice(5).replace(/-([a-z])/g, (_, ch) => ch.toUpperCase())] = value;
      }
    }
    return out;
  }

  get innerHTML() { return this._innerHTML; }
  set innerHTML(html) {
    this._innerHTML = String(html);
    this.children.forEach(unregisterTree);
    this.children = [];
    for (const node of parseFragment(this._innerHTML)) this.appendChild(node);
  }

  getAttribute(name) { return this.attrs.has(name) ? this.attrs.get(name) : null; }
  setAttribute(name, value) { this.attrs.set(name, String(value)); }
  removeAttribute(name) { this.attrs.delete(name); }
  hasAttribute(name) { return this.attrs.has(name); }

  addEventListener(type, fn) {
    if (!this.listeners.has(type)) this.listeners.set(type, []);
    this.listeners.get(type).push(fn);
  }

  dispatchEvent(event) {
    const type = typeof event === "string" ? event : event.type;
    for (const fn of this.listeners.get(type) || []) fn.call(this, event);
    return true;
  }

  appendChild(child) {
    child.parentElement = this;
    this.children.push(child);
    register(child);
    return child;
  }

  replaceChildren() {
    this.children.forEach(unregisterTree);
    this.children = [];
  }

  querySelectorAll(selector) {
    const out = [];
    const walk = (el) => {
      for (const child of el.children) {
        if (matches(child, selector)) out.push(child);
        walk(child);
      }
    };
    walk(this);
    return out;
  }

  querySelector(selector) { return this.querySelectorAll(selector)[0] || null; }

  contains(el) {
    for (let node = el; node; node = node.parentElement) {
      if (node === this) return true;
    }
    return false;
  }

  focus() { if (documentApi) documentApi.activeElement = this; }
  showModal() {}
  close() {}
}

function parseFragment(html) {
  const nodes = [];
  const thRanges = [];
  const thRe = /<th\b([^>]*)>([\s\S]*?)<\/th>/gi;
  let thMatch;
  while ((thMatch = thRe.exec(html))) {
    const th = new StubElement("th", attrPairs(thMatch[1]));
    register(th);
    thRanges.push({ start: thMatch.index, end: thMatch.index + thMatch[0].length, th });
    nodes.push({ start: thMatch.index, el: th });
  }
  const btnRe = /<button\b([^>]*)>[\s\S]*?<\/button>/gi;
  let btnMatch;
  while ((btnMatch = btnRe.exec(html))) {
    const btn = new StubElement("button", attrPairs(btnMatch[1]));
    register(btn);
    const owner = thRanges.find((r) => btnMatch.index >= r.start && btnMatch.index < r.end);
    if (owner) {
      btn.parentElement = owner.th;
      owner.th.children.push(btn);
    } else {
      nodes.push({ start: btnMatch.index, el: btn });
    }
  }
  return nodes.sort((a, b) => a.start - b.start).map((n) => n.el);
}

const root = new StubElement("div", { class: "lm-dbadmin" });
const tabList = new StubElement("div", { role: "tablist" });
root.querySelector = (selector) => (selector === '[role="tablist"]' ? tabList : null);
root.querySelectorAll = (selector) => (selector === ".lm-db-pane" ? [] : []);
register(root);
register(tabList);

const document = {
  activeElement: null,
  getElementById(id) {
    if (byId.has(id)) return byId.get(id);
    const el = new StubElement("div", { id });
    register(el);
    return el;
  },
  createElement(tag) { const el = new StubElement(tag); register(el); return el; },
  querySelector(selector) { return selector === ".lm-dbadmin" ? root : null; },
  querySelectorAll(selector) { return [...allElements].filter((el) => matches(el, selector)); },
};
documentApi = document;

let readyCallback = null;
const window = {
  LMReady: { then(fn) { readyCallback = fn; } },
};

const rowCalls = [];
const schemas = {
  users: { columns: ["id", "name", "email"], primary_key: ["id"] },
  orders: { columns: ["id", "total"], primary_key: ["id"] },
};
const LMApi = {
  async dbOverview() { return { tables: [] }; },
  async dbTables() {
    return { tables: [{ name: "users", est_rows: 3 }, { name: "orders", est_rows: 2 }] };
  },
  async dbTableSchema(name) { return schemas[name]; },
  async dbTableRows(table, opts) {
    rowCalls.push({
      table, page: opts.page, page_size: opts.page_size,
      order_by: opts.order_by, desc: opts.desc,
    });
    const schema = schemas[table];
    return {
      columns: schema.columns,
      primary_key: schema.primary_key,
      rows: [[1, "alice", "a@example.test"]].slice(0, schema.columns.length),
      total: 150,
      pages: 3,
      page: opts.page,
    };
  },
};

const context = vm.createContext({
  window, document, LMApi, URLSearchParams, setTimeout, clearTimeout,
});
vm.runInContext(fs.readFileSync(adminDbPath, "utf8"), context, { filename: adminDbPath });

async function flush(rounds = 4) {
  for (let i = 0; i < rounds; i += 1) {
    await new Promise((resolve) => setImmediate(resolve));
  }
}

function lastRowCall() { return rowCalls[rowCalls.length - 1]; }

test("sorting uses native header buttons, keeps header DOM and focus", async () => {
  assert.equal(typeof readyCallback, "function");
  readyCallback();
  await flush();

  const navButtons = document.querySelectorAll(".lm-db-tnav-btn");
  assert.equal(navButtons.length, 2, "both tables should be listed");
  navButtons[0].dispatchEvent({ type: "click" });
  await flush();

  const head = document.getElementById("lm-db-grid-head");
  const sortableThs = head.querySelectorAll("th[data-c]");
  const opThs = head.querySelectorAll("th").filter((th) => !th.hasAttribute("data-c"));
  const buttons = head.querySelectorAll(".lm-db-sort-btn");
  assert.equal(sortableThs.length, 3, "one header cell per data column");
  assert.equal(buttons.length, 3, "each sortable header owns one button");
  assert.equal(opThs.length, 1, "the operation column is not sortable");

  for (const th of sortableThs) {
    assert.equal(th.getAttribute("scope"), "col", "header cells declare their scope");
    assert.equal(th.getAttribute("aria-sort"), null, "no aria-sort before a sort is chosen");
    const button = th.querySelector(".lm-db-sort-btn");
    assert.ok(button, "sortable header cell contains the interactive control");
    assert.equal(button.getAttribute("type"), "button", "control is a native button");
    assert.equal(button.parentElement, th, "button lives inside the <th>, not the other way round");
  }
  assert.equal(opThs[0].querySelector(".lm-db-sort-btn"), null, "non-sortable header has no button");
  assert.match(head.innerHTML, /<th scope="col" class="lm-db-opcol">操作<\/th>/, "operation header is plain text");
  assert.match(head.innerHTML, /aria-hidden="true"/, "the direction icon is decorative");
  assert.match(head.innerHTML, /id 🔑/, "primary-key decoration is preserved");

  // Paging still belongs to the server, and sorting resets to page 1.
  const pager = document.getElementById("lm-db-tv-pager");
  assert.equal(pager.children.length, 3, "pager has previous/next controls");
  pager.children[2].dispatchEvent({ type: "click" });
  await flush();
  assert.equal(lastRowCall().page, 2, "next page is requested from the server");

  const firstButton = buttons[0];
  const firstTh = sortableThs[0];
  firstButton.focus();
  assert.equal(document.activeElement, firstButton);

  firstButton.dispatchEvent({ type: "click" });
  await flush();
  assert.deepEqual(lastRowCall(), {
    table: "users", page: 1, page_size: 50, order_by: "id", desc: "",
  }, "first activation requests ascending order on page 1");
  assert.equal(firstTh.getAttribute("aria-sort"), "ascending");
  for (const th of sortableThs.filter((candidate) => candidate !== firstTh)) {
    assert.equal(th.getAttribute("aria-sort"), null, "only the sorted header carries aria-sort");
  }
  assert.equal(document.activeElement, firstButton, "focus survives the async row refresh");
  assert.equal(head.querySelectorAll(".lm-db-sort-btn")[0], firstButton,
    "the header DOM is reused so the focused button is the same element");

  firstButton.dispatchEvent({ type: "click" });
  await flush();
  assert.deepEqual(lastRowCall(), {
    table: "users", page: 1, page_size: 50, order_by: "id", desc: "1",
  }, "activating the sorted column again flips it to descending");
  assert.equal(firstTh.getAttribute("aria-sort"), "descending");
  assert.equal(head.querySelectorAll(".lm-db-sort-btn")[0], firstButton);

  firstButton.dispatchEvent({ type: "click" });
  await flush();
  assert.equal(lastRowCall().desc, "", "a third activation returns to ascending");
  assert.equal(firstTh.getAttribute("aria-sort"), "ascending");

  // Switching columns starts ascending again and moves aria-sort.
  const secondButton = head.querySelectorAll(".lm-db-sort-btn")[1];
  const secondTh = head.querySelectorAll("th[data-c]")[1];
  secondButton.focus();
  secondButton.dispatchEvent({ type: "click" });
  await flush();
  assert.deepEqual(lastRowCall(), {
    table: "users", page: 1, page_size: 50, order_by: "name", desc: "",
  }, "a new column starts ascending");
  assert.equal(secondTh.getAttribute("aria-sort"), "ascending");
  assert.equal(firstTh.getAttribute("aria-sort"), null, "aria-sort leaves the previous column");
  assert.equal(head.querySelectorAll("th[aria-sort]").length, 1);
  assert.equal(head.querySelectorAll(".lm-db-sort-btn")[1], secondButton);

  // A refresh does not steal focus the user already moved away.
  const sqlBox = document.getElementById("lm-db-sql");
  document.activeElement = sqlBox;
  document.getElementById("lm-db-refresh").dispatchEvent({ type: "click" });
  await flush();
  assert.equal(document.activeElement, sqlBox, "refresh leaves a moved focus alone");
  assert.equal(lastRowCall().order_by, "name");
  assert.equal(head.querySelectorAll(".lm-db-sort-btn")[1], secondButton,
    "same-table refresh retains the header DOM");

  // A different table/column set rebuilds the header and clears sort state.
  document.querySelectorAll(".lm-db-tnav-btn")[1].dispatchEvent({ type: "click" });
  await flush();
  const rebuiltButtons = head.querySelectorAll(".lm-db-sort-btn");
  assert.equal(rebuiltButtons.length, 2, "orders has two sortable columns");
  assert.notEqual(rebuiltButtons[0], firstButton, "changed columns replace the header DOM");
  assert.equal(head.querySelectorAll("th[aria-sort]").length, 0,
    "switching tables clears aria-sort");
  assert.equal(rebuiltButtons[0].getAttribute("aria-sort"), null);
});

test("sortable header source follows the native button contract", () => {
  const source = fs.readFileSync(adminDbPath, "utf8");
  const css = fs.readFileSync(cssPath, "utf8");

  assert.match(source, /<th scope="col"/, "headers declare scope=col");
  assert.match(source, /<button type="button" class="lm-db-sort-btn"/,
    "sort control is a native type=button");
  assert.doesNotMatch(source, /th\.addEventListener\("click"/,
    "the <th> itself must not be clickable");
  const codeOnly = source.split(/\r?\n/).filter((line) => !line.trim().startsWith("//")).join("\n");
  assert.doesNotMatch(codeOnly, /aria-sort="none"/, "aria-sort is omitted when unsorted");
  assert.match(source, /removeAttribute\("aria-sort"\)/, "unsorted headers drop aria-sort");

  assert.match(css, /\.lm-db-sort-btn:focus-visible\s*\{[^}]*outline:/,
    "the header button has a visible focus indicator");
  assert.match(css, /min-height:\s*24px/, "the sort target meets the 24px minimum");
  assert.match(css, /th\[aria-sort="ascending"\][^{]*\{[^}]*content:\s*"▲"/,
    "ascending appearance is driven by aria-sort in CSS");
  assert.match(css, /th\[aria-sort="descending"\][^{]*\{[^}]*content:\s*"▼"/,
    "descending appearance is driven by aria-sort in CSS");
  assert.doesNotMatch(css, /\.lm-db-sortable\s*\{[^}]*cursor:\s*pointer/,
    "the header cell is no longer presented as the click target");
});