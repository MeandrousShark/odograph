// Behavioral tests for static/places_heat.js, run by Node's built-in test
// runner. The module is loaded the same way the browser loads it (a classic
// script with a guarded CommonJS export), so these exercise the real file
// rather than a copy of its logic.
const test = require("node:test");
const assert = require("node:assert");
const path = require("node:path");

const PlacesHeat = require(
  path.join(__dirname, "..", "..", "static", "places_heat.js")
);

test("visits sends dates, category and vehicle, omitting empty values", () => {
  assert.strictEqual(
    PlacesHeat.heatQuery({
      mode: "visits", from: "2026-01-01", to: "2026-03-31", category: "business", vehicle: "none",
    }),
    "mode=visits&from=2026-01-01&to=2026-03-31&category=business&vehicle=none"
  );
  assert.strictEqual(
    PlacesHeat.heatQuery({ mode: "visits", from: "", to: "", category: "", vehicle: "" }),
    "mode=visits"
  );
});

test("time spent sends dates only", () => {
  assert.strictEqual(
    PlacesHeat.heatQuery({
      mode: "time", from: "2026-01-01", to: "2026-03-31", category: "personal", vehicle: "4",
    }),
    "mode=time&from=2026-01-01&to=2026-03-31"
  );
});

test("an unknown mode falls back to visits", () => {
  assert.strictEqual(PlacesHeat.heatQuery({ mode: "other" }), "mode=visits");
});

test("dates out of order are refused, open ends are not", () => {
  assert.strictEqual(PlacesHeat.datesInOrder("2026-02-01", "2026-01-31"), false);
  assert.strictEqual(PlacesHeat.datesInOrder("2026-01-31", "2026-01-31"), true);
  assert.strictEqual(PlacesHeat.datesInOrder("", "2026-01-31"), true);
  assert.strictEqual(PlacesHeat.datesInOrder("2026-01-31", ""), true);
});

test("visits scale linearly by the largest cell", () => {
  assert.strictEqual(PlacesHeat.scaleWeight(5, 10, "visits"), 0.5);
  assert.strictEqual(PlacesHeat.scaleWeight(10, 10, "visits"), 1);
});

test("time spent scales on a log so a long stay does not wash out short ones", () => {
  const twoWeeks = 14 * 24 * 3600;
  const hour = PlacesHeat.scaleWeight(3600, twoWeeks, "time");
  assert.strictEqual(PlacesHeat.scaleWeight(twoWeeks, twoWeeks, "time"), 1);
  assert.ok(Math.abs(hour - Math.log1p(3600) / Math.log1p(twoWeeks)) < 1e-12);
  assert.ok(hour > 0.5, `an hour against two weeks drew at ${hour}`);
  assert.ok(hour > PlacesHeat.scaleWeight(3600, twoWeeks, "visits") * 50);
});

test("zero, negative and missing weights or max scale to zero, never NaN", () => {
  for (const mode of ["visits", "time"]) {
    assert.strictEqual(PlacesHeat.scaleWeight(0, 10, mode), 0);
    assert.strictEqual(PlacesHeat.scaleWeight(-1, 10, mode), 0);
    assert.strictEqual(PlacesHeat.scaleWeight(5, 0, mode), 0);
    assert.strictEqual(PlacesHeat.scaleWeight(5, undefined, mode), 0);
    assert.strictEqual(PlacesHeat.scaleWeight(NaN, 10, mode), 0);
  }
  assert.strictEqual(PlacesHeat.scaleWeight(20, 10, "visits"), 1);
});

test("points carry scaled intensity and drop empty or malformed cells", () => {
  const points = PlacesHeat.heatPoints({
    mode: "visits",
    max: 4,
    cells: [[37.5, -122.25, 4], [37.6, -122.2, 1], [37.7, -122.1, 0], ["x", 1, 2], [1, 2]],
  }, "visits");
  assert.deepStrictEqual(points, [[37.5, -122.25, 1], [37.6, -122.2, 0.25]]);
});

test("an empty or broken response yields no points", () => {
  assert.deepStrictEqual(PlacesHeat.heatPoints({ mode: "visits", cells: [], max: 0 }, "visits"), []);
  assert.deepStrictEqual(PlacesHeat.heatPoints({ mode: "time" }, "time"), []);
  assert.deepStrictEqual(PlacesHeat.heatPoints(null, "visits"), []);
});

test("the layer options are set on purpose for 0..1 weights", () => {
  const options = PlacesHeat.HEAT_OPTIONS;
  assert.strictEqual(options.max, 1);
  assert.ok(options.maxZoom < 19, "maxZoom left at the tile layer's 19 fades city zoom");
  assert.ok(options.minOpacity > 0 && options.minOpacity < 1);
  assert.ok(options.radius > 0 && options.blur >= 0);
  const stops = Object.keys(options.gradient).map(Number);
  assert.strictEqual(Math.max(...stops), 1);
  for (const colour of Object.values(options.gradient)) {
    assert.match(colour, /^#[0-9a-f]{6}$/);
  }
  assert.ok(PlacesHeat.FIT_MAX_ZOOM < 19);
});

test("the empty message names what each mode counts", () => {
  assert.match(PlacesHeat.emptyMessage("time"), /stays/);
  assert.match(PlacesHeat.emptyMessage("visits"), /trip/);
});

// A fake page and Leaflet just wide enough for init(): the selectors are the
// ones static/places_heat.js queries.
function makePage({ mode = "visits", from = "2026-01-01", to = "2026-03-31" } = {}) {
  const field = (value) => ({ value, disabled: false });
  const elements = {
    from: field(from), to: field(to), category: field("business"), vehicle: field("4"),
  };
  const radios = { visits: { value: "visits" }, time: { value: "time" } };
  const listeners = {};
  const form = {
    elements,
    mode,
    querySelector: (selector) => (selector === '[name="mode"]:checked' ? radios[form.mode] : null),
    addEventListener: (type, fn) => { listeners[type] = fn; },
  };
  const classes = new Set();
  const status = {
    textContent: "", hidden: true,
    classList: { toggle: (name, on) => (on ? classes.add(name) : classes.delete(name)), has: (n) => classes.has(n) },
  };
  const timeNote = { hidden: true };
  const parts = {
    "[data-places-heat-form]": form,
    "[data-places-heat-status]": status,
    "[data-places-heat-time-note]": timeNote,
    "[data-places-heat-map]": {},
  };
  const root = {
    dataset: { heatUrl: "/places/heat", tileUrl: "https://tiles.test/{z}/{x}/{y}.png", tileAttribution: "Tiles" },
    querySelector: (selector) => parts[selector] || null,
    querySelectorAll: (selector) => (
      selector === "[data-places-heat-trip-filter]" ? [elements.category, elements.vehicle] : []
    ),
  };
  const doc = { querySelector: (selector) => (selector === "[data-places-heat]" ? root : null) };

  const calls = { latLngs: [], fits: [], heatOptions: null, mapOptions: null, sized: 0 };
  const map = {
    setView() { return map; },
    invalidateSize() { calls.sized += 1; return map; },
    fitBounds(bounds, options) {
      // The fit must use the container's current size.
      if (!calls.sized) throw new Error("fitBounds before invalidateSize");
      calls.fits.push({ bounds, options });
      return map;
    },
  };
  const L = {
    Browser: { mobile: true },
    map(el, options) { calls.mapOptions = options; return map; },
    tileLayer() { return { addTo: () => ({}) }; },
    heatLayer(latlngs, options) {
      calls.heatOptions = options;
      const layer = { setLatLngs(points) { calls.latLngs.push(points); return layer; } };
      return { addTo: () => layer };
    },
    latLngBounds: (points) => ({ points, pad: (amount) => ({ points, pad: amount }) }),
  };
  return { doc, L, form, elements, status, timeNote, listeners, calls };
}

function jsonResponse(body, { ok = true, type = "application/json" } = {}) {
  return { ok, headers: { get: () => type }, json: async () => body };
}

test("init fetches the default view and draws and fits its cells", async () => {
  const page = makePage();
  const urls = [];
  const fetchJson = async (url) => {
    urls.push(url);
    return jsonResponse({ mode: "visits", cells: [[37.5, -122.25, 2], [37.6, -122.2, 1]], max: 2 });
  };
  await PlacesHeat.init(page.doc, page.L, fetchJson);
  assert.deepStrictEqual(urls, [
    "/places/heat?mode=visits&from=2026-01-01&to=2026-03-31&category=business&vehicle=4",
  ]);
  assert.strictEqual(page.calls.mapOptions.dragging, false);
  assert.strictEqual(page.calls.heatOptions, PlacesHeat.HEAT_OPTIONS);
  assert.deepStrictEqual(page.calls.latLngs.at(-1), [[37.5, -122.25, 1], [37.6, -122.2, 0.5]]);
  assert.strictEqual(page.calls.fits.length, 1);
  assert.strictEqual(page.calls.fits[0].options.maxZoom, PlacesHeat.FIT_MAX_ZOOM);
  assert.strictEqual(page.status.hidden, true);
  assert.strictEqual(page.timeNote.hidden, true);
  assert.strictEqual(page.elements.category.disabled, false);
});

test("switching to time spent disables the trip filters and drops them from the query", async () => {
  const page = makePage();
  const urls = [];
  const fetchJson = async (url) => {
    urls.push(url);
    return jsonResponse({ cells: [], max: 0 });
  };
  await PlacesHeat.init(page.doc, page.L, fetchJson);
  page.form.mode = "time";
  await page.listeners.change();
  assert.strictEqual(urls.at(-1), "/places/heat?mode=time&from=2026-01-01&to=2026-03-31");
  assert.strictEqual(page.elements.category.disabled, true);
  assert.strictEqual(page.elements.vehicle.disabled, true);
  assert.strictEqual(page.timeNote.hidden, false);
  assert.strictEqual(page.status.textContent, PlacesHeat.emptyMessage("time"));
  assert.strictEqual(page.status.hidden, false);
  assert.strictEqual(page.calls.fits.length, 0);
});

test("a non-JSON or failed response shows the failure state", async () => {
  for (const response of [
    jsonResponse("<html>login</html>", { type: "text/html; charset=utf-8" }),
    jsonResponse({}, { ok: false }),
  ]) {
    const page = makePage();
    await PlacesHeat.init(page.doc, page.L, async () => response);
    assert.match(page.status.textContent, /could not load/);
    assert.strictEqual(page.status.classList.has("places-heat-status-error"), true);
    assert.deepStrictEqual(page.calls.latLngs.at(-1), []);
  }
  const page = makePage();
  await PlacesHeat.init(page.doc, page.L, async () => { throw new Error("offline"); });
  assert.match(page.status.textContent, /could not load/);
});

test("dates out of order show a message without fetching", async () => {
  const page = makePage({ from: "2026-04-01", to: "2026-03-31" });
  let fetched = 0;
  await PlacesHeat.init(page.doc, page.L, async () => { fetched += 1; return jsonResponse({}); });
  assert.strictEqual(fetched, 0);
  assert.strictEqual(page.status.textContent, "From must be on or before To.");
});

test("an older response that lands after a newer request is dropped", async () => {
  const page = makePage();
  const pending = [];
  const fetchJson = (url, options) => new Promise((resolve, reject) => {
    pending.push({ resolve, url });
    options.signal.addEventListener("abort", () => reject(new Error("aborted")));
  });
  const first = PlacesHeat.init(page.doc, page.L, fetchJson);
  page.form.mode = "time";
  const second = page.listeners.change();
  pending[0].resolve(jsonResponse({ cells: [[1, 2, 5]], max: 5 }));
  pending[1].resolve(jsonResponse({ cells: [[3, 4, 60]], max: 60 }));
  await Promise.all([first, second]);
  assert.deepStrictEqual(page.calls.latLngs, [[[3, 4, 1]]]);
  assert.doesNotMatch(page.status.textContent, /could not load/);
});
