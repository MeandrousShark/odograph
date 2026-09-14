from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from tests.test_trips_template import _render_index


pytestmark = pytest.mark.ops


ROOT = Path(__file__).parents[1]


def _inline_scripts():
    body = _render_index()
    archive_start = body.index("  (() => {\n    const helpers = window.ArchiveState;")
    archive_end = body.index("</script>", archive_start)
    selection_start = body.index("  // Checkbox multi-select actions use plain fetch")
    selection_end = body.index("</script>", selection_start)
    return body[archive_start:archive_end], body[selection_start:selection_end]


HARNESS = r"""
const vm = require("node:vm");

const scenario = JSON.parse(process.argv[1]);
const listeners = new Map();
const requests = [];
const values = Object.assign({
  q: "",
  category: "",
  date_preset: "all",
  from: "",
  to: "",
  vehicle: "",
  exclusion: "",
}, scenario.values || {});
const state = Object.assign({
  q: values.q,
  category: values.category,
  date_preset: values.date_preset,
  from: values.from,
  to: values.to,
  vehicle: values.vehicle,
  exclusion: values.exclusion,
}, scenario.state || {});
const location = { href: "/trips" };
const localStorage = { removed: [], removeItem(key) { this.removed.push(key); } };
const history = {
  state: {},
  replaceState(next) { this.state = next; },
};

function makeElement(name, options = {}) {
  const handlers = new Map();
  const element = {
    name,
    id: options.id || "",
    value: options.value || "",
    type: options.type || "text",
    checked: Boolean(options.checked),
    disabled: Boolean(options.disabled),
    hidden: Boolean(options.hidden),
    textContent: options.textContent || "",
    href: options.href || "",
    dataset: Object.assign({}, options.dataset || {}),
    className: options.className || "",
    isConnected: true,
    attributes: {},
    classList: {
      add() {},
      remove() {},
      toggle() {},
    },
    addEventListener(type, callback) {
      const callbacks = handlers.get(type) || [];
      callbacks.push(callback);
      handlers.set(type, callbacks);
    },
    dispatch(type, event) {
      return (handlers.get(type) || []).map((callback) => callback(event));
    },
    setAttribute(key, value) { this.attributes[key] = String(value); },
    removeAttribute(key) { delete this.attributes[key]; },
    appendChild(child) { this.child = child; return child; },
    remove() { this.removed = true; },
    focus() {
      this.focusCount = (this.focusCount || 0) + 1;
      options.onFocus?.(this);
    },
    showModal() { this.open = true; },
    close(reason) {
      this.closed = (this.closed || 0) + 1;
      this.open = false;
      const fireClose = () => {
        for (const callback of handlers.get("close") || []) callback({ target: this, reason });
      };
      if (options.deferClose) queueMicrotask(fireClose);
      else fireClose();
    },
    contains(target) { return target === this || target.form === this || true; },
    matches(selector) {
      if (options.matches) return options.matches.call(this, selector);
      return false;
    },
    closest(selector) {
      if (options.closest) return options.closest.call(this, selector);
      return null;
    },
    querySelector(selector) {
      if (options.querySelector) return options.querySelector.call(this, selector);
      return null;
    },
    querySelectorAll(selector) {
      if (options.querySelectorAll) return options.querySelectorAll.call(this, selector);
      return [];
    },
  };
  return element;
}

const fields = {};
for (const name of ["q", "date_preset", "from", "to", "vehicle", "exclusion"]) {
  fields[name] = makeElement(name, { value: values[name] });
  fields[name].form = null;
}
fields.date_preset.type = "select-one";
fields.from.type = "date";
fields.to.type = "date";
const category = makeElement("category", { value: values.category, type: "radio", checked: true });
category.form = null;
fields.category = category;
const dateRange = makeElement("date-range", {
  hidden: fields.date_preset.value !== "custom",
  querySelectorAll(selector) { return selector === 'input[type="date"]' ? [fields.from, fields.to] : []; },
});
const secondary = makeElement("secondary", {
  querySelector(selector) { return selector === ".filter-clear" ? null : null; },
});
const form = makeElement("filter-form", {
  querySelector(selector) {
    const match = selector.match(/^\[name="([^"]+)"\]/);
    if (match) return fields[match[1]] || null;
    if (selector === "[data-archive-date-preset]") return fields.date_preset;
    if (selector === "[data-custom-date-range]") return dateRange;
    return null;
  },
  querySelectorAll(selector) {
    if (selector === 'input[name="category"]') return [category];
    if (selector === 'input[type="date"]') return [fields.from, fields.to];
    return [];
  },
});
for (const field of Object.values(fields)) field.form = form;
const filterSurface = makeElement("filter-surface", {
  dataset: { archiveFilterActive: "false" },
  querySelector(selector) {
    return selector === "[data-archive-filter-active-indicator]"
      ? makeElement("filter-indicator") : null;
  },
});
const filterToggle = makeElement("filter-toggle", {
  querySelector(selector) {
    return selector === "[data-archive-filter-active-indicator]"
      ? makeElement("filter-indicator") : null;
  },
});
const filterControls = makeElement("filter-controls");
const status = makeElement("status");
const retry = makeElement("retry");
const stateNode = makeElement("archive-state", { dataset: { archiveState: JSON.stringify(state) } });
const results = makeElement("results", {
  querySelector(selector) {
    return selector === "[data-archive-state]" ? stateNode : null;
  },
  querySelectorAll() { return []; },
  closest(selector) { return selector === "#trip-archive-results" ? this : null; },
});

const dialog = makeElement("dialog");
const mutation = makeElement("mutation", {
  closest(selector) {
    if (selector === "#trip-archive-results") return results;
    if (selector === "dialog") return dialog;
    return null;
  },
});

function makeSelectionCard(id) {
  const card = makeElement("card", { id: `trip-${id}` });
  const checkbox = makeElement("checkbox", {
    value: String(id),
    matches(selector) { return selector === ".merge-select"; },
  });
  card.querySelector = (selector) => selector === ".merge-select" ? checkbox : null;
  card.classList = { add() {}, remove() {}, toggle() {} };
  return { card, checkbox };
}
const initialSelectionRows = [1, 2, 3].map(makeSelectionCard);
const selectionCards = initialSelectionRows.map((row) => row.card);
const selectionCheckboxes = initialSelectionRows.map((row) => row.checkbox);
const paginationSelectionRow = makeSelectionCard(99);
const arrivingSelectionRow = makeSelectionCard(100);
const selectionCard = selectionCards[0];
const selectionCheckbox = selectionCheckboxes[0];
const selectionBar = makeElement("selection-bar");
const selectionCount = makeElement("selection-count");
const selectionClear = makeElement("selection-clear");
const selectionSelectAll = makeElement("selection-select-all");
const selectionActionsOpen = makeElement("selection-actions-open", {
  onFocus() { focusLog.push("actions"); },
});
const selectionMore = makeElement("selection-more");
const selectionActionControls = makeElement("selection-action-controls", {
  querySelector(selector) { return selector === ".selection-more" ? selectionMore : null; },
});
const selectionActionsMount = makeElement("selection-actions-mount");
const selectionActionsDialog = makeElement("selection-actions-dialog", {
  id: "selection-actions-dialog", deferClose: true,
});
const categoryOpen = makeElement("category-open");
const purposeOpen = makeElement("purpose-open");
const vehicleOpen = makeElement("vehicle-open");
const exclusionOpen = makeElement("exclusion-open");
const mergeOpen = makeElement("merge-open");
const mergeMinimum = makeElement("merge-minimum");
const focusLog = [];
const archiveHeader = makeElement("archive-header", {
  id: "trip-archive-header",
  onFocus() { focusLog.push("header"); },
});
const deleteSelectedOpen = makeElement("delete-selected-open", {
  onFocus() { focusLog.push("trigger"); },
});
const deleteSelectedConfirm = makeElement("delete-selected-confirm");
const deleteDialogError = makeElement("delete-dialog-error", { hidden: true });
const deleteDialogForm = makeElement("delete-dialog-form", {
  dataset: { selectionDialogConfirm: "delete-selected-confirm" },
});
const deleteSelectedDialog = makeElement("delete-selected-dialog", { id: "delete-selected-dialog", deferClose: true });
deleteSelectedDialog.querySelector = (selector) => {
  if (selector === ".selection-dialog-error") return deleteDialogError;
  if (selector === "[data-selection-dialog-confirm]") return deleteDialogForm;
  return null;
};
deleteSelectedDialog.querySelectorAll = (selector) => (
  selector === "button, input, select, textarea" ? [deleteSelectedConfirm] : []
);
const controls = {
  "selection-action-bar": selectionBar,
  "selection-count": selectionCount,
  "selection-clear": selectionClear,
  "selection-select-all": selectionSelectAll,
  "selection-actions-open": selectionActionsOpen,
  "selection-actions-dialog": selectionActionsDialog,
  "selection-action-controls": selectionActionControls,
  "selection-actions-mount": selectionActionsMount,
  "category-dialog-open": categoryOpen,
  "purpose-dialog-open": purposeOpen,
  "vehicle-dialog-open": vehicleOpen,
  "exclusion-dialog-open": exclusionOpen,
  "merge-dialog-open": mergeOpen,
  "merge-minimum": mergeMinimum,
  "delete-selected-open": deleteSelectedOpen,
  "delete-selected-dialog": deleteSelectedDialog,
  "delete-selected-confirm": deleteSelectedConfirm,
  "trip-archive-header": archiveHeader,
};
for (const id of ["category-dialog", "exclusion-dialog", "purpose-dialog", "vehicle-dialog", "merge-dialog"]) {
  controls[id] = makeElement(id, {
    id,
    querySelector() { return makeElement("dialog-child"); },
    querySelectorAll() { return []; },
  });
}
for (const id of [
  "category-dialog-confirm", "exclusion-dialog-confirm", "purpose-dialog-confirm",
  "vehicle-dialog-confirm", "merge-dialog-confirm", "vehicle-dialog-select",
  "purpose-dialog-input", "merge-dialog-category", "merge-dialog-purpose",
  "merge-dialog-notes", "merge-dialog-vehicle",
]) controls[id] = makeElement(id);

const document = {
  title: "Trips",
  body: makeElement("body"),
  addEventListener(type, callback) {
    const callbacks = listeners.get(type) || [];
    callbacks.push(callback);
    listeners.set(type, callbacks);
  },
  getElementById(id) {
    if (id === "trip-archive-results") return results;
    if (id === "trip-filter-controls") return filterControls;
    if (id === "archive-status") return status;
    if (id === "archive-status-retry") return retry;
    return controls[id] || null;
  },
  querySelector(selector) {
    if (selector === '[data-archive-filter-form]') return form;
    if (selector === '[data-archive-filter-surface]') return filterSurface;
    if (selector === '[data-archive-filter-toggle]') return filterToggle;
    if (selector === "#trip-archive-results") return results;
    if (selector === "#trip-archive-results [data-archive-state]") return stateNode;
    if (selector === '[data-archive-filter-form] .trip-filter-secondary') return secondary;
    if (selector === 'dialog[open]') return null;
    return null;
  },
  querySelectorAll(selector) {
    if (selector === ".trip-archive-item") return scenario.selection ? selectionCards : [];
    if (selector === '#trip-archive-results [data-archive-month]') return [];
    if (selector === '[data-selection-count]') return [];
    if (selector === '#trip-archive-export-links .trip-archive-export-action') return [];
    return [];
  },
  createElement() { return makeElement("created"); },
};

class FakeFormData {
  get(name) { return values[name] || ""; }
}

function emit(type, detail) {
  const event = {
    type,
    detail,
    target: detail && detail.target,
    defaultPrevented: false,
    preventDefault() { this.defaultPrevented = true; },
  };
  for (const callback of listeners.get(type) || []) callback(event);
  return event;
}

function xhr(headers = {}) {
  return {
    headers,
    aborted: false,
    getResponseHeader(name) { return this.headers[name] || null; },
    abort() { this.aborted = true; },
    addEventListener() {},
  };
}

const ArchiveState = require(process.cwd() + "/static/archive_state.js");
var window = {
  ArchiveState,
  localStorage,
  history,
  location,
  innerWidth: 1200,
  matchMedia() { return { matches: false, addEventListener() {}, addListener() {} }; },
  addEventListener() {},
  setTimeout(fn) { fn(); return 1; },
  clearTimeout() {},
  archiveSelectionBusyChanged() {},
  archiveClearSelection() {},
};
const htmx = {
  ajax(method, url, options) { requests.push({ method, url, options }); },
};
const bulkFetchCalls = [];
let resolveBulkFetch = null;
let resolveSelectionFetch = null;
let selectionFetchCount = 0;
const writeBegins = [];
const writeFinishes = [];
const announcements = [];
let writeBusy = false;
const bulkArchiveController = {
  beginWrite() {
    if (writeBusy) {
      writeBegins.push(false);
      return false;
    }
    writeBusy = true;
    writeBegins.push(true);
    return true;
  },
  finishWrite(refresh) {
    writeBusy = false;
    writeFinishes.push(refresh);
  },
  announce(message) { announcements.push(message); },
  isWriteBusy() { return writeBusy; },
  isWriteUnavailable() { return false; },
  isReadBusy() { return false; },
  selectionQuery() { return scenario.selectionQuery || ""; },
};
if ((scenario.action || "").startsWith("bulk-delete")) {
  window.archiveController = bulkArchiveController;
}
window.invalidateArchiveHistoryCache = () => {
  localStorage.removed.push("archive-history");
};

function fetch(url, options) {
  bulkFetchCalls.push({ url, options });
  if (url.startsWith("/trips/selection")) {
    selectionFetchCount += 1;
    if ((scenario.action.startsWith("selection-stale")
        && !(scenario.action === "selection-stale-newer" && selectionFetchCount > 1))
        || scenario.action === "selection-loading") {
      return new Promise((resolve) => { resolveSelectionFetch = resolve; });
    }
    if (scenario.action === "selection-retry" && selectionFetchCount === 1) {
      return Promise.resolve({ ok: false, json: async () => ({ detail: "temporary" }) });
    }
    if (scenario.action === "selection-error") {
      return Promise.resolve({ ok: false, json: async () => ({ detail: "failed" }) });
    }
    const payload = scenario.selectionPayload || { trip_ids: [1, 2, 99], count: 3 };
    return Promise.resolve({ ok: true, json: async () => payload });
  }
  if (scenario.action === "bulk-delete-network-failure") {
    return Promise.reject(new Error("network down"));
  }
  if (scenario.action === "bulk-delete-http-failure") {
    return Promise.resolve({
      ok: false,
      json: async () => ({ detail: "Server rejected deletion" }),
    });
  }
  if (scenario.action === "bulk-delete-duplicate") {
    return new Promise((resolve) => { resolveBulkFetch = resolve; });
  }
  return Promise.resolve({
    ok: true,
    json: async () => ({ deleted: scenario.deletedCount ?? 3 }),
  });
}
const context = {
  console,
  window,
  document,
  htmx,
  FormData: FakeFormData,
  URLSearchParams,
  queueMicrotask,
  setTimeout: window.setTimeout,
  clearTimeout: window.clearTimeout,
  fetch,
};
window.window = window;
vm.createContext(context);
vm.runInContext(process.argv[2], context, { filename: "trips-archive-inline.js" });
if ((scenario.action || "").startsWith("bulk-delete")) {
  window.archiveController = bulkArchiveController;
  window.invalidateArchiveHistoryCache = () => {
    localStorage.removed.push("archive-history");
  };
}
if (process.argv[3]) vm.runInContext(process.argv[3], context, { filename: "trips-selection-inline.js" });
if (process.argv[3]) vm.runInContext("window.__selection = selection;", context);

async function settle() {
  for (let index = 0; index < 8; index += 1) await Promise.resolve();
}

async function click(element) {
  const callbacks = element.dispatch("click", { target: element }) || [];
  await Promise.all(callbacks);
}

async function selectBulkTrips() {
  for (const checkbox of selectionCheckboxes) {
    checkbox.checked = true;
    emit("change", { target: checkbox });
  }
  await settle();
  await click(deleteSelectedOpen);
}

function beginMutation(path) {
  const request = xhr();
  const detail = { requestConfig: { verb: "POST", path, headers: {} }, xhr: request, elt: mutation };
  const before = emit("htmx:beforeRequest", detail);
  if (before.defaultPrevented) throw new Error("mutation unexpectedly prevented");
  return { request, detail };
}

async function finishMutation(item, { successful = true, marker = true } = {}) {
  if (marker) item.request.headers["X-Archive-Write"] = "success";
  const swap = { xhr: item.request, shouldSwap: true, isError: !successful };
  emit("htmx:beforeSwap", swap);
  const after = {
    xhr: item.request,
    successful,
    requestConfig: item.detail.requestConfig,
    elt: mutation,
  };
  emit("htmx:afterRequest", after);
  return { swap, after };
}

function completeRefresh(call, nextState = state, successful = true) {
  stateNode.dataset.archiveState = JSON.stringify(nextState);
  const refresh = xhr();
  const detail = {
    requestConfig: { verb: "GET", path: "/trips/list", headers: call.options.headers },
    xhr: refresh,
    elt: filterSurface,
  };
  const before = emit("htmx:beforeRequest", detail);
  if (before.defaultPrevented) throw new Error("refresh unexpectedly prevented");
  emit("htmx:afterRequest", {
    xhr: refresh,
    successful,
    requestConfig: detail.requestConfig,
    elt: filterSurface,
  });
}

(async () => {
  const result = {
    requests,
    localStorage,
    location,
    status,
    retry,
    results,
    selectionBar,
    dialog,
    deleteSelectedDialog,
    deleteDialogError,
    deleteSelectedConfirm,
    archiveHeader,
    focusLog,
    bulkFetchCalls,
    writeBegins,
    writeFinishes,
    announcements,
  };
  if (scenario.action === "success") {
    const item = beginMutation("/trips/1/tag");
    const outcome = await finishMutation(item);
    result.swap = outcome.swap;
    result.busyAfterWrite = window.archiveController.isWriteBusy();
    completeRefresh(requests.at(-1));
    result.busyAfterRefresh = window.archiveController.isWriteBusy();
  } else if (scenario.action === "validation") {
    const item = beginMutation("/trips/1/edit");
    const outcome = await finishMutation(item, { marker: false });
    result.swap = outcome.swap;
    result.requestCount = requests.length;
    result.busy = window.archiveController.isWriteBusy();
    result.statusText = status.textContent;
  } else if (scenario.action === "failure") {
    const item = beginMutation("/trips/1/tag");
    const outcome = await finishMutation(item, { successful: false, marker: false });
    result.swap = outcome.swap;
    result.requestCount = requests.length;
    result.busy = window.archiveController.isWriteBusy();
    result.statusText = status.textContent;
  } else if (scenario.action === "deferred-filter") {
    const item = beginMutation("/trips/1/tag");
    values.from = scenario.next.from;
    values.to = scenario.next.to;
    values.vehicle = scenario.next.vehicle;
    fields.from.value = values.from;
    fields.to.value = values.to;
    fields.vehicle.value = values.vehicle;
    emit("change", { target: fields.from });
    await finishMutation(item);
    const firstRefresh = requests.at(-1);
    completeRefresh(firstRefresh, state);
    const secondRefresh = requests.at(-1);
    result.urls = requests.map((request) => request.url);
    result.firstHeaders = firstRefresh.options.headers;
    result.secondHeaders = secondRefresh.options.headers;
    result.busy = window.archiveController.isWriteBusy();
  } else if (scenario.action === "history-before-send") {
    const item = beginMutation("/trips/1/tag");
    const historyXhr = xhr();
    historyXhr.opened = false;
    historyXhr.sent = false;
    historyXhr.open = () => { historyXhr.opened = true; };
    historyXhr.send = () => { historyXhr.sent = true; };
    historyXhr.abort = () => {
      if (!historyXhr.opened || !historyXhr.sent) throw new Error("aborted before send");
      historyXhr.aborted = true;
    };
    emit("htmx:historyCacheMiss", { path: "/old", xhr: historyXhr });
    historyXhr.open();
    historyXhr.send();
    await settle();
    result.opened = historyXhr.opened;
    result.sent = historyXhr.sent;
    result.aborted = historyXhr.aborted;
    result.writeBusy = window.archiveController.isWriteBusy();
    await finishMutation(item);
    completeRefresh(requests.at(-1));
    result.location = location.href;
  } else if (scenario.action === "navigation-during-write") {
    const item = beginMutation("/trips/1/delete");
    emit("htmx:historyRestore", { path: "/trips?from=2025-01-01", cacheMiss: false });
    await finishMutation(item);
    completeRefresh(requests.at(-1));
    result.location = location.href;
    result.requestCount = requests.length;
  } else if (scenario.action === "selected-delete") {
    const checkbox = selectionCheckbox;
    checkbox.checked = true;
    emit("change", { target: checkbox });
    const item = beginMutation("/trips/1/delete");
    await finishMutation(item);
    result.selectionHidden = selectionBar.hidden;
    result.dialogClosed = dialog.closed || 0;
    result.requestCount = requests.length;
  } else if ((scenario.action || "").startsWith("bulk-delete")) {
    await selectBulkTrips();
    const first = click(deleteSelectedConfirm);
    await settle();
    result.submittedIds = Array.from(
      bulkFetchCalls[0].options.body.getAll("trip_ids"),
      (id) => Number(id),
    );
    if (scenario.action === "bulk-delete-duplicate") {
      await click(deleteSelectedConfirm);
      result.afterDuplicate = {
        fetchCount: bulkFetchCalls.length,
        selection: Array.from(window.__selection),
        dialogOpen: deleteSelectedDialog.open,
        writeBegins: writeBegins.slice(),
        writeFinishes: writeFinishes.slice(),
      };
      resolveBulkFetch({
        ok: true,
        json: async () => ({ deleted: 3 }),
      });
    }
    await first;
    await settle();
    result.selection = Array.from(window.__selection);
    result.selectionHidden = selectionBar.hidden;
    result.dialogOpen = deleteSelectedDialog.open;
    result.dialogClosed = deleteSelectedDialog.closed || 0;
    result.errorText = deleteDialogError.textContent;
    result.errorHidden = deleteDialogError.hidden;
    result.confirmDisabled = deleteSelectedConfirm.disabled;
    result.writeBusy = writeBusy;
  } else if (scenario.action === "selection-read-queued") {
    values.vehicle = "7";
    fields.vehicle.value = "7";
    emit("change", { target: fields.vehicle });
    result.selectDisabled = selectionSelectAll.disabled;
    await click(selectionSelectAll);
    result.selectionFetchCount = bulkFetchCalls.length;
  } else if (scenario.action === "selection-loading") {
    const pending = selectionSelectAll.dispatch("click", { target: selectionSelectAll })[0];
    await settle();
    selectionCheckbox.checked = true;
    const attemptedChange = emit("change", { target: selectionCheckbox });
    const inlineWrite = xhr();
    const inlineWriteAttempt = emit("htmx:beforeRequest", {
      requestConfig: { verb: "POST", path: "/trips/1/tag", headers: {} },
      xhr: inlineWrite,
      elt: mutation,
    });
    result.loading = {
      selectDisabled: selectionSelectAll.disabled,
      checkboxDisabled: selectionCheckbox.disabled,
      checkboxChecked: selectionCheckbox.checked,
      changePrevented: attemptedChange.defaultPrevented,
      categoryDisabled: categoryOpen.disabled,
      deleteDisabled: deleteSelectedOpen.disabled,
      inlineWritePrevented: inlineWriteAttempt.defaultPrevented,
    };
    window.archiveClearSelection();
    resolveSelectionFetch({ ok: true, json: async () => ({ trip_ids: [1], count: 1 }) });
    await pending;
    result.selection = Array.from(window.__selection);
  } else if ((scenario.action || "").startsWith("selection-stale")) {
    selectionCheckbox.checked = true;
    emit("change", { target: selectionCheckbox });
    const pending = selectionSelectAll.dispatch("click", { target: selectionSelectAll })[0];
    await settle();
    if (scenario.action === "selection-stale-filter") {
      values.vehicle = "7";
      fields.vehicle.value = "7";
      emit("change", { target: fields.vehicle });
    } else if (scenario.action === "selection-stale-history") {
      emit("htmx:historyRestore", { path: "/trips?q=other", cacheMiss: false });
    } else if (scenario.action === "selection-stale-newer") {
      window.archiveClearSelection();
      await click(selectionSelectAll);
    } else {
      window.archiveClearSelection();
    }
    resolveSelectionFetch({ ok: true, json: async () => ({ trip_ids: [2, 99], count: 2 }) });
    await pending;
    await settle();
    result.selection = Array.from(window.__selection);
    result.selectionHidden = selectionBar.hidden;
  } else if ((scenario.action || "").startsWith("selection-")) {
    if (scenario.priorSelection) {
      selectionCheckbox.checked = true;
      emit("change", { target: selectionCheckbox });
    }
    await click(selectionSelectAll);
    await settle();
    if (scenario.action === "selection-retry") {
      result.firstAnnouncement = status.textContent;
      await click(selectionSelectAll);
      await settle();
    }
    if (scenario.action === "selection-deselect") {
      selectionCheckbox.checked = false;
      emit("change", { target: selectionCheckbox });
    }
    if (scenario.action === "selection-pagination") {
      selectionCards.push(paginationSelectionRow.card, arrivingSelectionRow.card);
      emit("htmx:afterSettle", {});
      result.paginationChecks = {
        snapshotChecked: paginationSelectionRow.checkbox.checked,
        arrivingChecked: arrivingSelectionRow.checkbox.checked,
      };
    }
    if (scenario.action === "selection-delete") {
      await click(deleteSelectedOpen);
      await click(deleteSelectedConfirm);
      await settle();
      result.submittedIds = Array.from(
        bulkFetchCalls[1].options.body.getAll("trip_ids"), (id) => Number(id),
      );
    }
    result.selection = Array.from(window.__selection);
    result.selectionHidden = selectionBar.hidden;
    result.selectionCount = selectionCount.textContent;
    result.checkboxStates = selectionCheckboxes.map((checkbox) => ({
      checked: checkbox.checked, disabled: checkbox.disabled,
    }));
    result.statusText = status.textContent;
    result.selectDisabled = selectionSelectAll.disabled;
    result.fetches = bulkFetchCalls.map((call) => ({
      url: call.url,
      method: call.options.method,
      cache: call.options.cache,
    }));
  } else if ((scenario.action || "").startsWith("sheet-")) {
    selectionCheckbox.checked = true;
    emit("change", { target: selectionCheckbox });
    await click(selectionActionsOpen);
    result.sheetOpened = selectionActionsDialog.open;
    result.controlsMounted = selectionActionsMount.child === selectionActionControls;
    if (scenario.action === "sheet-transition") {
      await click(categoryOpen);
      await settle();
      result.sheetOpenAfterAction = selectionActionsDialog.open;
      result.actionDialogOpen = controls["category-dialog"].open;
      controls["category-dialog"].close();
    } else if (scenario.action === "sheet-stale-transition") {
      categoryOpen.dispatch("click", { target: categoryOpen });
      window.archiveSelectionNavigationStarted();
      await settle();
      result.sheetOpenAfterAction = selectionActionsDialog.open;
      result.actionDialogOpen = Boolean(controls["category-dialog"].open);
    } else {
      selectionActionsDialog.close('escape');
    }
    await settle();
    result.controlsRestored = selectionBar.child === selectionActionControls;
    result.focusLog = focusLog.slice();
  }
  process.stdout.write(JSON.stringify(result));
})().catch((error) => {
  process.stderr.write(error.stack || String(error));
  process.exitCode = 1;
});
"""


def _run(action, **extra):
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed; install Node to run the archive controller harness")
    archive, selection = _inline_scripts()
    needs_selection = (
        action == "selected-delete" or action.startswith("bulk-delete")
        or action.startswith("selection-") or action.startswith("sheet-")
    )
    args = [
        node,
        "-e",
        HARNESS,
        json.dumps({"action": action, **extra}),
        archive,
        selection if needs_selection else "",
    ]
    try:
        result = subprocess.run(
            args, cwd=ROOT, text=True, capture_output=True, check=False, timeout=30,
        )
    except subprocess.TimeoutExpired as exc:
        pytest.fail(f"archive controller Node harness timed out: {exc}")
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_inline_controller_refreshes_only_after_marked_write():
    result = _run("success")
    assert result["swap"]["shouldSwap"] is False
    assert result["swap"]["isError"] is False
    assert result["requests"][-1]["method"] == "GET"
    assert result["busyAfterWrite"] is True
    assert result["busyAfterRefresh"] is False


def test_inline_controller_keeps_validation_html_and_releases_write():
    result = _run("validation")
    assert result["swap"]["shouldSwap"] is True
    assert result["requestCount"] == 0
    assert result["busy"] is False
    assert result["statusText"] == ""


def test_inline_controller_reports_transport_failure_without_refresh():
    result = _run("failure")
    assert result["swap"]["shouldSwap"] is True
    assert result["requestCount"] == 0
    assert result["busy"] is False
    assert result["statusText"] == "The update could not be saved."


def test_inline_controller_defers_date_and_vehicle_filter_until_write_refresh():
    result = _run(
        "deferred-filter",
        values={"from": "2025-01-01", "to": "2025-01-31", "vehicle": "1"},
        next={"from": "2025-02-01", "to": "2025-02-28", "vehicle": "2"},
    )
    assert "from=2025-01-01" in result["urls"][0]
    assert "loaded_depth=" in result["urls"][0]
    assert "from=2025-02-01" in result["urls"][1]
    assert "vehicle=2" in result["urls"][1]
    assert "loaded_depth=" not in result["urls"][1]
    assert result["busy"] is False


def test_inline_controller_defers_history_miss_abort_until_after_send():
    result = _run("history-before-send")
    assert result["opened"] is True
    assert result["sent"] is True
    assert result["aborted"] is True
    assert result["writeBusy"] is True
    assert result["location"] == "/old"


def test_inline_controller_defers_history_navigation_until_refresh():
    result = _run("navigation-during-write")
    assert result["location"] == "/trips?from=2025-01-01"
    assert result["requestCount"] == 1


def test_selected_delete_prunes_selection_and_closes_dialog_before_refresh():
    result = _run("selected-delete")
    assert result["selectionHidden"] is True
    assert result["dialogClosed"] == 1
    assert result["requestCount"] == 1


def test_selected_delete_posts_explicit_ids_and_focuses_archive_header_after_success():
    result = _run("bulk-delete-success")
    assert result["submittedIds"] == [1, 2, 3]
    assert result["selection"] == []
    assert result["selectionHidden"] is True
    assert result["dialogOpen"] is False
    assert result["dialogClosed"] == 1
    assert result["localStorage"]["removed"] == ["archive-history"]
    assert result["writeBegins"] == [True]
    assert result["writeFinishes"] == [True]
    assert result["writeBusy"] is False
    assert result["focusLog"]
    assert all(target == "header" for target in result["focusLog"])
    assert result["deleteSelectedConfirm"]["disabled"] is False


def test_selected_delete_http_failure_keeps_selection_and_dialog_open():
    result = _run("bulk-delete-http-failure")
    assert result["submittedIds"] == [1, 2, 3]
    assert result["selection"] == [1, 2, 3]
    assert result["dialogOpen"] is True
    assert result["dialogClosed"] == 0
    assert result["errorText"] == "Server rejected deletion"
    assert result["errorHidden"] is False
    assert result["localStorage"]["removed"] == []
    assert result["writeBegins"] == [True]
    assert result["writeFinishes"] == [False]
    assert result["writeBusy"] is False


def test_selected_delete_network_failure_keeps_selection_and_dialog_open():
    result = _run("bulk-delete-network-failure")
    assert result["submittedIds"] == [1, 2, 3]
    assert result["selection"] == [1, 2, 3]
    assert result["dialogOpen"] is True
    assert result["dialogClosed"] == 0
    assert result["errorText"] == "Deletion failed."
    assert result["errorHidden"] is False
    assert result["localStorage"]["removed"] == []
    assert result["writeBegins"] == [True]
    assert result["writeFinishes"] == [False]
    assert result["writeBusy"] is False


def test_selected_delete_duplicate_submit_is_blocked_while_request_is_in_flight():
    result = _run("bulk-delete-duplicate")
    assert result["submittedIds"] == [1, 2, 3]
    assert result["afterDuplicate"]["fetchCount"] == 1
    assert result["afterDuplicate"]["selection"] == [1, 2, 3]
    assert result["afterDuplicate"]["dialogOpen"] is True
    assert result["afterDuplicate"]["writeBegins"] == [True, False]
    assert result["afterDuplicate"]["writeFinishes"] == []
    assert result["selection"] == []
    assert result["dialogOpen"] is False
    assert result["writeFinishes"] == [True]


def test_select_all_matching_fetches_the_canonical_applied_snapshot_without_cache():
    result = _run(
        "selection-success",
        selection=True,
        values={
            "q": "airport",
            "category": "business",
            "date_preset": "this_month",
            "from": "2026-09-01",
            "to": "2026-09-30",
            "vehicle": "7",
        },
    )

    assert result["fetches"] == [{
        "url": (
            "/trips/selection?q=airport&category=business&from=2026-09-01"
            "&to=2026-09-30&vehicle=7"
        ),
        "method": "GET",
        "cache": "no-store",
    }]
    assert result["selection"] == [1, 2, 99]
    assert result["selectionCount"] == "3 trips selected (1 outside this view)"
    assert result["selectionHidden"] is False
    assert [item["checked"] for item in result["checkboxStates"]] == [True, True, False]
    assert result["selectDisabled"] is False


def test_empty_matching_snapshot_clears_the_previous_selection_cleanly():
    result = _run(
        "selection-empty", selection=True, priorSelection=True,
        selectionPayload={"trip_ids": [], "count": 0},
    )

    assert result["selection"] == []
    assert result["selectionHidden"] is True
    assert result["statusText"] == "No matching trips to select"


@pytest.mark.parametrize(
    ("action", "payload"),
    [
        ("selection-error", None),
        ("selection-malformed", {"trip_ids": [2, 99], "count": 1}),
    ],
)
def test_failed_or_malformed_matching_snapshot_preserves_the_previous_selection(action, payload):
    kwargs = {"selection": True, "priorSelection": True}
    if payload is not None:
        kwargs["selectionPayload"] = payload
    result = _run(action, **kwargs)

    assert result["selection"] == [1]
    assert result["selectionCount"] == "1 trip selected"
    assert result["statusText"] == "Matching trips could not be selected. Try again."
    assert result["selectDisabled"] is False


def test_matching_snapshot_can_retry_after_a_transport_failure():
    result = _run("selection-retry", selection=True, priorSelection=True)

    assert result["firstAnnouncement"] == "Matching trips could not be selected. Try again."
    assert result["selection"] == [1, 2, 99]
    assert len(result["fetches"]) == 2
    assert result["statusText"] == "3 trips selected"


@pytest.mark.parametrize(
    "action",
    [
        "selection-stale-filter", "selection-stale-history",
        "selection-stale-clear", "selection-stale-newer",
    ],
)
def test_late_matching_snapshot_cannot_restore_selection_after_navigation_or_clear(action):
    result = _run(action, selection=True)

    expected = [1, 2, 99] if action == "selection-stale-newer" else []
    assert result["selection"] == expected
    assert result["selectionHidden"] is (not expected)


def test_loading_snapshot_blocks_row_changes_and_write_entry_points_until_cancelled():
    result = _run("selection-loading", selection=True)

    assert result["loading"] == {
        "selectDisabled": True,
        "checkboxDisabled": True,
        "checkboxChecked": False,
        "changePrevented": True,
        "categoryDisabled": True,
        "deleteDisabled": True,
        "inlineWritePrevented": True,
    }
    assert result["selection"] == []


def test_queued_filter_request_blocks_snapshotting_the_previous_applied_query():
    result = _run("selection-read-queued", selection=True)

    assert result["selectDisabled"] is True
    assert result["selectionFetchCount"] == 0


def test_visible_deselection_updates_total_while_unloaded_ids_remain_selected():
    result = _run("selection-deselect", selection=True)

    assert result["selection"] == [2, 99]
    assert result["selectionCount"] == "2 trips selected (1 outside this view)"
    assert [item["checked"] for item in result["checkboxStates"]] == [False, True, False]


def test_pagination_reconciles_snapshot_ids_without_selecting_new_arrivals():
    result = _run("selection-pagination", selection=True)

    assert result["paginationChecks"] == {
        "snapshotChecked": True,
        "arrivingChecked": False,
    }
    assert result["selection"] == [1, 2, 99]
    assert result["selectionCount"] == "3 trips selected"


def test_snapshot_selection_posts_every_explicit_id_and_clears_only_after_delete_success():
    result = _run("selection-delete", selection=True)

    assert result["submittedIds"] == [1, 2, 99]
    assert result["selection"] == []
    assert result["selectionHidden"] is True


def test_mobile_action_sheet_restores_focus_and_reuses_the_existing_action_dialog():
    result = _run("sheet-transition", selection=True)

    assert result["sheetOpened"] is True
    assert result["controlsMounted"] is True
    assert result["sheetOpenAfterAction"] is False
    assert result["actionDialogOpen"] is True
    assert result["controlsRestored"] is True
    assert result["focusLog"][-1] == "actions"


def test_mobile_action_sheet_escape_close_restores_controls_and_trigger_focus():
    result = _run("sheet-close", selection=True)

    assert result["sheetOpened"] is True
    assert result["controlsRestored"] is True
    assert result["focusLog"][-1] == "actions"


def test_navigation_cancels_a_queued_sheet_to_action_dialog_transition():
    result = _run("sheet-stale-transition", selection=True)

    assert result["sheetOpenAfterAction"] is False
    assert result["actionDialogOpen"] is False
    assert result["controlsRestored"] is True
    assert result["focusLog"][-1] == "header"
