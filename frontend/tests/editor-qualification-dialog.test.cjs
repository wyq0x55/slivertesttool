const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

function createElement(tagName) {
  const attributes = new Map();
  const listeners = new Map();
  return {
    tagName,
    children: [],
    style: {},
    appendChild(child) {
      this.children.push(child);
      child.parentNode = this;
    },
    setAttribute(name, value) {
      attributes.set(name, value);
    },
    getAttribute(name) {
      return attributes.get(name) ?? null;
    },
    addEventListener(type, listener) {
      listeners.set(type, listener);
    },
    click() {
      return listeners.get("click")?.();
    },
    showModal() {
      this.open = true;
    },
    close() {
      this.open = false;
    },
  };
}

function findById(element, id) {
  if (element.id === id) return element;
  for (const child of element.children) {
    const found = findById(child, id);
    if (found) return found;
  }
  return null;
}

test("editor toolbar opens a named signal list dialog with a labeled, described textarea", async () => {
  const body = createElement("body");
  const toolbar = createElement("div");
  toolbar.id = "lm-topbar-exec";
  body.appendChild(toolbar);
  const runButton = createElement("button");
  runButton.id = "lm-run-selected";
  toolbar.appendChild(runButton);
  const document = {
    body,
    createElement,
    getElementById: (id) => findById(body, id),
    querySelector: (selector) => selector === ".lm-editor" ? { dataset: { projectId: "42" } } : null,
  };
  const requests = [];
  const window = {
    LMApi: {
      async getProject(id) {
        requests.push(["getProject", id]);
        return { project: { default_signal_list: "v_speed" } };
      },
      async patchProject(id, updates) {
        requests.push(["patchProject", id, { ...updates }]);
      },
    },
  };
  const script = path.join(__dirname, "../../app/static/js/lanmatrix/editor_qualification.js");
  vm.runInNewContext(fs.readFileSync(script, "utf8"), { window, document });

  const trigger = document.getElementById("lm-signal-list-btn");
  assert.equal(trigger.parentNode, toolbar);
  assert.equal(document.getElementById("lm-signal-list-dialog"), null);
  trigger.click();
  await new Promise(setImmediate);

  const dialog = document.getElementById("lm-signal-list-dialog");
  assert.equal(dialog.tagName, "dialog");
  assert.equal(dialog.open, true);
  const [form] = dialog.children;
  const heading = form.children.find((child) => child.tagName === "h3");
  assert.match(heading.textContent, /默认信号集配置/);
  assert.ok(heading.id);
  assert.equal(dialog.getAttribute("aria-labelledby"), heading.id);

  const textarea = document.getElementById("lm-default-signal-list");
  assert.equal(textarea.tagName, "textarea");
  assert.equal(textarea.value, "v_speed");
  const label = form.children.find((child) => child.tagName === "label");
  assert.ok(label, "a native textarea label is present");
  assert.equal(label.htmlFor, textarea.id);
  assert.ok(!label.hidden);
  assert.match(label.textContent, /观测信号/);
  assert.ok(form.children.indexOf(label) < form.children.indexOf(textarea));
  const instructions = findById(dialog, textarea.getAttribute("aria-describedby"));
  assert.equal(instructions.tagName, "p");
  assert.match(instructions.textContent, /每行填写一个观测信号名/);

  document.getElementById("lm-signal-list-cancel").click();
  assert.equal(dialog.open, false);
  assert.deepEqual(requests, [["getProject", 42]]);

  trigger.click();
  await new Promise(setImmediate);
  assert.equal(document.getElementById("lm-signal-list-dialog"), dialog);
  assert.equal(dialog.open, true);
  textarea.value = " v_speed \r\n\r\n u1_brake_lamp ";
  await document.getElementById("lm-signal-list-save").click();
  assert.deepEqual(requests, [
    ["getProject", 42],
    ["getProject", 42],
    ["patchProject", 42, { default_signal_list: "v_speed\nu1_brake_lamp" }],
  ]);
  assert.equal(dialog.open, false);
});
