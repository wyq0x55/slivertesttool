// Guards the JSON editor's Tab handling (WCAG 2.1.2 No Keyboard Trap).
//
// json_editor.js inserts two spaces on Tab instead of moving focus. That is
// fine for a bare Tab, but `e.key === "Tab"` is also true for Shift+Tab,
// Ctrl+Tab, Alt+Tab and Cmd+Tab, which are all legitimate ways to leave the
// textarea. The handler must therefore act only on an unmodified Tab and let
// every modified Tab fall through to the browser.
//
// The module is a plain IIFE over `window` that also reads `document` as a
// bare global, so it runs against a minimal shim in node:vm and the assertions
// exercise the real keydown listener installed on the textarea.
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const { test } = require("node:test");

const root = path.resolve(__dirname, "../..");
const jsonEditorSrc = fs.readFileSync(
  path.join(root, "app/static/js/lanmatrix/json_editor.js"),
  "utf8",
);
const rowtoolsSrc = fs.readFileSync(
  path.join(root, "app/static/js/lanmatrix/rowtools.js"),
  "utf8",
);

class FakeElement {
  constructor(tag) {
    this.tagName = String(tag).toUpperCase();
    this.ownerDocument = null;
    this.parentNode = null;
    this.children = [];
    this.attributes = new Map();
    this.listeners = new Map();
    this.className = "";
    this.textContent = "";
    this.value = "";
    this.scrollTop = 0;
    this.scrollLeft = 0;
    this.selectionStart = 0;
    this.selectionEnd = 0;
    this.style = {};
    const classes = new Set();
    this._classes = classes;
    this.classList = {
      add: (...names) => names.forEach((name) => classes.add(name)),
      remove: (...names) => names.forEach((name) => classes.delete(name)),
      toggle: (name, force) => {
        const on = force === undefined ? !classes.has(name) : !!force;
        if (on) classes.add(name);
        else classes.delete(name);
        return on;
      },
      contains: (name) => classes.has(name),
    };
  }

  appendChild(child) {
    this.children.push(child);
    child.parentNode = this;
    return child;
  }

  replaceChildren(...nodes) {
    this.children = nodes.slice();
    nodes.forEach((node) => {
      node.parentNode = this;
    });
  }

  setAttribute(name, value) {
    this.attributes.set(name, String(value));
  }

  addEventListener(type, handler) {
    if (!this.listeners.has(type)) this.listeners.set(type, []);
    this.listeners.get(type).push(handler);
  }

  fire(type, event) {
    (this.listeners.get(type) || []).forEach((handler) => handler(event));
  }

  focus() {
    if (this.ownerDocument) this.ownerDocument.activeElement = this;
  }
}

function makeDocument() {
  const doc = {
    activeElement: null,
    createElement(tag) {
      const el = new FakeElement(tag);
      el.ownerDocument = doc;
      return el;
    },
    createTextNode(text) {
      return { nodeType: 3, textContent: String(text), parentNode: null };
    },
  };
  return doc;
}

// Fresh editor per assertion so one keydown cannot leak state into the next.
function makeEditor() {
  const document = makeDocument();
  const window = {};
  vm.runInNewContext(jsonEditorSrc, { window, document, console }, {
    filename: "json_editor.js",
  });
  const host = document.createElement("div");
  const changes = [];
  const editor = new window.LMCodeEditor(host, {
    onChange: (value) => changes.push(value),
  });
  return { editor, changes };
}

function keyEvent(overrides) {
  return Object.assign(
    {
      key: "Tab",
      keyCode: 9,
      shiftKey: false,
      ctrlKey: false,
      altKey: false,
      metaKey: false,
      isComposing: false,
      defaultPrevented: false,
      preventDefault() {
        this.defaultPrevented = true;
      },
    },
    overrides || {},
  );
}

// Put the caret between "a" and "b" so an insert is distinguishable from
// an append, then fire a keydown at the textarea.
function pressTab(overrides) {
  const ctx = makeEditor();
  ctx.editor.ta.value = "ab";
  ctx.editor.ta.selectionStart = 1;
  ctx.editor.ta.selectionEnd = 1;
  const event = keyEvent(overrides);
  ctx.editor.ta.fire("keydown", event);
  return { ...ctx, event };
}

test("plain Tab inserts two spaces and prevents the default focus move", () => {
  const { editor, changes, event } = pressTab();
  assert.equal(editor.ta.value, "a  b");
  assert.equal(editor.getValue(), "a  b");
  assert.equal(event.defaultPrevented, true);
  assert.deepEqual(changes, ["a  b"]);
  // The caret lands after the inserted spaces, ready for the next keystroke.
  assert.equal(editor.ta.selectionStart, 3);
  assert.equal(editor.ta.selectionEnd, 3);
});

test("Shift+Tab falls through to the browser (regression guard)", () => {
  const { editor, changes, event } = pressTab({ shiftKey: true });
  assert.equal(editor.ta.value, "ab");
  assert.equal(editor.getValue(), "ab");
  assert.equal(event.defaultPrevented, false);
  assert.deepEqual(changes, []);
});

for (const modifier of ["ctrlKey", "altKey", "metaKey"]) {
  test(`${modifier.replace(/Key$/, "")}+Tab falls through to the browser`, () => {
    const { editor, changes, event } = pressTab({ [modifier]: true });
    assert.equal(editor.ta.value, "ab");
    assert.equal(event.defaultPrevented, false);
    assert.deepEqual(changes, []);
  });
}

test("the IME guard still returns early for keyCode 229", () => {
  const { editor, changes, event } = pressTab({ keyCode: 229 });
  assert.equal(editor.ta.value, "ab");
  assert.equal(event.defaultPrevented, false);
  assert.deepEqual(changes, []);
});

test("the IME guard still returns early while composing", () => {
  const { editor, changes, event } = pressTab({ isComposing: true });
  assert.equal(editor.ta.value, "ab");
  assert.equal(event.defaultPrevented, false);
  assert.deepEqual(changes, []);
});

test("non-Tab keys were never intercepted", () => {
  const { editor, changes, event } = pressTab({ key: "Escape", keyCode: 27 });
  assert.equal(editor.ta.value, "ab");
  assert.equal(event.defaultPrevented, false);
  assert.deepEqual(changes, []);
});

test("rowtools help text tells users how to leave the JSON editor", () => {
  const window = {};
  vm.runInNewContext(rowtoolsSrc, { window, console }, {
    filename: "rowtools.js",
  });
  const entry = window.LMRowTools.SHORTCUTS.find(
    (shortcut) => shortcut.scope === "JSON 编辑" && shortcut.keys.includes("Tab"),
  );
  assert.ok(entry, "a Tab shortcut entry exists for the JSON 编辑 scope");
  assert.match(entry.desc, /缩进/);
  assert.match(entry.desc, /Shift\+Tab/);
  assert.match(entry.desc, /上一个控件/);
});