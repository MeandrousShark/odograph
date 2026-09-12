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

const selectionCards = [1, 2, 3].map((id) => makeElement("card", { id: `trip-${id}` }));
const selectionCheckboxes = selectionCards.map((card) => {
  const checkbox = makeElement("checkbox", {
    value: card.id.slice("trip-".length),
    matches(selector) { return selector === ".merge-select"; },
  });
  card.querySelector = (selector) => selector === ".merge-select" ? checkbox : null;
  card.classList = { add() {}, remove() {}, toggle() {} };
  return checkbox;
});
const selectionCard = selectionCards[0];
const selectionCheckbox = selectionCheckboxes[0];
const selectionBar = makeElement("selection-bar");
const selectionCount = makeElement("selection-count");
const selectionClear = makeElement("selection-clear");
const selectionSelectAll = makeElement("selection-select-all");
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
};
if ((scenario.action || "").startsWith("bulk-delete")) {
  window.archiveController = bulkArchiveController;
}
window.invalidateArchiveHistoryCache = () => {
  localStorage.removed.push("archive-history");
};

function fetch(url, options) {
  bulkFetchCalls.push({ url, options });
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
    args = [
        node,
        "-e",
        HARNESS,
        json.dumps({"action": action, **extra}),
        archive,
        selection if action == "selected-delete" or action.startswith("bulk-delete") else "",
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
