// Behavioral tests for static/trip_tools.js, run by Node's built-in test
// runner. The module loads the same way the browser loads it (a classic
// script with a guarded CommonJS export), and init() runs against a small
// fake DOM shaped like trip.html's toolbar, so these exercise the real
// state machine and request handling rather than a copy of them.
const test = require("node:test");
const assert = require("node:assert");
const path = require("node:path");

const TripTools = require(
  path.join(__dirname, "..", "..", "static", "trip_tools.js")
);

function makeElement(id, attributes = {}) {
  const attrs = new Map(Object.entries(attributes));
  const listeners = new Map();
  return {
    id,
    hidden: false,
    disabled: false,
    open: false,
    textContent: "",
    children: [],
    getAttribute(name) {
      return attrs.has(name) ? attrs.get(name) : null;
    },
    setAttribute(name, value) {
      attrs.set(name, String(value));
    },
    addEventListener(type, callback) {
      const callbacks = listeners.get(type) || [];
      callbacks.push(callback);
      listeners.set(type, callbacks);
    },
    fire(type, detail, properties = {}) {
      const event = {
        type,
        detail,
        defaultPrevented: false,
        preventDefault() {
          this.defaultPrevented = true;
        },
        ...properties,
      };
      for (const callback of listeners.get(type) || []) callback(event);
      return event;
    },
    querySelectorAll(selector) {
      if (selector === "[data-tool]") {
        return this.children.filter((child) => child.getAttribute("data-tool") !== null);
      }
      if (selector === "input, select, button") return this.children;
      return [];
    },
    closest() {
      return this.parent || null;
    },
    focus() {
      this.focused = true;
    },
  };
}

function makePage(tools = ["merge", "split", "name-start", "name-end"]) {
  const panelIds = {
    merge: "merge-panel",
    split: "split-panel",
    "name-start": "name-start",
    "name-end": "name-end",
  };
  const byId = new Map();
  const disclosure = makeElement("advanced-tools");
  const root = makeElement("trip-tools", { "data-active-tool": "" });
  root.parent = disclosure;
  const buttons = {};
  for (const tool of tools) {
    const button = makeElement(`${tool}-toggle`, {
      "data-tool": tool,
      "aria-controls": panelIds[tool],
      "aria-expanded": "false",
    });
    const panel = makeElement(panelIds[tool]);
    panel.hidden = true;
    root.children.push(button);
    buttons[tool] = button;
    byId.set(panelIds[tool], panel);
  }
  const form = makeElement("merge-form");
  // A recent-purpose picker with no choices renders disabled.
  const emptyPicker = makeElement("purpose-recent");
  emptyPicker.disabled = true;
  form.children = [
    makeElement("notes"), emptyPicker, makeElement("merge-prev"), makeElement("merge-next"),
  ];
  const status = makeElement("merge-status");
  byId.set("merge-form", form);
  byId.set("merge-status", status);
  const view = makeElement("window");
  const doc = { getElementById: (id) => byId.get(id) || null, defaultView: view };
  const controller = TripTools.init(root, doc);
  const changes = [];
  controller.onChange((tool, previous) => changes.push([tool, previous]));
  return {
    controller, root, disclosure, buttons, panel: (id) => byId.get(id), form, status, changes,
    emptyPicker, view,
  };
}

function xhr(status, headers = {}) {
  return {
    status,
    getResponseHeader(name) {
      return Object.prototype.hasOwnProperty.call(headers, name) ? headers[name] : null;
    },
  };
}

test("activating a tool opens it and activating it again closes it", () => {
  assert.strictEqual(TripTools.nextActiveTool("", "merge"), "merge");
  assert.strictEqual(TripTools.nextActiveTool("merge", "merge"), "");
  assert.strictEqual(TripTools.nextActiveTool("merge", "split"), "split");
});

test("only an active naming tool names a map click", () => {
  assert.deepStrictEqual(TripTools.namingFieldIds("name-start"), { lat: "start-lat", lon: "start-lon" });
  assert.deepStrictEqual(TripTools.namingFieldIds("name-end"), { lat: "end-lat", lon: "end-lon" });
  for (const active of ["", "merge", "split", null, undefined, "toString"]) {
    assert.strictEqual(TripTools.namingFieldIds(active), null);
  }
});

test("a 2xx or HX-Redirect response counts as a successful request", () => {
  assert.strictEqual(TripTools.requestSucceeded(xhr(204, { "HX-Redirect": "/trips/7" })), true);
  assert.strictEqual(TripTools.requestSucceeded(xhr(204)), true);
  assert.strictEqual(TripTools.requestSucceeded(xhr(200)), true);
  assert.strictEqual(TripTools.requestSucceeded(xhr(409, { "HX-Redirect": "/trips" })), true);
});

test("an error status, send error, abort or timeout is a failed request", () => {
  assert.strictEqual(TripTools.requestSucceeded(xhr(400)), false);
  assert.strictEqual(TripTools.requestSucceeded(xhr(500)), false);
  // onerror, onabort and ontimeout all reach afterRequest with status 0.
  assert.strictEqual(TripTools.requestSucceeded(xhr(0)), false);
  assert.strictEqual(TripTools.requestSucceeded(undefined), false);
  assert.strictEqual(TripTools.requestSucceeded({ status: 0 }), false);
});

test("the toolbar keeps zero or one tool active", () => {
  const page = makePage();
  assert.strictEqual(page.controller.active(), "");

  page.buttons.merge.fire("click");
  assert.strictEqual(page.root.getAttribute("data-active-tool"), "merge");
  assert.strictEqual(page.buttons.merge.getAttribute("aria-expanded"), "true");
  assert.strictEqual(page.panel("merge-panel").hidden, false);

  page.buttons["name-start"].fire("click");
  assert.strictEqual(page.controller.active(), "name-start");
  assert.strictEqual(page.buttons.merge.getAttribute("aria-expanded"), "false");
  assert.strictEqual(page.panel("merge-panel").hidden, true);
  assert.strictEqual(page.panel("name-start").hidden, false);
  const expanded = Object.values(page.buttons).filter(
    (button) => button.getAttribute("aria-expanded") === "true"
  );
  assert.strictEqual(expanded.length, 1);

  page.buttons["name-start"].fire("click");
  assert.strictEqual(page.controller.active(), "");
  assert.ok(Object.values(page.buttons).every((b) => b.getAttribute("aria-expanded") === "false"));
  assert.strictEqual(page.panel("name-start").hidden, true);

  assert.deepStrictEqual(page.changes, [
    ["merge", ""],
    ["name-start", "merge"],
    ["", "name-start"],
  ]);
});

test("the split tool shows the split panel and another tool closes it", () => {
  const page = makePage();
  page.buttons.split.fire("click");
  assert.strictEqual(page.panel("split-panel").hidden, false);

  page.buttons.merge.fire("click");
  assert.strictEqual(page.panel("split-panel").hidden, true);
  assert.deepStrictEqual(page.changes, [["split", ""], ["merge", "split"]]);
});

test("closing the disclosure clears the active tool", () => {
  const page = makePage();
  page.buttons.split.fire("click");
  page.disclosure.open = true;
  page.disclosure.fire("toggle");
  assert.strictEqual(page.controller.active(), "split");

  page.disclosure.open = false;
  page.disclosure.fire("toggle");
  assert.strictEqual(page.controller.active(), "");
  assert.strictEqual(page.panel("split-panel").hidden, true);
  assert.deepStrictEqual(page.changes, [["split", ""], ["", "split"]]);
});

test("setting the same tool again does not report a change", () => {
  const page = makePage();
  page.controller.setActive("");
  page.controller.setActive("merge");
  page.controller.setActive("merge");
  assert.deepStrictEqual(page.changes, [["merge", ""]]);
});

test("a toolbar with only some tools still works", () => {
  const page = makePage(["split"]);
  page.buttons.split.fire("click");
  assert.strictEqual(page.controller.active(), "split");
  page.buttons.split.fire("click");
  assert.strictEqual(page.controller.active(), "");
});

test("a merge shows progress and keeps it while the redirect loads", () => {
  const page = makePage();
  page.form.fire("htmx:beforeRequest", {});
  assert.strictEqual(page.status.textContent, "Processing...");

  page.form.fire("htmx:afterRequest", { xhr: xhr(204, { "HX-Redirect": "/trips/9" }) });
  assert.strictEqual(page.status.textContent, "Processing...");
  assert.ok(page.form.children.every((control) => control.disabled));
});

// htmx's own hx-disabled-elt bookkeeping: everything listed is disabled for
// the request and every one of them is re-enabled before afterRequest.
function simulateHtmxRequest(page, button, response) {
  page.form.fire("htmx:beforeRequest", { elt: button });
  page.form.children.forEach((control) => { control.disabled = true; });
  page.form.children.forEach((control) => { control.disabled = false; });
  page.form.fire("htmx:afterRequest", { elt: button, xhr: response });
}

test("a failed merge clears the progress and leaves the form usable", () => {
  for (const failure of [xhr(400), xhr(500), xhr(0)]) {
    const page = makePage();
    const button = page.form.children.find((control) => control.id === "merge-prev");
    simulateHtmxRequest(page, button, failure);
    assert.strictEqual(page.status.textContent, "");
    const usable = page.form.children.filter((control) => control !== page.emptyPicker);
    assert.ok(usable.every((control) => !control.disabled));
    // The picker had no choices, so it stays disabled as rendered.
    assert.strictEqual(page.emptyPicker.disabled, true);
    // Focus returns to the button the request disabled.
    assert.strictEqual(button.focused, true);
  }
});

test("a page restored from the back/forward cache after a merge is usable again", () => {
  const page = makePage();
  const button = page.form.children.find((control) => control.id === "merge-next");
  simulateHtmxRequest(page, button, xhr(204, { "HX-Redirect": "/trips/9" }));
  assert.strictEqual(page.status.textContent, "Processing...");
  assert.ok(page.form.children.every((control) => control.disabled));

  // A fresh load is not a restore and changes nothing.
  page.view.fire("pageshow", undefined, { persisted: false });
  assert.strictEqual(page.status.textContent, "Processing...");

  page.view.fire("pageshow", undefined, { persisted: true });
  assert.strictEqual(page.status.textContent, "");
  const usable = page.form.children.filter((control) => control !== page.emptyPicker);
  assert.ok(usable.every((control) => !control.disabled));
  assert.strictEqual(page.emptyPicker.disabled, true);
});

test("the merge form never submits natively", () => {
  const page = makePage();
  assert.strictEqual(page.form.fire("submit").defaultPrevented, true);
});

test("init tolerates a page without the toolbar", () => {
  assert.strictEqual(TripTools.init(null, { getElementById: () => null }), null);
});
