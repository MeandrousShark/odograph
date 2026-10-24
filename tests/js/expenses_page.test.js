// Behavioral tests for static/expenses.js, run by Node's built-in test
// runner. The module loads the same way the browser loads it (a classic
// script with a guarded CommonJS export), and init() runs against a small
// fake DOM shaped like expenses.html's add form and editor rows, so these
// exercise the real handlers rather than a copy of them.
const test = require("node:test");
const assert = require("node:assert");
const path = require("node:path");

const ExpensesPage = require(
  path.join(__dirname, "..", "..", "static", "expenses.js")
);

const LABELS = {
  expenseWarningVehicle: "The expense vehicle does not match the linked trip vehicle.",
  expenseWarningDate: "The expense date does not match the linked trip's local date.",
  expenseWarningNotMyVehicle: "The linked trip is marked not my vehicle.",
};

function makeListeners() {
  const listeners = new Map();
  return {
    addEventListener(type, callback) {
      const callbacks = listeners.get(type) || [];
      callbacks.push(callback);
      listeners.set(type, callbacks);
    },
    fire(type, properties = {}) {
      const event = { type, ...properties };
      for (const callback of listeners.get(type) || []) callback(event);
      return event;
    },
    count(type) {
      return (listeners.get(type) || []).length;
    },
  };
}

function option(value, dataset = {}) {
  return { value, dataset };
}

// A form control the document-level change handler can be pointed at. The
// selectors are the ones static/expenses.js matches against.
function control(form, name, value, extra = {}) {
  const base = {
    name,
    value,
    form,
    treatmentSelect: false,
    matches(selector) {
      if (selector === "[data-expense-treatment-select]") return this.treatmentSelect;
      return selector.split(", ").some((part) => part === `[data-expense-form] [name="${this.name}"]`);
    },
    closest(selector) {
      if (selector === "form" || selector === "[data-expense-form]") return this.form;
      return null;
    },
  };
  // Descriptors rather than a spread, so getters stay live.
  return Object.defineProperties(base, Object.getOwnPropertyDescriptors(extra));
}

function warning(code) {
  return { dataset: { expenseWarning: code }, hidden: true, textContent: "" };
}

function makeForm({ vehicleId = "1", date = "2026-06-15", trips = [], selectedTrip = "" } = {}) {
  const form = { dataset: { ...LABELS } };
  const options = [option(""), ...trips];
  const tripSelect = control(form, "trip_id", selectedTrip, {
    options,
    get selectedIndex() {
      return Math.max(0, options.findIndex((candidate) => candidate.value === this.value));
    },
  });
  const fields = {
    vehicle_id: control(form, "vehicle_id", vehicleId),
    incurred_on: control(form, "incurred_on", date),
    trip_id: tripSelect,
    category: control(form, "category", "fuel"),
    treatment: control(form, "treatment", "business_use_allocated"),
  };
  fields.category.treatmentSelect = true;
  const warnings = [warning("vehicle"), warning("date"), warning("not-my-vehicle")];
  form.fields = fields;
  form.warnings = Object.fromEntries(warnings.map((item) => [item.dataset.expenseWarning, item]));
  form.querySelector = (selector) => {
    const match = selector.match(/^\[name="([^"]+)"\]$/);
    return match ? fields[match[1]] || null : null;
  };
  form.querySelectorAll = (selector) => (selector === "[data-expense-warning]" ? warnings : []);
  return form;
}

function makeEditor(id, open = false) {
  const disclosure = {
    open,
    dataset: { expenseEditorToggle: id },
    ...makeListeners(),
  };
  const panel = { id, hidden: true };
  return { disclosure, panel };
}

function makeDocument({ forms = [], editors = [], orphans = [] } = {}) {
  const doc = makeListeners();
  doc.querySelectorAll = (selector) => {
    if (selector === "[data-expense-form]") return forms;
    if (selector === "[data-expense-editor-toggle]") {
      return [...editors.map((editor) => editor.disclosure), ...orphans];
    }
    return [];
  };
  doc.getElementById = (id) => {
    const editor = editors.find((candidate) => candidate.panel.id === id);
    return editor ? editor.panel : null;
  };
  return doc;
}

const TRIP = option("42", { tripVehicleId: "1", tripDate: "2026-06-15", tripExclusion: "" });

test("each category defaults the treatment the page used before", () => {
  assert.strictEqual(ExpensesPage.defaultTreatment("parking"), "fully_business");
  assert.strictEqual(ExpensesPage.defaultTreatment("tolls"), "fully_business");
  assert.strictEqual(ExpensesPage.defaultTreatment("fuel"), "business_use_allocated");
  assert.strictEqual(ExpensesPage.defaultTreatment("depreciation"), "business_use_allocated");
  assert.strictEqual(ExpensesPage.defaultTreatment("other"), "");
});

test("changing the category resets the treatment, and other clears it", () => {
  const form = makeForm();
  const doc = makeDocument({ forms: [form] });
  ExpensesPage.init(doc);
  const category = form.fields.category;

  category.value = "tolls";
  doc.fire("change", { target: category });
  assert.strictEqual(form.fields.treatment.value, "fully_business");

  category.value = "other";
  doc.fire("change", { target: category });
  assert.strictEqual(form.fields.treatment.value, "");

  category.value = "fuel";
  doc.fire("change", { target: category });
  assert.strictEqual(form.fields.treatment.value, "business_use_allocated");
});

test("an unlinked expense shows no warnings", () => {
  assert.deepStrictEqual(ExpensesPage.activeWarnings(option(""), "1", "2026-06-15"), {
    vehicle: false, date: false, "not-my-vehicle": false,
  });
  assert.deepStrictEqual(ExpensesPage.activeWarnings(undefined, "1", "2026-06-15"), {
    vehicle: false, date: false, "not-my-vehicle": false,
  });
});

test("a matching linked trip shows no warnings", () => {
  assert.deepStrictEqual(ExpensesPage.activeWarnings(TRIP, "1", "2026-06-15"), {
    vehicle: false, date: false, "not-my-vehicle": false,
  });
});

test("each mismatch with the linked trip raises its own warning", () => {
  const notMine = option("43", { tripVehicleId: "2", tripDate: "2026-06-14", tripExclusion: "not_my_vehicle" });
  assert.deepStrictEqual(ExpensesPage.activeWarnings(notMine, "1", "2026-06-15"), {
    vehicle: true, date: true, "not-my-vehicle": true,
  });
});

test("init shows rendered warnings with the form's own copy", () => {
  const mismatched = option("43", { tripVehicleId: "2", tripDate: "2026-06-15", tripExclusion: "not_my_vehicle" });
  const form = makeForm({ trips: [mismatched], selectedTrip: "43" });
  ExpensesPage.init(makeDocument({ forms: [form] }));

  assert.strictEqual(form.warnings.vehicle.hidden, false);
  assert.strictEqual(form.warnings.vehicle.textContent, LABELS.expenseWarningVehicle);
  assert.strictEqual(form.warnings.date.hidden, true);
  assert.strictEqual(form.warnings["not-my-vehicle"].hidden, false);
  assert.strictEqual(form.warnings["not-my-vehicle"].textContent, LABELS.expenseWarningNotMyVehicle);
});

test("editing the date, vehicle or trip updates that form's warnings", () => {
  const form = makeForm({ trips: [TRIP], selectedTrip: "42" });
  const other = makeForm({ trips: [TRIP], selectedTrip: "42" });
  const doc = makeDocument({ forms: [form, other] });
  ExpensesPage.init(doc);
  assert.strictEqual(form.warnings.date.hidden, true);

  form.fields.incurred_on.value = "2026-06-16";
  doc.fire("change", { target: form.fields.incurred_on });
  assert.strictEqual(form.warnings.date.hidden, false);
  assert.strictEqual(form.warnings.date.textContent, LABELS.expenseWarningDate);
  assert.strictEqual(other.warnings.date.hidden, true);

  form.fields.vehicle_id.value = "2";
  doc.fire("change", { target: form.fields.vehicle_id });
  assert.strictEqual(form.warnings.vehicle.hidden, false);

  form.fields.trip_id.value = "";
  doc.fire("change", { target: form.fields.trip_id });
  assert.strictEqual(form.warnings.vehicle.hidden, true);
  assert.strictEqual(form.warnings.date.hidden, true);
});

test("a change outside the watched fields leaves warnings alone", () => {
  const form = makeForm({ trips: [TRIP], selectedTrip: "42" });
  const doc = makeDocument({ forms: [form] });
  ExpensesPage.init(doc);
  form.fields.incurred_on.value = "2026-06-16";

  doc.fire("change", { target: form.fields.treatment });
  assert.strictEqual(form.warnings.date.hidden, true);
});

test("each editor row follows its Edit expense disclosure", () => {
  const first = makeEditor("expense-editor-4");
  const second = makeEditor("expense-editor-5", true);
  const orphan = makeEditor("expense-editor-6");
  const doc = makeDocument({ editors: [first, second], orphans: [orphan.disclosure] });
  ExpensesPage.init(doc);

  assert.strictEqual(first.panel.hidden, true);
  assert.strictEqual(second.panel.hidden, false);
  // No panel to drive, so nothing is bound.
  assert.strictEqual(orphan.disclosure.count("toggle"), 0);

  first.disclosure.open = true;
  first.disclosure.fire("toggle");
  assert.strictEqual(first.panel.hidden, false);
  assert.strictEqual(second.panel.hidden, false);

  first.disclosure.open = false;
  first.disclosure.fire("toggle");
  assert.strictEqual(first.panel.hidden, true);
});
