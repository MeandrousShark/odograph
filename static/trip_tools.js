// Advanced trip tools on the trip detail page.
//
// One toolbar opens at most one tool at a time. The rules (which tool a click
// leaves active, which naming form a map click fills, whether an htmx request
// succeeded) are plain functions so they can be exercised without a browser;
// init() binds them to the page. It loads as a classic browser script and as
// a CommonJS module, so the same file runs in both places with no build step.
(() => {
  "use strict";

  // Activating the active tool closes it; any other tool replaces it.
  const nextActiveTool = (active, clicked) => (active === clicked ? "" : clicked);

  // A map click fills a naming form only while that naming tool is active.
  const NAMING_FIELDS = {
    "name-start": { lat: "start-lat", lon: "start-lon" },
    "name-end": { lat: "end-lat", lon: "end-lon" },
  };
  const namingFieldIds = (active) =>
    (Object.prototype.hasOwnProperty.call(NAMING_FIELDS, active) ? NAMING_FIELDS[active] : null);

  // htmx 1.9 follows HX-Redirect before it records detail.successful, so a
  // 204 + HX-Redirect reaches htmx:afterRequest with successful unset. Read
  // the response instead. A send error, abort or timeout has status 0 and no
  // headers.
  const requestSucceeded = (xhr) => {
    if (!xhr) return false;
    if (xhr.status >= 200 && xhr.status < 300) return true;
    return Boolean(typeof xhr.getResponseHeader === "function" && xhr.getResponseHeader("HX-Redirect"));
  };

  const bindMerge = (form, status, view) => {
    if (!form || !status) return;
    const controls = () => Array.prototype.slice.call(form.querySelectorAll("input, select, button"));
    // htmx re-enables everything it disabled when a request ends, including a
    // control rendered disabled, such as a recent-purpose picker with no
    // choices. Remember those so an unlock restores the rendered state.
    const renderedDisabled = new Set(controls().filter((control) => control.disabled));
    const unlock = () => {
      status.textContent = "";
      controls().forEach((control) => { control.disabled = renderedDisabled.has(control); });
    };

    // The direction buttons are type="button", so only a stray implicit
    // submission could reach this; it must not navigate.
    form.addEventListener("submit", (event) => event.preventDefault());
    form.addEventListener("htmx:beforeRequest", () => {
      status.textContent = "Processing...";
    });
    form.addEventListener("htmx:afterRequest", (event) => {
      if (requestSucceeded(event.detail && event.detail.xhr)) {
        // htmx re-enables hx-disabled-elt before this event, so lock the
        // form again while the redirect loads.
        controls().forEach((control) => { control.disabled = true; });
        return;
      }
      unlock();
      // Disabling the clicked button dropped its focus.
      const button = event.detail && event.detail.elt;
      if (button && typeof button.focus === "function") button.focus();
    });
    // Back from the merged trip can restore this page from the back/forward
    // cache with the form still locked for the redirect.
    if (view) {
      view.addEventListener("pageshow", (event) => {
        if (event.persisted) unlock();
      });
    }
  };

  const init = (root, doc) => {
    if (!root) return null;
    const listeners = [];
    const buttons = Array.prototype.slice.call(root.querySelectorAll("[data-tool]"));
    const active = () => root.getAttribute("data-active-tool") || "";

    const setActive = (tool) => {
      const previous = active();
      const next = tool || "";
      root.setAttribute("data-active-tool", next);
      buttons.forEach((button) => {
        const on = button.getAttribute("data-tool") === next;
        button.setAttribute("aria-expanded", on ? "true" : "false");
        const panel = doc.getElementById(button.getAttribute("aria-controls"));
        if (panel) panel.hidden = !on;
      });
      if (previous !== next) listeners.forEach((listener) => listener(next, previous));
    };

    buttons.forEach((button) => {
      button.addEventListener("click", () => {
        setActive(nextActiveTool(active(), button.getAttribute("data-tool")));
      });
    });

    const disclosure = root.closest("details");
    if (disclosure) {
      disclosure.addEventListener("toggle", () => {
        if (!disclosure.open) setActive("");
      });
    }

    bindMerge(doc.getElementById("merge-form"), doc.getElementById("merge-status"), doc.defaultView);

    return {
      active,
      setActive,
      onChange(listener) {
        listeners.push(listener);
      },
    };
  };

  const TripTools = {
    nextActiveTool,
    namingFieldIds,
    requestSucceeded,
    init,
  };

  if (typeof window !== "undefined") window.TripTools = TripTools;
  if (typeof module !== "undefined" && module.exports) module.exports = TripTools;
})();
