const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const root = path.resolve(__dirname, "../..");
const read = (file) => fs.readFileSync(path.join(root, file), "utf8");
const settle = () => new Promise((resolve) => setImmediate(resolve));

// Minimal element shim. `textContent` concatenates descendants like the real
// DOM (and a write clears children), `hidden` and `innerHTML` are tracked
// accessors so a test can prove the production code never touches them.
function textOf(node) {
  if (!node) return "";
  if (node.nodeType === 3) return node.textContent;
  if (node._text !== null) return node._text;
  return node.children.map(textOf).join("");
}

function makeNode(tag) {
  const node = {
    nodeType: 1,
    tagName: tag ? tag.toUpperCase() : "",
    id: "",
    className: "",
    type: "",
    value: "",
    dataset: {},
    children: [],
    listeners: {},
    attributes: {},
    innerHTMLWrites: [],
    hiddenWrites: [],
    _text: null,
    _innerHTML: "",
    _hidden: false,
    style: {},
    // Multiple handlers are kept so a duplicated binding is observable.
    addEventListener(type, callback) {
      if (!this.listeners[type]) this.listeners[type] = [];
      this.listeners[type].push(callback);
    },
    dispatch(type, event = {}) {
      return Promise.all((this.listeners[type] || []).map((callback) => callback(event)));
    },
    appendChild(child) { this.children.push(child); return child; },
    append(...items) {
      for (const item of items) {
        this.children.push(typeof item === "string" ? { nodeType: 3, textContent: item } : item);
      }
    },
    replaceChildren(...items) {
      this.children.length = 0;
      this._text = null;
      this.append(...items);
    },
    setAttribute(name, value) { this.attributes[name] = String(value); },
    getAttribute(name) { return this.attributes[name] ?? null; },
    removeAttribute(name) { delete this.attributes[name]; },
    closest() { return null; },
    querySelectorAll() { return []; },
    select() {},
    remove() {},
    reset() {},
  };
  Object.defineProperty(node, "textContent", {
    configurable: true,
    get() { return textOf(node); },
    set(value) { node._text = String(value); node.children.length = 0; },
  });
  Object.defineProperty(node, "innerHTML", {
    configurable: true,
    get() { return node._innerHTML; },
    set(value) { node._innerHTML = String(value); node.innerHTMLWrites.push(String(value)); },
  });
  Object.defineProperty(node, "hidden", {
    configurable: true,
    get() { return node._hidden; },
    set(value) { node.hiddenWrites.push(value); node._hidden = value; },
  });
  return node;
}

function harness(tokens = ["sekret"]) {
  const fixed = new Map();
  const created = [];
  // Only the ids present in the rendered template exist before a token is
  // issued; every other id resolves to null like a real document, so a
  // re-introduced top-level lookup would fail the test instead of being
  // silently satisfied by a fabricated node.
  for (const id of ["lm-token-rows", "lm-token-info", "lm-token-form",
    "lm-token-secret", "lm-token-name", "lm-token-days"]) {
    const node = makeNode("");
    node.id = id;
    fixed.set(id, node);
  }
  const get = (id) => {
    for (let i = created.length - 1; i >= 0; i -= 1) {
      if (created[i].id === id) return created[i];
    }
    return fixed.get(id) ?? null;
  };

  const form = get("lm-token-form");
  form.name = { value: "agent" };
  form.days = { value: "90" };
  get("lm-token-name").value = "agent";
  get("lm-token-days").value = "90";

  const calls = [];
  const clipboardWrites = [];
  const queue = [...tokens];
  const timers = new Map();
  let timerId = 0;
  const runTimers = () => {
    const due = [...timers.values()];
    timers.clear();
    due.forEach((callback) => callback());
  };
  const api = {
    meTokens: async () => ({ tokens: [] }),
    createMeToken: async (name, days) => {
      calls.push({ name, days });
      return { token: queue.length > 1 ? queue.shift() : queue[0] };
    },
    revokeMeToken: async () => ({}),
  };

  const context = vm.createContext({
    document: {
      getElementById: get,
      createElement: (tag) => {
        const node = makeNode(tag);
        created.push(node);
        return node;
      },
      createTextNode: (value) => ({ nodeType: 3, textContent: String(value) }),
      body: makeNode("body"),
    },
    LMApi: api,
    LMUI: { alert() {}, confirm: async () => false },
    navigator: {
      clipboard: {
        writeText: async (text) => { clipboardWrites.push(text); },
      },
    },
    window: { isSecureContext: true },
    setTimeout: (callback) => {
      timerId += 1;
      timers.set(timerId, callback);
      return timerId;
    },
    clearTimeout: (id) => { timers.delete(id); },
  });

  vm.runInContext(read("app/static/js/lanmatrix/tokens.js"), context, { filename: "tokens.js" });

  return {
    get,
    calls,
    clipboardWrites: () => clipboardWrites,
    pendingTimers: () => timers.size,
    runTimers,
    allNodes: () => [...fixed.values(), ...created],
    ready: settle,
    submit: (event) => form.dispatch("submit", event),
  };
}

test("the token banner is one mounted, initially empty status region", () => {
  const html = read("app/templates/lanmatrix/tokens.html");

  const occurrences = html.match(/id="lm-token-secret"/g) || [];
  assert.equal(occurrences.length, 1, "the banner id must appear exactly once");

  const tag = html.match(/<div\b[^>]*\bid="lm-token-secret"[^>]*>/);
  assert.ok(tag, "expected a div with id=lm-token-secret");
  assert.match(tag[0], /\brole="status"/, "the banner must stay a status live region");
  assert.match(tag[0], /\baria-atomic="true"/, "the whole banner must be announced as one unit");
  assert.doesNotMatch(tag[0], /\shidden(?=[\s>])/, "the region must be mounted, not hidden");

  assert.match(
    html,
    /<div\b[^>]*\bid="lm-token-secret"[^>]*><\/div>/,
    "the live region must start empty so that filling it is the announcement",
  );
});

test("an empty banner takes no space and stays announced-able", () => {
  const css = read("app/static/css/design-system.css");
  assert.match(
    css,
    /\.lm-banner:empty\s*\{[^}]*\}/,
    "the empty mounted banner must not paint a bordered box",
  );
  const rule = css.match(/\.lm-banner:empty\s*\{[^}]*\}/)[0];
  assert.doesNotMatch(
    rule,
    /display\s*:\s*none/,
    "an empty live region must stay in the accessibility tree: a region added " +
      "to it at the same time as its text is not announced reliably",
  );
  assert.match(
    rule,
    /clip-path\s*:\s*inset\(50%\)/,
    "the empty banner is clipped out of view instead of removed from the tree",
  );
});

test("issuing a token fills the live region with the secret as text", async () => {
  const h = harness();
  await h.ready();

  const box = h.get("lm-token-secret");
  assert.equal(box.textContent, "", "the banner starts empty");
  assert.equal(box.children.length, 0, "the banner starts with no children");
  assert.deepEqual(box.hiddenWrites, [], "the banner is never toggled hidden");

  await h.submit({ preventDefault() {} });
  await settle();

  assert.deepEqual(h.calls, [{ name: "agent", days: 90 }], "the real submit handler ran");

  const value = h.get("lm-token-value");
  assert.equal(value.textContent, "sekret", "the token is written as text");
  assert.match(box.textContent, /请立即复制保存，此 Token 只显示这一次。/);
  assert.match(box.textContent, /sekret/, "the live region carries the issued token");
  assert.equal(box.children.length, 2, "the announcement is inserted into the mounted region");
  assert.deepEqual(box.hiddenWrites, [], "content insertion replaces the old hidden toggle");

  const innerHTMLWrites = h.allNodes().flatMap((node) => node.innerHTMLWrites || []);
  assert.deepEqual(innerHTMLWrites, [], "the secret is never assigned through innerHTML");
});

test("the re-attached copy button keeps its feedback behaviour", async () => {
  const h = harness();
  await h.ready();
  await h.submit({ preventDefault() {} });
  await settle();

  const button = h.get("lm-token-copy");
  assert.equal(button.textContent, "复制");
  assert.equal(button.listeners.click.length, 1, "the copy handler is attached once to the new button");

  await button.dispatch("click");

  assert.deepEqual(h.clipboardWrites(), ["sekret"], "the copy handler reads the issued token");
  assert.equal(button.textContent, "已复制");
  assert.equal(button.getAttribute("data-copied"), "");
  assert.equal(h.pendingTimers(), 1, "the label restore is scheduled");
  h.runTimers();
  assert.equal(button.textContent, "复制", "the timer restores the default label");
  assert.equal(button.getAttribute("data-copied"), null);
});

test("re-issuing replaces the secret, clears the feedback timer and binds one handler", async () => {
  const h = harness(["sekret", "rotated"]);
  await h.ready();
  await h.submit({ preventDefault() {} });
  await settle();

  const firstButton = h.get("lm-token-copy");
  await firstButton.dispatch("click");
  assert.equal(h.pendingTimers(), 1);

  await h.submit({ preventDefault() {} });
  await settle();

  const box = h.get("lm-token-secret");
  assert.equal(box.children.length, 2, "the region is replaced, not appended to");
  assert.doesNotMatch(box.textContent, /sekret/, "the previous secret is gone");
  assert.equal(h.get("lm-token-value").textContent, "rotated");
  assert.equal(h.pendingTimers(), 0, "the stale feedback timer is cleared");

  const button = h.get("lm-token-copy");
  assert.notEqual(button, firstButton, "a fresh copy button is built");
  assert.equal(button.listeners.click.length, 1);
  await button.dispatch("click");
  assert.deepEqual(h.clipboardWrites(), ["sekret", "rotated"], "one handler call per click");
});