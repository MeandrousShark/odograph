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
  const panels = {
    none: { hidden: false },
    places: { hidden: true },
    map: { hidden: true },
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

  return { context, dispatch, fields, status, hint, mapElement, resetButton, maps, tileLayers };
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
