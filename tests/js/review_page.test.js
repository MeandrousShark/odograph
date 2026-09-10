// Behavioral tests for the inline Review page controller. The template
// script is loaded as-is in a small browser-shaped VM, so these exercise
// htmx event ordering and live form state instead of copying its handlers.
const test = require("node:test");
const assert = require("node:assert");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const SCRIPT_PATH = path.join(
  __dirname,
  "..",
  "..",
  "app",
  "templates",
  "review.html"
);
const ReviewState = require(
  path.join(__dirname, "..", "..", "static", "review_state.js")
);

function makeAttributeStore() {
  const attributes = new Map();
  return {
    hasAttribute(name) {
      return attributes.has(name);
    },
    getAttribute(name) {
      return attributes.get(name) ?? null;
    },
    setAttribute(name, value) {
      attributes.set(name, String(value));
    },
    removeAttribute(name) {
      attributes.delete(name);
    },
  };
}

function makeBrowser() {
  const listeners = new Map();
  const bodyListeners = new Map();
  const requests = [];
  let currentForm;
  let nextRequestId = 1;
  let context;
  let observedUndoAction = null;
  let currentCard = {
    id: "review-card",
    isConnected: true,
    renderedBy: null,
  };

  const addListener = (store, type, callback) => {
    const callbacks = store.get(type) || [];
    callbacks.push(callback);
    store.set(type, callbacks);
  };

  const control = (id, name, value = "", tagName = "input") => {
    const attrs = makeAttributeStore();
    const item = {
      ...attrs,
      id,
      name,
      value,
      tagName,
      disabled: false,
      checked: false,
      isConnected: true,
      closest(selector) {
        return selector === "form" ? currentForm : null;
      },
      getAttribute(name) {
        if (name === "hx-post") {
          if (id === "review-next") return "/review/42/tag";
          if (id === "review-skip") return "/review/42/skip";
        }
        return attrs.getAttribute(name);
      },
      matches(selector) {
        return selector === "#review-form input[name=\"category\"]" && name === "category";
      },
    };
    return item;
  };

  const formAttrs = makeAttributeStore();
  const fields = {
    purpose: control("review-purpose", "purpose", "old purpose"),
    notes: control("review-notes", "notes", "old notes"),
    vehicle: control("review-vehicle", "vehicle_id", "7", "select"),
    exclusion: control("review-exclusion", "exclusion", "", "select"),
    category: control("review-business", "category", "business"),
    from: control("review-from", "from", "2026-09-01"),
    to: control("review-to", "to", "2026-09-30"),
    filterVehicle: control("review-filter-vehicle", "vehicle", "7"),
    q: control("review-q", "q", "client"),
  };
  fields.category.checked = true;
  const controls = [
    fields.purpose,
    fields.notes,
    fields.vehicle,
    fields.exclusion,
    fields.category,
  ];
  const next = control("review-next", "");
  const skip = control("review-skip", "");
  const undo = control("review-undo", "");
  controls.push(next, skip);

  const form = {
    ...formAttrs,
    isConnected: true,
    elements: {
      from: fields.from,
      to: fields.to,
      vehicle: fields.filterVehicle,
      q: fields.q,
    },
    querySelector(selector) {
      if (selector === 'input[name="category"]:checked') {
        return fields.category.checked ? fields.category : null;
      }
      return null;
    },
    querySelectorAll(selector) {
      if (selector === "input, select, textarea, button") return controls;
      if (selector === "[data-review-disabled-before-write]") {
        return controls.filter((item) => item.hasAttribute("data-review-disabled-before-write"));
      }
      return [];
    },
  };
  currentForm = form;

  const body = {
    isConnected: true,
    addEventListener(type, callback) {
      addListener(bodyListeners, type, callback);
    },
  };

  const document = {
    body,
    documentElement: {
      contains(item) {
        return Boolean(item && item.isConnected);
      },
    },
    addEventListener(type, callback) {
      addListener(listeners, type, callback);
    },
    getElementById(id) {
      if (id === "review-form") return currentForm;
      if (id === "review-next") return next;
      if (id === "review-skip") return skip;
      if (id === "review-undo") return undo;
      return null;
    },
    querySelector(selector) {
      if (selector === "#review-card" && currentCard.isConnected) return currentCard;
      return null;
    },
  };

  const emit = (type, detail) => {
    const event = {
      detail,
      defaultPrevented: false,
      preventDefault() {
        this.defaultPrevented = true;
      },
    };
    for (const callback of listeners.get(type) || []) callback(event);
    return event;
  };

  const collect = (source, values) => {
    if (values) return { ...values };
    if (source === body) return {};
    return {
      purpose: fields.purpose.value,
      notes: fields.notes.value,
      vehicle_id: fields.vehicle.value,
      exclusion: fields.exclusion.value,
      category: fields.category.checked ? fields.category.value : "",
      from: fields.from.value,
      to: fields.to.value,
      vehicle: fields.filterVehicle.value,
      q: fields.q.value,
    };
  };

  const issue = (method, path, options = {}) => {
    const source = options.source || body;
    const xhr = { id: nextRequestId++ };
    const parameters = collect(source, options.values);
    const detail = {
      elt: source,
      xhr,
      target: typeof options.target === "string"
        ? document.querySelector(options.target)
        : options.target || null,
      pathInfo: { requestPath: path },
      requestConfig: {
        path,
        parameters,
        swapOverride: options.swap,
      },
    };
    const event = emit("htmx:beforeRequest", detail);
    if (event.defaultPrevented) return Promise.resolve({ cancelled: true });
    const request = { method, path, source, xhr, parameters, detail };
    requests.push(request);
    return new Promise((resolve) => {
      request.resolve = resolve;
    });
  };

  const htmx = {
    ajax(method, path, options) {
      if (path.endsWith("/undo")) observedUndoAction = context.lastReviewAction;
      return issue(method, path, options);
    },
  };

  context = {
    console,
    document,
    htmx,
    window: null,
    ReviewState,
    Promise,
    queueMicrotask,
    setTimeout,
    clearTimeout,
    cancelAnimationFrame() {},
    requestAnimationFrame() { return 1; },
  };
  vm.createContext(context);
  context.window = context;

  let source = fs.readFileSync(SCRIPT_PATH, "utf8");
  const scriptStart = source.indexOf('<script nonce="{{ csp_nonce }}">') + '<script nonce="{{ csp_nonce }}">'.length;
  const scriptEnd = source.indexOf("</script>", scriptStart);
  source = source.slice(scriptStart, scriptEnd)
    .replace(/\{\{ map_tile_url \| tojson \}\}/g, '"/tiles/{z}/{x}/{y}.png"')
    .replace(/\{\{ map_tile_attribution \| tojson \}\}/g, '"test tiles"');
  vm.runInContext(source, context, { filename: SCRIPT_PATH });

  const complete = async (request, successful, { detachForm = false } = {}) => {
    if (detachForm) {
      for (const item of controls) item.isConnected = false;
      form.isConnected = false;
      currentForm = null;
      if (successful && /\/review\/\d+\/(?:tag|skip)$/.test(request.path)) {
        currentCard.isConnected = false;
        currentCard = {
          id: "review-card",
          isConnected: true,
          renderedBy: null,
        };
      }
    }
    if (successful && request.path.endsWith("/undo") && request.detail.target === currentCard) {
      currentCard.renderedBy = request;
    }
    emit("htmx:afterRequest", {
      ...request.detail,
      successful,
    });
    request.resolve?.();
    await flush();
  };

  return {
    context,
    body,
    form,
    fields,
    next,
    skip,
    undo,
    controls,
    get reviewCard() {
      return currentCard;
    },
    requests,
    issue,
    complete,
    restoreForm() {
      currentForm = form;
      form.isConnected = true;
      for (const item of controls) item.isConnected = true;
    },
    observedUndoAction: () => observedUndoAction,
  };
}

async function flush() {
  for (let index = 0; index < 8; index += 1) await Promise.resolve();
}

test("queued Next does not record Undo state and replays with newer visible fields", async () => {
  const browser = makeBrowser();
  const autosave = browser.issue("POST", "/trips/42/notes", { source: browser.fields.notes });

  browser.fields.notes.value = "latest notes";
  const queuedNext = browser.issue("POST", "/review/42/tag", { source: browser.next });
  assert.strictEqual(browser.context.pendingReviewAction, null);
  assert.strictEqual(browser.requests.length, 1);

  await browser.complete(browser.requests[0], true);
  assert.strictEqual(await queuedNext.then((result) => result.cancelled), true);
  assert.strictEqual(browser.requests.length, 2);
  assert.strictEqual(browser.requests[1].path, "/review/42/tag");
  assert.strictEqual(browser.requests[1].parameters.notes, "latest notes");
  assert.strictEqual(browser.context.pendingReviewAction.tripId, "42");
  await autosave;
});

test("an advancing request locks fields and restores their prior state after failure", async () => {
  const browser = makeBrowser();
  browser.fields.category.checked = false;
  browser.next.disabled = true;
  const request = browser.issue("POST", "/review/42/skip", { source: browser.skip });

  assert.ok(browser.controls.every((control) => control.disabled));
  assert.strictEqual(browser.form.getAttribute("aria-busy"), "true");

  await browser.complete(browser.requests[0], false);
  assert.strictEqual(browser.form.getAttribute("aria-busy"), null);
  assert.strictEqual(browser.next.disabled, true);
  assert.strictEqual(browser.skip.disabled, false);
  assert.strictEqual(browser.fields.notes.disabled, false);
  await request;
});

test("successful card swaps discard queued writes from detached sources", async () => {
  const browser = makeBrowser();
  browser.issue("POST", "/review/42/tag", { source: browser.next });
  browser.issue("POST", "/trips/42/notes", { source: browser.fields.notes });
  assert.strictEqual(browser.requests.length, 1);

  await browser.complete(browser.requests[0], true, { detachForm: true });
  assert.strictEqual(browser.requests.length, 1);
  assert.strictEqual(browser.context.lastReviewAction.tripId, "42");
});

test("queued Undo resolves and renders into the replacement review card", async () => {
  const browser = makeBrowser();
  const originalCard = browser.reviewCard;
  browser.issue("POST", "/review/42/tag", { source: browser.next });
  browser.issue("POST", "/review/42/undo", {
    source: browser.body,
    target: "#review-card",
    swap: "outerHTML",
    values: { kind: "tag", from: "", to: "", vehicle: "", q: "" },
  });
  assert.strictEqual(browser.requests.length, 1);

  await browser.complete(browser.requests[0], true, { detachForm: true });
  const replacementCard = browser.reviewCard;
  assert.notStrictEqual(replacementCard, originalCard);
  assert.strictEqual(originalCard.isConnected, false);
  assert.strictEqual(browser.requests.length, 2);
  assert.strictEqual(browser.requests[1].detail.target, replacementCard);

  await browser.complete(browser.requests[1], true);
  assert.strictEqual(browser.reviewCard.renderedBy, browser.requests[1]);
});

test("queued Undo replays after action bookkeeping and preserves failed Undo state", async () => {
  const browser = makeBrowser();
  browser.issue("POST", "/review/42/tag", { source: browser.next });
  browser.issue("POST", "/review/42/undo", {
    source: browser.body,
    target: "#review-card",
    swap: "outerHTML",
    values: { kind: "tag", from: "", to: "", vehicle: "", q: "" },
  });
  assert.strictEqual(browser.requests.length, 1);

  await browser.complete(browser.requests[0], true, { detachForm: true });
  assert.strictEqual(browser.observedUndoAction()?.tripId, "42");
  assert.strictEqual(browser.requests.length, 2);

  await browser.complete(browser.requests[1], false);
  assert.ok(browser.context.lastReviewAction);

  browser.context.triggerReviewUndo();
  assert.strictEqual(browser.undo.disabled, true);
  assert.strictEqual(browser.requests.length, 3);
  await browser.complete(browser.requests[2], true);
  assert.strictEqual(browser.context.lastReviewAction, null);
  assert.strictEqual(browser.undo.disabled, true);
});

test("active Undo locks Review fields and restores them after failure", async () => {
  const browser = makeBrowser();
  browser.issue("POST", "/review/42/tag", { source: browser.next });
  await browser.complete(browser.requests[0], true, { detachForm: true });
  browser.restoreForm();
  browser.context.triggerReviewUndo();
  assert.strictEqual(browser.requests.length, 2);
  assert.ok(browser.controls.every((control) => control.disabled));

  browser.issue("POST", "/trips/42/notes", { source: browser.fields.notes });
  browser.issue("POST", "/review/42/skip", { source: browser.skip });
  assert.strictEqual(browser.requests.length, 2);

  await browser.complete(browser.requests[1], false);
  assert.strictEqual(browser.form.getAttribute("aria-busy"), null);
  assert.strictEqual(browser.fields.notes.disabled, false);
  assert.strictEqual(browser.skip.disabled, false);
  assert.strictEqual(browser.undo.disabled, false);
  assert.strictEqual(browser.requests.length, 3);
  await browser.complete(browser.requests[2], true);
  assert.strictEqual(browser.requests.length, 4);
});

test("successful Undo swaps away locked sources and drops their queued writes", async () => {
  const browser = makeBrowser();
  browser.issue("POST", "/review/42/tag", { source: browser.next });
  await browser.complete(browser.requests[0], true, { detachForm: true });
  browser.restoreForm();
  browser.context.triggerReviewUndo();
  browser.issue("POST", "/trips/42/notes", { source: browser.fields.notes });
  assert.strictEqual(browser.requests.length, 2);

  await browser.complete(browser.requests[1], true, { detachForm: true });
  assert.strictEqual(browser.requests.length, 2);
  assert.strictEqual(browser.context.lastReviewAction, null);
});
