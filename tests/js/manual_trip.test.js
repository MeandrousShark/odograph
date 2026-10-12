// Behavioral tests for the inline manual-trip route picker. The template
// script is loaded as-is in a small browser-shaped VM, so these tests exercise
// event ordering and field state instead of repeating its logic in Node.
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
  "_manual_trip_script.html"
);

function field(name, value = "", tagName = "input") {
  return {
    name,
    value,
    tagName,
    required: name === "distance",
    disabled: false,
    matches(selector) {
      return selector.split(", ").some((part) => {
        const type = part.startsWith("select[") ? "select" : "input";
        const match = part.match(/\[name="([^"]+)"\]/);
        return match && this.tagName === type && this.name === match[1];
      });
    },
    closest() {
      return null;
    },
  };
}

function searchInputField(endpoint) {
  const input = field("q");
  input.dataset = { routeSearch: endpoint };
  return input;
}

function resultButton(container, lat, lon, label) {
  return {
    dataset: { lat, lon },
    textContent: label,
    closest(selector) {
      if (selector === "[data-fill-place-latlon]") return this;
      if (selector === "[data-route-search-results]") return container;
      return null;
    },
  };
}

function response(body, ok = true) {
  return {
    ok,
    json: async () => body,
  };
}

function makeBrowser({ fetchImpl } = {}) {
  const listeners = new Map();
  const fields = {
    route_mode: field("route_mode", "places"),
    start_place: field("start_place", "", "select"),
    end_place: field("end_place", "", "select"),
    start_lat: field("start_lat"),
    start_lon: field("start_lon"),
    end_lat: field("end_lat"),
    end_lon: field("end_lon"),
    distance: field("distance"),
    routed_distance: field("routed_distance"),
    start_label: field("start_label"),
    end_label: field("end_label"),
  };
  const search = {
    inputs: { start: searchInputField("start"), end: searchInputField("end") },
    results: {
      start: { dataset: { routeSearchResults: "start" }, innerHTML: "<li>old</li>" },
      end: { dataset: { routeSearchResults: "end" }, innerHTML: "<li>old</li>" },
    },
    chosen: { start: { textContent: "" }, end: { textContent: "" } },
    status: { textContent: "" },
  };
  const panels = {
    none: { hidden: false },
    places: { hidden: true },
    map: { hidden: true },
    search: { hidden: true },
  };
  const status = { textContent: "" };
  const hint = { hidden: true };
  const mapElement = { hidden: true };
  const resetButton = {
    closest(selector) {
      return selector === "[data-route-map-reset]" ? this : null;
    },
  };
  const form = {
    querySelector(selector) {
      const nameMatch = selector.match(/\[name="([^"]+)"\]/);
      if (nameMatch) {
        if (selector.includes(":checked")) {
          return selector.includes('name="route_mode"') ? fields.route_mode : null;
        }
        return fields[nameMatch[1]] || null;
      }
      const searchMatch = selector.match(/^\[data-route-search(-results|-chosen)?="([^"]+)"\]$/);
      if (searchMatch) {
        const kind = { undefined: "inputs", "-results": "results", "-chosen": "chosen" }[searchMatch[1]];
        return search[kind][searchMatch[2]];
      }
      if (selector === "[data-route-search-status]") return search.status;
      const panelMatch = selector.match(/\[data-route-panel="([^"]+)"\]/);
      if (panelMatch) return panels[panelMatch[1]];
      if (selector === ".route-picker-status") return status;
      if (selector === ".route-distance-hint") return hint;
      return null;
    },
  };

  const maps = [];
  const tileLayers = [];
  const L = {
    map() {
      const map = {
        layers: [],
        setView() {},
        on(type, callback) {
          if (type === "click") this.clickHandler = callback;
        },
        invalidateSize() {},
        fitBounds() {},
        removeLayer(layer) {
          const index = this.layers.indexOf(layer);
          if (index !== -1) this.layers.splice(index, 1);
        },
      };
      maps.push(map);
      return map;
    },
    tileLayer() {
      const layer = { kind: "tiles" };
      tileLayers.push(layer);
      return {
        addTo(map) {
          map.layers.push(layer);
          return layer;
        },
      };
    },
    marker(data) {
      const layer = { kind: "marker", data };
      return {
        ...layer,
        addTo(map) {
          map.layers.push(this);
          return this;
        },
      };
    },
    geoJSON() {
      const layer = {
        kind: "line",
        getBounds() {
          return { pad() { return this; } };
        },
      };
      return {
        ...layer,
        addTo(map) {
          map.layers.push(this);
          return this;
        },
      };
    },
  };

  const document = {
    addEventListener(type, callback) {
      const callbacks = listeners.get(type) || [];
      callbacks.push(callback);
      listeners.set(type, callbacks);
    },
    getElementById(id) {
      if (id === "manual-trip-form") return form;
      if (id === "route-picker-map") return mapElement;
      return null;
    },
    querySelector() {
      return null;
    },
  };

  const context = {
    console,
    document,
    L,
    URLSearchParams,
    Intl,
    Number,
    parseFloat,
    fetch: fetchImpl || (() => Promise.reject(new Error("fetch not set"))),
    requestAnimationFrame(callback) {
      callback();
    },
    getComputedStyle() {
      return { getPropertyValue: () => "#2d6a4f" };
    },
  };
  vm.createContext(context);

  let source = fs.readFileSync(SCRIPT_PATH, "utf8");
  source = source
    .replace(/\{\{ csp_nonce \}\}/g, "")
    .replace(/\{\{ csrf \}\}/g, "test-token")
    .replace(/\{\{ display_tz \}\}/g, "UTC")
    .replace(/\{\{ map_tile_url \| tojson \}\}/g, '"/tiles/{z}/{x}/{y}.png"')
    .replace(/\{\{ map_tile_attribution \| tojson \}\}/g, '"test tiles"')
    .replace(/^.*?<script[^>]*>/s, "")
    .replace(/<\/script>\s*$/s, "");
  vm.runInContext(source, context, { filename: SCRIPT_PATH });

  async function dispatch(type, target) {
    const callbacks = listeners.get(type) || [];
    for (const callback of callbacks) await callback({ target });
  }

  // Full event objects (detail, preventDefault), as htmx dispatches them.
  async function emit(type, detail) {
    const event = {
      target: detail.elt || detail.target,
      detail,
      defaultPrevented: false,
      preventDefault() { this.defaultPrevented = true; },
    };
    for (const callback of listeners.get(type) || []) await callback(event);
    return event;
  }

  return { context, dispatch, emit, fields, status, hint, mapElement, resetButton, maps, tileLayers, panels, search };
}

async function flush() {
  for (let index = 0; index < 8; index += 1) await Promise.resolve();
}

function routeData(distance = "4.2") {
  return {
    ok: true,
    distance_miles: distance,
    geometry: { type: "LineString", coordinates: [[-122.3, 47.6], [-122.2, 47.7]] },
    start: [47.6, -122.3],
    end: [47.7, -122.2],
  };
}

async function startPlacePreview(browser) {
  browser.fields.start_place.value = "1";
  browser.fields.end_place.value = "2";
  await browser.dispatch("change", browser.fields.start_place);
  await flush();
}

test("clearing a named endpoint invalidates a delayed preview", async () => {
  let resolveFetch;
  const pending = new Promise((resolve) => { resolveFetch = resolve; });
  const browser = makeBrowser({ fetchImpl: () => pending });

  await startPlacePreview(browser);
  assert.strictEqual(browser.fields.distance.required, true);
  assert.strictEqual(browser.status.textContent, "Looking up the route…");

  browser.fields.end_place.value = "";
  await browser.dispatch("change", browser.fields.end_place);
  assert.strictEqual(browser.fields.routed_distance.value, "");
  assert.strictEqual(browser.fields.distance.required, true);
  assert.strictEqual(browser.status.textContent, "");

  resolveFetch(response(routeData("9.9")));
  await flush();
  assert.strictEqual(browser.fields.distance.value, "");
  assert.strictEqual(browser.fields.routed_distance.value, "");
  assert.strictEqual(browser.fields.distance.required, true);
  assert.strictEqual(browser.status.textContent, "");
});

test("clearing a named endpoint removes the previous route and preview hint", async () => {
  const browser = makeBrowser({ fetchImpl: async () => response(routeData()) });
  await startPlacePreview(browser);

  assert.strictEqual(browser.fields.distance.value, "4.2");
  assert.strictEqual(browser.fields.routed_distance.value, "4.2");
  assert.strictEqual(browser.fields.distance.required, false);
  assert.strictEqual(browser.maps[0].layers.filter((layer) => layer.kind !== "tiles").length, 3);

  browser.fields.end_place.value = "";
  await browser.dispatch("change", browser.fields.end_place);
  assert.strictEqual(browser.fields.distance.value, "");
  assert.strictEqual(browser.fields.routed_distance.value, "");
  assert.strictEqual(browser.fields.distance.required, true);
  assert.strictEqual(browser.maps[0].layers.filter((layer) => layer.kind !== "tiles").length, 0);
});

test("a same-value distance re-entry stays user-owned during invalidation", async () => {
  const browser = makeBrowser({ fetchImpl: async () => response(routeData("3.7")) });
  await startPlacePreview(browser);

  browser.fields.distance.value = "3.7";
  await browser.dispatch("input", browser.fields.distance);
  browser.fields.end_place.value = "";
  await browser.dispatch("change", browser.fields.end_place);

  assert.strictEqual(browser.fields.distance.value, "3.7");
  assert.strictEqual(browser.fields.routed_distance.value, "");
  assert.strictEqual(browser.fields.distance.required, true);
});

test("map reset clears both clicked markers and the route preview", async () => {
  const browser = makeBrowser({ fetchImpl: async () => response(routeData()) });
  browser.fields.route_mode.value = "map";
  await browser.dispatch("change", browser.fields.route_mode);
  const map = browser.maps[0];
  const point = (lat, lng) => ({
    lat,
    lng,
    wrap() { return this; },
  });

  map.clickHandler({ latlng: point(47.6, -122.3) });
  map.clickHandler({ latlng: point(47.7, -122.2) });
  await flush();
  assert.ok(map.layers.some((layer) => layer.kind === "line"));
  assert.ok(map.layers.some((layer) => layer.kind === "marker"));

  await browser.dispatch("click", browser.resetButton);
  assert.strictEqual(browser.fields.start_lat.value, "");
  assert.strictEqual(browser.fields.end_lat.value, "");
  assert.strictEqual(map.layers.filter((layer) => layer.kind !== "tiles").length, 0);
});

test("a delayed preview does not overwrite a newer explicit distance edit", async () => {
  let resolveFetch;
  const pending = new Promise((resolve) => { resolveFetch = resolve; });
  const browser = makeBrowser({ fetchImpl: () => pending });
  await startPlacePreview(browser);

  browser.fields.distance.value = "7.7";
  await browser.dispatch("input", browser.fields.distance);
  resolveFetch(response(routeData("4.2")));
  await flush();

  assert.strictEqual(browser.fields.distance.value, "7.7");
  assert.strictEqual(browser.fields.routed_distance.value, "4.2");
  assert.strictEqual(browser.fields.distance.required, false);
  assert.strictEqual(browser.status.textContent, "Route found: 4.2 mi.");
});

test("preview failures preserve explicit distance and restore required state", async (t) => {
  const cases = [
    ["transport failure", () => Promise.reject(new Error("offline")), "Could not check the route. Enter the distance."],
    ["HTTP failure", async () => response({ message: "bad selection" }, false), "Could not check the route. Enter the distance."],
    ["routing unavailable", async () => response({ ok: false }), "Automatic routing is unavailable. Enter the distance."],
  ];

  for (const [name, fetchImpl, status] of cases) {
    await t.test(name, async () => {
      const browser = makeBrowser({ fetchImpl });
      browser.fields.distance.value = "8.5";
      await browser.dispatch("input", browser.fields.distance);
      await startPlacePreview(browser);
      await flush();

      assert.strictEqual(browser.fields.distance.value, "8.5");
      assert.strictEqual(browser.fields.routed_distance.value, "");
      assert.strictEqual(browser.fields.distance.required, true);
      assert.strictEqual(browser.status.textContent, status);
    });
  }
});

// Address-search mode.

async function enterSearchMode(browser) {
  browser.fields.route_mode.value = "search";
  await browser.dispatch("change", browser.fields.route_mode);
  // Entering a mode clears results; seed both lists as if a search had run.
  browser.search.results.start.innerHTML = "<li>old</li>";
  browser.search.results.end.innerHTML = "<li>old</li>";
}

async function pick(browser, endpoint, lat, lon, label) {
  const button = resultButton(browser.search.results[endpoint], lat, lon, label);
  await browser.dispatch("click", button);
  await flush();
}

function recordingFetch(bodies, data = routeData()) {
  return async (url, options) => {
    bodies.push({ url, body: options.body });
    return response(data);
  };
}

test("search mode shows only its own panel and the map", async () => {
  const browser = makeBrowser();
  await enterSearchMode(browser);

  assert.strictEqual(browser.panels.search.hidden, false);
  assert.strictEqual(browser.panels.none.hidden, true);
  assert.strictEqual(browser.panels.map.hidden, true);
  assert.strictEqual(browser.mapElement.hidden, false);
  assert.strictEqual(browser.fields.start_label.disabled, true);
});

test("picking start then end fills the right fields and previews as search", async () => {
  const bodies = [];
  const browser = makeBrowser({ fetchImpl: recordingFetch(bodies) });
  await enterSearchMode(browser);

  await pick(browser, "start", "47.6012349", "-122.3300001", "Start <b>Place</b>");
  assert.strictEqual(browser.fields.start_lat.value, "47.601235");
  assert.strictEqual(browser.fields.start_lon.value, "-122.330000");
  assert.strictEqual(browser.fields.end_lat.value, "");
  assert.strictEqual(bodies.length, 0, "no preview until both endpoints are set");
  assert.strictEqual(browser.search.chosen.start.textContent, "Start <b>Place</b>");
  assert.strictEqual(browser.search.results.start.innerHTML, "");
  assert.strictEqual(browser.search.results.end.innerHTML, "<li>old</li>");
  assert.strictEqual(browser.search.status.textContent, "Start set to Start <b>Place</b>");

  await pick(browser, "end", "47.7", "-122.2", "End Place");
  assert.strictEqual(browser.fields.end_lat.value, "47.700000");
  assert.strictEqual(browser.fields.end_lon.value, "-122.200000");
  assert.strictEqual(browser.search.status.textContent, "End set to End Place");
  assert.strictEqual(bodies.length, 1);
  assert.strictEqual(bodies[0].url, "/trips/manual/route-preview");
  assert.strictEqual(bodies[0].body.get("route_mode"), "search");
  assert.strictEqual(bodies[0].body.get("start_lat"), "47.601235");
  assert.strictEqual(bodies[0].body.get("end_lon"), "-122.200000");
  assert.strictEqual(browser.fields.distance.value, "4.2");
  assert.strictEqual(browser.fields.routed_distance.value, "4.2");
  assert.strictEqual(browser.fields.distance.required, false);
  assert.strictEqual(browser.maps[0].layers.filter((layer) => layer.kind === "marker").length, 2);
});

test("editing a search field after a pick clears only that endpoint and the preview", async () => {
  const bodies = [];
  const browser = makeBrowser({ fetchImpl: recordingFetch(bodies) });
  await enterSearchMode(browser);
  await pick(browser, "start", "47.6", "-122.3", "A");
  await pick(browser, "end", "47.7", "-122.2", "B");
  assert.strictEqual(browser.fields.distance.value, "4.2");

  browser.search.inputs.end.value = "B st";
  await browser.dispatch("input", browser.search.inputs.end);

  assert.strictEqual(browser.fields.end_lat.value, "");
  assert.strictEqual(browser.fields.end_lon.value, "");
  assert.strictEqual(browser.search.chosen.end.textContent, "");
  assert.strictEqual(browser.fields.start_lat.value, "47.600000");
  assert.strictEqual(browser.search.chosen.start.textContent, "A");
  assert.strictEqual(browser.fields.routed_distance.value, "");
  assert.strictEqual(browser.fields.distance.value, "");
  assert.strictEqual(browser.fields.distance.required, true);
  assert.strictEqual(browser.maps[0].layers.filter((layer) => layer.kind !== "tiles").length, 0);
});

test("switching modes clears search inputs, results, chosen labels and coordinates", async () => {
  const browser = makeBrowser({ fetchImpl: recordingFetch([]) });
  await enterSearchMode(browser);
  browser.search.inputs.start.value = "seatac";
  await pick(browser, "start", "47.6", "-122.3", "A");
  await pick(browser, "end", "47.7", "-122.2", "B");
  browser.search.inputs.end.value = "typed";

  for (const mode of ["map", "none"]) {
    await enterSearchMode(browser);
    await pick(browser, "start", "47.6", "-122.3", "A");
    browser.fields.route_mode.value = mode;
    await browser.dispatch("change", browser.fields.route_mode);

    assert.strictEqual(browser.search.inputs.start.value, "");
    assert.strictEqual(browser.search.inputs.end.value, "");
    assert.strictEqual(browser.search.results.start.innerHTML, "");
    assert.strictEqual(browser.search.results.end.innerHTML, "");
    assert.strictEqual(browser.search.chosen.start.textContent, "");
    assert.strictEqual(browser.search.status.textContent, "");
    for (const name of ["start_lat", "start_lon", "end_lat", "end_lon"]) {
      assert.strictEqual(browser.fields[name].value, "");
    }
    assert.strictEqual(browser.panels.search.hidden, true);
    browser.search.results.start.innerHTML = "<li>old</li>";
    browser.search.results.end.innerHTML = "<li>old</li>";
  }
  assert.strictEqual(browser.fields.start_label.disabled, false);
});

test("a typed label survives a search round trip and stays disabled meanwhile", async () => {
  const browser = makeBrowser();
  browser.fields.start_label.value = "Grandma";
  await enterSearchMode(browser);
  assert.strictEqual(browser.fields.start_label.disabled, true);
  assert.strictEqual(browser.fields.start_label.value, "Grandma");
  browser.fields.route_mode.value = "none";
  await browser.dispatch("change", browser.fields.route_mode);
  assert.strictEqual(browser.fields.start_label.disabled, false);
  assert.strictEqual(browser.fields.start_label.value, "Grandma");
});

test("a pick aborts any search still in flight for that field", async () => {
  const browser = makeBrowser({ fetchImpl: recordingFetch([]) });
  const aborted = [];
  browser.context.htmx = { trigger: (element, name) => aborted.push([element, name]) };
  await enterSearchMode(browser);
  aborted.length = 0;

  await pick(browser, "end", "47.7", "-122.2", "B");

  assert.deepStrictEqual(aborted, [[browser.search.inputs.end, "htmx:abort"]]);
});

test("a stale preview does not overwrite a newer search pick", async () => {
  const pending = [];
  const fetchImpl = () => new Promise((resolve) => pending.push(resolve));
  const browser = makeBrowser({ fetchImpl });
  await enterSearchMode(browser);
  await pick(browser, "start", "47.6", "-122.3", "A");
  await pick(browser, "end", "47.7", "-122.2", "B");
  await pick(browser, "end", "47.8", "-122.1", "C");
  assert.strictEqual(pending.length, 2);

  pending[1](response(routeData("7.7")));
  await flush();
  pending[0](response(routeData("1.1")));
  await flush();

  assert.strictEqual(browser.fields.end_lat.value, "47.800000");
  assert.strictEqual(browser.fields.distance.value, "7.7");
  assert.strictEqual(browser.fields.routed_distance.value, "7.7");
});

test("a pick with unusable coordinates changes nothing", async () => {
  const bodies = [];
  const browser = makeBrowser({ fetchImpl: recordingFetch(bodies) });
  await enterSearchMode(browser);

  await pick(browser, "start", "not-a-number", "-122.3", "A");

  assert.strictEqual(browser.fields.start_lat.value, "");
  assert.strictEqual(browser.search.chosen.start.textContent, "");
  assert.strictEqual(browser.search.results.start.innerHTML, "<li>old</li>");
});

// Autocomplete request lifecycle: htmx:confirm gates a request, beforeRequest
// tags its xhr, beforeSwap drops responses that were invalidated meanwhile.

function withHtmx(browser) {
  const aborted = [];
  browser.context.htmx = { trigger: (element, name) => aborted.push([element.dataset.routeSearch, name]) };
  return aborted;
}

// Typing: the input event, then (after the debounce) htmx:confirm and, if it
// was not refused, htmx:beforeRequest. Returns the xhr, or null if refused.
async function typeSearch(browser, endpoint, text) {
  const input = browser.search.inputs[endpoint];
  input.value = text;
  await browser.dispatch("input", input);
  return fireSearch(browser, endpoint);
}

async function fireSearch(browser, endpoint) {
  const input = browser.search.inputs[endpoint];
  const confirm = await browser.emit("htmx:confirm", { elt: input, target: browser.search.results[endpoint] });
  if (confirm.defaultPrevented) return null;
  const xhr = {};
  await browser.emit("htmx:beforeRequest", { elt: input, xhr });
  return xhr;
}

async function swapAllowed(browser, endpoint, xhr) {
  const event = await browser.emit("htmx:beforeSwap", {
    elt: browser.search.results[endpoint], target: browser.search.results[endpoint], xhr, shouldSwap: true,
  });
  return event.detail.shouldSwap;
}

test("a response from before a mode switch is not swapped in after returning to Search", async () => {
  const browser = makeBrowser();
  const aborted = withHtmx(browser);
  await enterSearchMode(browser);
  const xhr = await typeSearch(browser, "start", "Main St");
  assert.ok(xhr);

  browser.fields.route_mode.value = "map";
  await browser.dispatch("change", browser.fields.route_mode);
  await enterSearchMode(browser);

  assert.ok(aborted.some(([endpoint, name]) => endpoint === "start" && name === "htmx:abort"));
  assert.strictEqual(await swapAllowed(browser, "start", xhr), false);
});

test("a response arriving while another mode is active is not swapped in", async () => {
  const browser = makeBrowser();
  await enterSearchMode(browser);
  const xhr = await typeSearch(browser, "end", "Main St");
  browser.fields.route_mode.value = "map";
  await browser.dispatch("change", browser.fields.route_mode);
  assert.strictEqual(await swapAllowed(browser, "end", xhr), false);
});

test("the same text searches again after a mode switch round trip", async () => {
  const browser = makeBrowser();
  await enterSearchMode(browser);
  assert.ok(await typeSearch(browser, "start", "Main St"));

  browser.fields.route_mode.value = "map";
  await browser.dispatch("change", browser.fields.route_mode);
  await enterSearchMode(browser);

  assert.ok(await typeSearch(browser, "start", "Main St"), "request must fire again");
});

test("a keyup that did not change the text neither searches nor aborts", async () => {
  const browser = makeBrowser();
  const aborted = withHtmx(browser);
  await enterSearchMode(browser);
  const xhr = await typeSearch(browser, "start", "Main St");
  aborted.length = 0;

  assert.strictEqual(await fireSearch(browser, "start"), null);
  assert.deepStrictEqual(aborted, []);
  assert.strictEqual(await swapAllowed(browser, "start", xhr), true);
});

test("a pick cancels a debounced search that has not fired yet", async () => {
  const browser = makeBrowser({ fetchImpl: recordingFetch([]) });
  await enterSearchMode(browser);
  browser.search.inputs.start.value = "B";
  await browser.dispatch("input", browser.search.inputs.start);
  await pick(browser, "start", "47.6", "-122.3", "Place B");

  assert.strictEqual(await fireSearch(browser, "start"), null);
  assert.strictEqual(browser.search.results.start.innerHTML, "");
});

test("a pick drops the response of a search already in flight", async () => {
  const browser = makeBrowser({ fetchImpl: recordingFetch([]) });
  withHtmx(browser);
  await enterSearchMode(browser);
  const xhr = await typeSearch(browser, "start", "B");
  await pick(browser, "start", "47.6", "-122.3", "Place B");

  assert.strictEqual(await swapAllowed(browser, "start", xhr), false);
});

test("typing after a pick searches again and the new response is swapped in", async () => {
  const browser = makeBrowser({ fetchImpl: recordingFetch([]) });
  await enterSearchMode(browser);
  await pick(browser, "start", "47.6", "-122.3", "Place B");

  const xhr = await typeSearch(browser, "start", "Place B2");

  assert.ok(xhr);
  assert.strictEqual(await swapAllowed(browser, "start", xhr), true);
});

test("a pick on one field does not affect the other field's search", async () => {
  const browser = makeBrowser({ fetchImpl: recordingFetch([]) });
  await enterSearchMode(browser);
  const xhr = await typeSearch(browser, "end", "Elm");
  await pick(browser, "start", "47.6", "-122.3", "A");

  assert.strictEqual(await swapAllowed(browser, "end", xhr), true);
  assert.ok(await typeSearch(browser, "end", "Elm St"));
});

test("emptied text sends nothing, drops pending responses and clears the list", async () => {
  const browser = makeBrowser();
  const aborted = withHtmx(browser);
  await enterSearchMode(browser);
  const xhr = await typeSearch(browser, "start", "Main");
  browser.search.results.start.innerHTML = "<li>Main St</li>";
  aborted.length = 0;

  assert.strictEqual(await typeSearch(browser, "start", "   "), null);

  assert.strictEqual(browser.search.results.start.innerHTML, "");
  assert.deepStrictEqual(aborted, [["start", "htmx:abort"]]);
  assert.strictEqual(await swapAllowed(browser, "start", xhr), false);
  assert.ok(await typeSearch(browser, "start", "Main"), "same text as before the clear searches again");
});

test("no autocomplete request starts outside Search mode", async () => {
  const browser = makeBrowser();
  browser.search.inputs.start.value = "Main St";
  assert.strictEqual(await fireSearch(browser, "start"), null);
});

test("requests from other elements are left alone", async () => {
  const browser = makeBrowser();
  await enterSearchMode(browser);
  const other = { dataset: {}, value: "x" };
  const confirm = await browser.emit("htmx:confirm", { elt: other });
  assert.strictEqual(confirm.defaultPrevented, false);
  const swap = await browser.emit("htmx:beforeSwap", { elt: other, xhr: {}, shouldSwap: true });
  assert.strictEqual(swap.detail.shouldSwap, true);
});
