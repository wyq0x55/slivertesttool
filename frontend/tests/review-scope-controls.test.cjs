/* The review queue's three scope switchers are independent toggle buttons over
 * one list, not a tab widget: each click re-queries the same queue with a
 * different role/status filter, there is no tabpanel and no arrow-key roving.
 * The markup therefore exposes role="group" + aria-pressed, and this test runs
 * the REAL workspace_reviews.js to prove the script keeps that state in sync.
 */
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const homeTemplate = fs.readFileSync(
  path.join(__dirname, "../../app/templates/lanmatrix/home.html"), "utf8",
);
const source = fs.readFileSync(
  path.join(__dirname, "../../app/static/js/lanmatrix/workspace_reviews.js"),
  "utf8",
);

const attr = (tag, name) => {
  const m = tag.match(new RegExp(`(?:^|\\s)${name}="([^"]*)"`));
  return m ? m[1] : null;
};

/* The scope block as it ships, so the buttons the script drives are exactly
 * the ones the template renders. */
function scopeBlock() {
  const start = homeTemplate.indexOf('<div class="lm-rv-scopes"');
  assert.notEqual(start, -1, "the scope group must exist");
  const end = homeTemplate.indexOf("</div>", start);
  return homeTemplate.slice(start, end + "</div>".length);
}

/* One string per button, from its <button> through its closing </button>, so
 * the count badge nested inside it can be checked against the right scope. */
function buttonSegments() {
  const block = scopeBlock();
  const starts = [...block.matchAll(/<button\b/g)].map((m) => m.index);
  return starts.map((start, i) => block.slice(
    start, i + 1 < starts.length ? starts[i + 1] : block.indexOf("</div>", start),
  ));
}

function templateButtons() {
  return buttonSegments().map((segment) => segment.match(/^<button\b[^>]*>/)[0]);
}

function element(tagName) {
  const node = {
    tagName,
    dataset: {},
    attributes: {},
    children: [],
    listeners: {},
    classes: new Set(),
    textContent: "",
    hidden: false,
    classList: {
      add(name) { node.classes.add(name); },
      remove(name) { node.classes.delete(name); },
      contains(name) { return node.classes.has(name); },
      toggle(name, force) {
        const on = force === undefined ? !node.classes.has(name) : force;
        if (on) node.classes.add(name); else node.classes.delete(name);
        return on;
      },
    },
    setAttribute(name, value) { node.attributes[name] = String(value); },
    getAttribute(name) {
      return Object.hasOwn(node.attributes, name) ? node.attributes[name] : null;
    },
    appendChild(child) { child.parentElement = node; node.children.push(child); },
    append(...children) { children.forEach((child) => node.appendChild(child)); },
    replaceChildren(...children) {
      node.children = [];
      node.append(...children);
    },
    querySelector() { return null; },
    addEventListener(type, listener) { node.listeners[type] = listener; },
  };
  return node;
}

function templateElement(tag) {
  const node = element("button");
  const cl = attr(tag, "class");
  if (cl) cl.split(/\s+/).forEach((name) => node.classes.add(name));
  if (attr(tag, "role")) node.attributes.role = attr(tag, "role");
  if (attr(tag, "aria-pressed")) {
    node.attributes["aria-pressed"] = attr(tag, "aria-pressed");
  }
  for (const m of tag.matchAll(/\bdata-([\w-]+)="([^"]*)"/g)) {
    node.dataset[m[1]] = m[2];
  }
  return node;
}

function textNodeHost() {
  const node = element("div");
  node.querySelector = (selector) => {
    if (selector === "h3" || selector === "p") {
      node[selector] = node[selector] || element(selector);
      return node[selector];
    }
    return null;
  };
  return node;
}

async function boot() {
  const scopeButtons = templateButtons().map(templateElement);
  const rowsHost = element("tbody");
  const nodes = {
    "lm-h-rv-rows": rowsHost,
    "lm-h-rv-card": element("div"),
    "lm-h-rv-empty": textNodeHost(),
    "lm-h-rv-toolbar": element("div"),
    "lm-h-rv-count": element("span"),
    "lm-h-rv-title": element("h2"),
    "lm-h-rv-n-pending": element("span"),
    "lm-h-rv-n-rejected": element("span"),
    "lm-h-rv-n-decided": element("span"),
  };
  const document = {
    listeners: {},
    createElement: element,
    createTextNode: (text) => ({ textContent: text }),
    getElementById: (id) => nodes[id] ?? null,
    querySelector: () => null,
    querySelectorAll: (selector) =>
      selector === ".lm-rv-scope" ? scopeButtons : [],
    addEventListener(type, listener) { this.listeners[type] = listener; },
  };

  const calls = [];
  const LMApi = {
    meReviews: async (params) => {
      calls.push(params);
      return {
        reviews: [],
        projects: [],
        queue_counts: { pending: 8, rejected: 3, decided: 2 },
      };
    },
  };
  vm.runInNewContext(source, {
    window: { LMApi }, document, LMApi, console,
  });

  const settle = () => new Promise((resolve) => setImmediate(resolve));
  document.listeners.DOMContentLoaded();
  await settle();
  await settle();

  const pressed = (button) => button.getAttribute("aria-pressed");
  const find = (role, status) =>
    scopeButtons.find(
      (b) => b.dataset.role === role && b.dataset.status === status,
    );

  return { scopeButtons, calls, settle, pressed, find, nodes };
}

test("the template renders a toggle-button group, not a fake tablist",
  () => {
  const block = scopeBlock();
  assert.match(block, /role="group"/);
  assert.match(block, /aria-label="审核范围"/);
  assert.doesNotMatch(block, /role="tablist"/);
  assert.doesNotMatch(block, /role="tab"/);
  assert.doesNotMatch(block, /aria-selected/);

  const buttons = templateButtons();
  assert.equal(buttons.length, 3);
  assert.deepEqual(
    buttons.map((b) => attr(b, "aria-pressed")),
    ["true", "false", "false"],
  );
  assert.equal(attr(buttons[0], "data-role"), "reviewer");
  assert.equal(attr(buttons[0], "data-status"), "pending");

  /* Every class, data attribute, id and badge survives the role change. */
  const segments = buttonSegments();
  assert.match(segments[0], /id="lm-h-rv-n-pending"/);
  assert.match(segments[1], /id="lm-h-rv-n-rejected"/);
  assert.match(segments[2], /id="lm-h-rv-n-decided"/);
  assert.match(segments[0], /class="btn small lm-rv-scope is-on"/);
  assert.equal(attr(segments[1], "class"), "btn small ghost lm-rv-scope");
});

test("the real script keeps aria-pressed on the default scope", async () => {
  const { find, pressed } = await boot();
  assert.equal(pressed(find("reviewer", "pending")), "true");
  assert.equal(pressed(find("requester", "rejected")), "false");
  assert.equal(pressed(find("reviewer", "decided")), "false");
  const buttons = templateButtons();
  for (let i = 0; i < 3; i += 1) assert.equal(buttons[i].includes('role="tab"'), false);
});

test("activating another scope moves aria-pressed and .is-on", async () => {
  const { scopeButtons, calls, settle, pressed, find } = await boot();
  const rejected = find("requester", "rejected");
  rejected.listeners.click();
  await settle();
  await settle();

  assert.equal(pressed(rejected), "true");
  assert.equal(pressed(find("reviewer", "pending")), "false");
  assert.equal(pressed(find("reviewer", "decided")), "false");
  assert.equal(rejected.classList.contains("is-on"), true);
  assert.equal(rejected.classList.contains("ghost"), false);
  assert.equal(find("reviewer", "pending").classList.contains("is-on"), false);
  assert.equal(find("reviewer", "pending").classList.contains("ghost"), true);
  /* No tab widget leftovers on any button. */
  for (const button of scopeButtons) {
    assert.equal(button.getAttribute("aria-selected"), null);
    assert.equal(button.getAttribute("role"), null);
  }

  /* Same request shape, with only the filter swapped. The params object was
   * built inside the VM realm, so spread it into a host object first. */
  assert.deepEqual({ ...calls[0] }, {
    limit: 200, role: "reviewer", status: "pending",
  });
  assert.deepEqual({ ...calls.at(-1) }, {
    limit: 200, role: "requester", status: "rejected",
  });
});

test("scope changes leave the count badges and request contract alone",
  async () => {
  const { calls, settle, find, nodes } = await boot();
  assert.equal(nodes["lm-h-rv-n-pending"].textContent, "8");
  assert.equal(nodes["lm-h-rv-n-rejected"].textContent, "3");
  assert.equal(nodes["lm-h-rv-n-decided"].textContent, "2");

  find("reviewer", "decided").listeners.click();
  await settle();
  await settle();

  assert.equal(nodes["lm-h-rv-n-pending"].textContent, "8");
  assert.equal(nodes["lm-h-rv-n-rejected"].textContent, "3");
  assert.equal(nodes["lm-h-rv-n-decided"].textContent, "2");
  assert.equal(calls.length, 2);
  assert.deepEqual({ ...calls.at(-1) }, {
    limit: 200, role: "reviewer", status: "decided",
  });
});