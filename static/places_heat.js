// Places heatmap: fetches /places/heat for the chosen mode, dates and
// filters and draws the cells with Leaflet.heat. The query, weight and
// option rules are plain functions so they can be exercised without a
// browser; init() binds them to the page. It loads as a classic browser
// script and as a CommonJS module, so the same file runs in both places with
// no build step.
(() => {
  "use strict";

  // Leaflet.heat 1.0.0 sums points into (radius + blur) / 2 px bins and
  // scales each bin by 1 / 2^(maxZoom - zoom), with maxZoom defaulting to
  // the map's 19, which fades a city view to minOpacity. maxZoom 10 leaves
  // city zooms undamped, and 15 px bins merge nearby 50 m cells into a spot
  // that reads at zoom 9 to 13. Weights arrive scaled to 0..1, hence max 1.
  // minOpacity keeps a single visit visible beside a busy home. Tuned by eye
  // on seeded data in both themes.
  const HEAT_OPTIONS = {
    radius: 18,
    blur: 12,
    maxZoom: 10,
    max: 1,
    minOpacity: 0.4,
    // Canvas colours, not CSS variables: the dark theme inverts only the
    // tile pane, so these read the same on both themes' tiles. The first
    // stop sits at minOpacity so the faintest cells are blue, not grey.
    gradient: {
      0.3: "#2b6cb0",
      0.5: "#0f9d8a",
      0.7: "#e0a100",
      0.85: "#e8590c",
      1: "#c81d4e",
    },
  };
  // A single cell would otherwise fit to the tile layer's zoom 19.
  const FIT_MAX_ZOOM = 15;

  // Category and vehicle describe trips; stays carry neither, so time mode
  // never sends them.
  const heatQuery = (state) => {
    const mode = state.mode === "time" ? "time" : "visits";
    const params = new URLSearchParams({ mode });
    if (state.from) params.set("from", state.from);
    if (state.to) params.set("to", state.to);
    if (mode === "visits") {
      if (state.category) params.set("category", state.category);
      if (state.vehicle) params.set("vehicle", state.vehicle);
    }
    return params.toString();
  };

  const datesInOrder = (from, to) => !from || !to || from <= to;

  // Time is drawn on a log scale so a two-week stay at home does not wash
  // out every other stop; visit counts are linear.
  const scaleWeight = (weight, max, mode) => {
    if (!(weight > 0) || !(max > 0)) return 0;
    const scaled = mode === "time"
      ? Math.log1p(weight) / Math.log1p(max)
      : weight / max;
    return Math.min(scaled, 1);
  };

  // Zero-weight cells are dropped: Leaflet.heat would still draw them at
  // minOpacity.
  const heatPoints = (data, mode) => {
    const cells = data && Array.isArray(data.cells) ? data.cells : [];
    const max = Number(data && data.max);
    const points = [];
    for (const cell of cells) {
      if (!Array.isArray(cell) || cell.length < 3) continue;
      const [lat, lon, weight] = cell.map(Number);
      if (!Number.isFinite(lat) || !Number.isFinite(lon)) continue;
      const intensity = scaleWeight(weight, max, mode);
      if (intensity > 0) points.push([lat, lon, intensity]);
    }
    return points;
  };

  const emptyMessage = (mode) => (mode === "time"
    ? "No stays in these dates."
    : "No trip starts or ends match these filters.");

  const readState = (form) => ({
    mode: form.querySelector('[name="mode"]:checked').value,
    from: form.elements.from.value,
    to: form.elements.to.value,
    category: form.elements.category.value,
    vehicle: form.elements.vehicle.value,
  });

  // A busy server answers 503 when the account already has a request under
  // way, so a short wait and a resend usually succeeds.
  const BUSY_RETRIES = 2;
  const RETRY_MAX_MS = 2000;
  // Requests take turns, so a stalled one would hold "Loading..." up and
  // every later change behind it.
  const REQUEST_TIMEOUT_MS = 15000;

  const retryDelay = (header) => {
    const text = String(header == null ? "" : header).trim();
    const seconds = /^\d+$/.test(text) ? Number(text) : 0;
    return seconds > 0 ? Math.min(seconds * 1000, RETRY_MAX_MS) : 1000;
  };

  // Wrapped so the browser's timers are never called with the wrong `this`.
  const defaultTimers = {
    setTimeout: (fn, ms) => setTimeout(fn, ms),
    clearTimeout: (handle) => clearTimeout(handle),
  };

  // Returns the first load, so tests can await it; the change listener is
  // that same load function. Each returns a promise that settles once no
  // request is in flight or waiting.
  const init = (doc, L, fetchJson, timers = defaultTimers) => {
    const root = doc.querySelector("[data-places-heat]");
    if (!root || !L || typeof L.heatLayer !== "function") return null;
    const form = root.querySelector("[data-places-heat-form]");
    const status = root.querySelector("[data-places-heat-status]");
    const timeNote = root.querySelector("[data-places-heat-time-note]");
    const tripFilters = root.querySelectorAll("[data-places-heat-trip-filter]");
    const mapEl = root.querySelector("[data-places-heat-map]");

    const map = L.map(mapEl, { dragging: !L.Browser.mobile });
    // A view before the heat layer: its onAdd projects immediately.
    map.setView([20, 0], 2);
    L.tileLayer(root.dataset.tileUrl, {
      maxZoom: 19,
      attribution: root.dataset.tileAttribution,
    }).addTo(map);
    const layer = L.heatLayer([], HEAT_OPTIONS).addTo(map);

    const showStatus = (text, failed) => {
      status.textContent = text;
      status.hidden = !text;
      status.classList.toggle("places-heat-status-error", Boolean(failed));
    };

    const draw = (data, mode) => {
      const points = heatPoints(data, mode);
      layer.setLatLngs(points);
      if (!points.length) {
        showStatus(emptyMessage(mode), false);
        return;
      }
      showStatus("", false);
      // Leaflet keeps the size it measured at creation, which can predate
      // the container's layout (as trip.html and review.html also handle).
      map.invalidateSize();
      map.fitBounds(L.latLngBounds(points.map(([lat, lon]) => [lat, lon])).pad(0.1), {
        maxZoom: FIT_MAX_ZOOM,
      });
    };

    // Settles with { data }, { busy, delay } or { failed }, and never rejects.
    const request = (state) => new Promise((resolve) => {
      const controller = new AbortController();
      let done = false;
      const settle = (outcome) => {
        if (done) return;
        done = true;
        timers.clearTimeout(limit);
        resolve(outcome);
      };
      // Raced rather than left to the abort, which a fetch may not honour.
      const limit = timers.setTimeout(() => {
        controller.abort();
        settle({ failed: true });
      }, REQUEST_TIMEOUT_MS);
      (async () => {
        const response = await fetchJson(root.dataset.heatUrl + "?" + heatQuery(state), {
          headers: { Accept: "application/json" },
          credentials: "same-origin",
          signal: controller.signal,
        });
        if (response.status === 503) {
          return { busy: true, delay: retryDelay(response.headers.get("retry-after")) };
        }
        // An expired session answers with the login page, not JSON.
        const type = response.headers.get("content-type") || "";
        if (!response.ok || !type.includes("application/json")) return { failed: true };
        return { data: await response.json() };
      })().then(settle, () => settle({ failed: true }));
    });

    // One request at a time: the server refuses a second request from the
    // same account while the first runs, and an aborted fetch still runs
    // to completion there. A change made meanwhile waits in `wanted`.
    let wanted = null;
    let stale = false;
    let running = null;
    let wake = null;

    const pause = (ms) => new Promise((resolve) => {
      const handle = timers.setTimeout(() => {
        wake = null;
        resolve(true);
      }, ms);
      wake = () => {
        timers.clearTimeout(handle);
        wake = null;
        resolve(false);
      };
    });

    const send = async (state) => {
      stale = false;
      for (let attempt = 0; ; attempt += 1) {
        const outcome = await request(state);
        if (stale || wanted) return;
        if (outcome.busy && attempt < BUSY_RETRIES) {
          if (await pause(outcome.delay)) continue;
          return;
        }
        if (outcome.data === undefined) {
          layer.setLatLngs([]);
          showStatus("The heatmap could not load. Change a control to try again.", true);
          return;
        }
        draw(outcome.data, state.mode);
        return;
      }
    };

    const drain = async () => {
      try {
        while (wanted) {
          const state = wanted;
          wanted = null;
          await send(state);
        }
      } finally {
        running = null;
      }
    };

    const load = () => {
      const state = readState(form);
      const timeMode = state.mode === "time";
      tripFilters.forEach((field) => { field.disabled = timeMode; });
      timeNote.hidden = !timeMode;
      if (wake) wake();
      if (!datesInOrder(state.from, state.to)) {
        wanted = null;
        stale = true;
        layer.setLatLngs([]);
        showStatus("From must be on or before To.", true);
        return running || Promise.resolve();
      }
      wanted = state;
      showStatus("Loading...", false);
      if (!running) running = drain();
      return running;
    };

    form.addEventListener("change", load);
    form.addEventListener("submit", (event) => {
      event.preventDefault();
      load();
    });
    return load();
  };

  const PlacesHeat = {
    HEAT_OPTIONS,
    FIT_MAX_ZOOM,
    heatQuery,
    datesInOrder,
    scaleWeight,
    heatPoints,
    emptyMessage,
    BUSY_RETRIES,
    REQUEST_TIMEOUT_MS,
    retryDelay,
    init,
  };

  if (typeof window !== "undefined") {
    window.PlacesHeat = PlacesHeat;
    // Loaded from <head>, so wait for the map markup.
    if (document.readyState === "loading") {
      document.addEventListener("DOMContentLoaded", () => init(document, window.L, window.fetch.bind(window)));
    } else {
      init(document, window.L, window.fetch.bind(window));
    }
  }
  if (typeof module !== "undefined" && module.exports) module.exports = PlacesHeat;
})();
