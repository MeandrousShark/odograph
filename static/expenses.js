// Expense ledger page: tax-treatment defaults, live warnings for a linked
// trip, and the inline editor rows. The rules are plain functions so they can
// be exercised without a browser; init() binds them to the page. It loads as
// a classic browser script and as a CommonJS module, so the same file runs in
// both places with no build step.
(() => {
  "use strict";

  // "other" has no sensible default, so the user must choose a treatment.
  const defaultTreatment = (category) => {
    if (category === "other") return "";
    return (category === "parking" || category === "tolls")
      ? "fully_business" : "business_use_allocated";
  };

  // Which live warnings apply for the selected trip option (or none).
  const activeWarnings = (option, vehicleId, incurredOn) => {
    const linkedTrip = Boolean(option && option.value !== "");
    return {
      vehicle: linkedTrip && option.dataset.tripVehicleId !== vehicleId,
      date: linkedTrip && option.dataset.tripDate !== incurredOn,
      "not-my-vehicle": linkedTrip && option.dataset.tripExclusion === "not_my_vehicle",
    };
  };

  // Warning copy comes from the form's data-expense-warning-* attributes.
  const warningLabelKey = (code) => {
    const key = code === "not-my-vehicle" ? "notMyVehicle" : code;
    return "expenseWarning" + key[0].toUpperCase() + key.slice(1);
  };

  const updateExpenseWarnings = (form) => {
    const tripSelect = form.querySelector('[name="trip_id"]');
    const vehicleSelect = form.querySelector('[name="vehicle_id"]');
    const dateInput = form.querySelector('[name="incurred_on"]');
    const option = tripSelect && tripSelect.options[tripSelect.selectedIndex];
    const active = activeWarnings(option, vehicleSelect.value, dateInput.value);
    form.querySelectorAll("[data-expense-warning]").forEach((warning) => {
      const code = warning.dataset.expenseWarning;
      warning.hidden = !active[code];
      if (active[code]) warning.textContent = form.dataset[warningLabelKey(code)];
    });
  };

  const WARNING_FIELDS = '[data-expense-form] [name="trip_id"], '
    + '[data-expense-form] [name="vehicle_id"], [data-expense-form] [name="incurred_on"]';

  const init = (doc) => {
    doc.addEventListener("change", (event) => {
      if (event.target.matches("[data-expense-treatment-select]")) {
        const treatment = event.target.closest("form").querySelector('[name="treatment"]');
        treatment.value = defaultTreatment(event.target.value);
      }
      if (event.target.matches(WARNING_FIELDS)) {
        updateExpenseWarnings(event.target.closest("[data-expense-form]"));
      }
    });
    doc.querySelectorAll("[data-expense-editor-toggle]").forEach((disclosure) => {
      const panel = doc.getElementById(disclosure.dataset.expenseEditorToggle);
      if (!panel) return;
      const syncPanel = () => {
        panel.hidden = !disclosure.open;
      };
      disclosure.addEventListener("toggle", syncPanel);
      syncPanel();
    });
    doc.querySelectorAll("[data-expense-form]").forEach(updateExpenseWarnings);
  };

  const ExpensesPage = {
    defaultTreatment,
    activeWarnings,
    updateExpenseWarnings,
    init,
  };

  if (typeof window !== "undefined") {
    window.ExpensesPage = ExpensesPage;
    // Loaded from <head>, so wait for the ledger markup.
    if (document.readyState === "loading") {
      document.addEventListener("DOMContentLoaded", () => init(document));
    } else {
      init(document);
    }
  }
  if (typeof module !== "undefined" && module.exports) module.exports = ExpensesPage;
})();
