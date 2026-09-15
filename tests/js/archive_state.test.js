// Behavioral tests for static/archive_state.js, run by Node's built-in test
// runner. The module is loaded the same way the browser loads it (a classic
// script with a guarded CommonJS export), so these exercise the real file
// rather than a copy of its logic.
const test = require("node:test");
const assert = require("node:assert");
const path = require("node:path");

const ArchiveState = require(
  path.join(__dirname, "..", "..", "static", "archive_state.js")
);

test("a newer request supersedes the token an older one is holding", () => {
  const scope = ArchiveState.createRequestScope();

  const older = scope.start();
  assert.strictEqual(scope.isCurrent(older), true);

  const newer = scope.start();
  assert.strictEqual(scope.isCurrent(older), false);
  assert.strictEqual(scope.isCurrent(newer), true);
});

test("a superseded request object comes back exactly once so it can be aborted", () => {
  const scope = ArchiveState.createRequestScope();
  const first = { name: "first" };
  const second = { name: "second" };

  const older = scope.start();
  scope.retain(older, first);
  const newer = scope.start();
  scope.retain(newer, second);

  assert.deepStrictEqual(scope.takeSuperseded(), [first]);
  assert.deepStrictEqual(scope.takeSuperseded(), []);
  assert.strictEqual(scope.pending(), 1);

  scope.release(second);
  assert.strictEqual(scope.pending(), 0);
});

test("starting a write generation supersedes an outstanding archive read", () => {
  const scope = ArchiveState.createRequestScope();
  const read = { abort() {} };

  const readToken = scope.start();
  scope.retain(readToken, read);
  scope.start();

  assert.strictEqual(scope.isCurrent(readToken), false);
  assert.deepStrictEqual(scope.takeSuperseded(), [read]);
  assert.strictEqual(scope.pending(), 0);
});

test("a released request is not reported as superseded later", () => {
  const scope = ArchiveState.createRequestScope();
  const request = { name: "settled" };

  const token = scope.start();
  scope.retain(token, request);
  scope.release(request);
  scope.start();

  assert.deepStrictEqual(scope.takeSuperseded(), []);
});

// Each month paginates on its own, so two outstanding "Load more" requests
// belong to the same view and must both survive; only a new filter, Clear,
// retry, or history restore replaces that view.
test("requests joining a generation are both current and go stale together", () => {
  const scope = ArchiveState.createRequestScope();
  const filter = { name: "filter" };
  const pagerA = { name: "pager-a" };
  const pagerB = { name: "pager-b" };

  const token = scope.start();
  scope.retain(token, filter);
  scope.retain(scope.currentToken(), pagerA);
  scope.retain(scope.currentToken(), pagerB);

  assert.strictEqual(scope.currentToken(), token);
  assert.strictEqual(scope.isCurrent(token), true);
  assert.strictEqual(scope.pending(), 3);
  // Nothing newer has been asked for, so none of them is stale.
  assert.deepStrictEqual(scope.takeSuperseded(), []);

  const newer = scope.start();
  assert.strictEqual(scope.isCurrent(token), false);
  assert.strictEqual(scope.currentToken(), newer);
  assert.deepStrictEqual(scope.takeSuperseded(), [filter, pagerA, pagerB]);
  assert.deepStrictEqual(scope.takeSuperseded(), []);
  assert.strictEqual(scope.pending(), 0);
});

test("a request released before the next generation is never superseded, and its peers still are", () => {
  const scope = ArchiveState.createRequestScope();
  const settled = { name: "settled" };
  const outstanding = { name: "outstanding" };

  const token = scope.start();
  scope.retain(token, settled);
  scope.retain(scope.currentToken(), outstanding);
  scope.release(settled);
  assert.strictEqual(scope.pending(), 1);

  scope.start();
  assert.deepStrictEqual(scope.takeSuperseded(), [outstanding]);
  assert.deepStrictEqual(scope.takeSuperseded(), []);
});

function fakeClock() {
  const scheduled = new Map();
  let nextHandle = 1;
  return {
    schedule(fn, delay) {
      const handle = nextHandle++;
      scheduled.set(handle, { fn, delay });
      return handle;
    },
    cancel(handle) {
      scheduled.delete(handle);
    },
    tick() {
      const due = Array.from(scheduled.entries());
      scheduled.clear();
      due.forEach((entry) => entry[1].fn());
    },
    size() {
      return scheduled.size;
    },
    delays() {
      return Array.from(scheduled.values()).map((entry) => entry.delay);
    },
  };
}

function debouncerFor(clock, runs) {
  return ArchiveState.createDebouncer({
    run: () => runs.push(runs.length),
    delay: 350,
    schedule: clock.schedule,
    cancel: clock.cancel,
  });
}

test("a debounced run happens once after the delay", () => {
  const clock = fakeClock();
  const runs = [];
  const debouncer = debouncerFor(clock, runs);

  debouncer.request();
  assert.strictEqual(debouncer.isPending(), true);
  assert.deepStrictEqual(clock.delays(), [350]);
  assert.deepStrictEqual(runs, []);

  clock.tick();
  assert.strictEqual(runs.length, 1);
  assert.strictEqual(debouncer.isPending(), false);
});

test("a second request within the window reschedules instead of running twice", () => {
  const clock = fakeClock();
  const runs = [];
  const debouncer = debouncerFor(clock, runs);

  debouncer.request();
  debouncer.request();
  debouncer.request();
  assert.strictEqual(clock.size(), 1);

  clock.tick();
  assert.strictEqual(runs.length, 1);
});

test("flush runs the pending debounce exactly once and clears it", () => {
  const clock = fakeClock();
  const runs = [];
  const debouncer = debouncerFor(clock, runs);

  debouncer.request();
  assert.strictEqual(debouncer.flush(), true);
  assert.strictEqual(runs.length, 1);
  assert.strictEqual(debouncer.isPending(), false);
  assert.strictEqual(clock.size(), 0);

  // A second flush with nothing pending must not run again, and the
  // cancelled timer must not fire behind it either.
  assert.strictEqual(debouncer.flush(), false);
  clock.tick();
  assert.strictEqual(runs.length, 1);
});

test("cancel prevents the pending run", () => {
  const clock = fakeClock();
  const runs = [];
  const debouncer = debouncerFor(clock, runs);

  debouncer.request();
  assert.strictEqual(debouncer.cancel(), true);
  assert.strictEqual(debouncer.isPending(), false);

  clock.tick();
  assert.deepStrictEqual(runs, []);
  assert.strictEqual(debouncer.cancel(), false);
});

test("a deferred history miss abort runs after htmx opens and sends its XHR", async () => {
  let opened = false;
  let sent = false;
  let aborted = false;
  const xhr = {
    abort() {
      assert.strictEqual(opened, true);
      assert.strictEqual(sent, true);
      aborted = true;
    },
  };

  ArchiveState.defer(() => xhr.abort());
  assert.strictEqual(aborted, false);
  opened = true;
  sent = true;
  await new Promise((resolve) => setImmediate(resolve));
  assert.strictEqual(aborted, true);
});

test("a retained history miss can be released when its deferred abort wins", async () => {
  const scope = ArchiveState.createRequestScope();
  const xhr = { abort() {} };
  const token = scope.start();
  scope.retain(token, xhr);
  assert.strictEqual(scope.pending(), 1);

  ArchiveState.defer(() => {
    scope.release(xhr);
    xhr.abort();
  });
  await new Promise((resolve) => setImmediate(resolve));
  assert.strictEqual(scope.pending(), 0);
});

test("the serializer drops empty values and keeps the ones asked for", () => {
  const pairs = [
    ["q", "coffee run"],
    ["category", ""],
    ["date_preset", "all"],
    ["from", ""],
    ["to", ""],
    ["vehicle", "3"],
    ["exclusion", ""],
  ];

  assert.strictEqual(
    ArchiveState.serializeFilters(pairs),
    "q=coffee%20run&date_preset=all&vehicle=3"
  );
  assert.strictEqual(
    ArchiveState.serializeFilters(
      [["q", ""], ["date_preset", ""]],
      { keepEmpty: ["date_preset"] }
    ),
    "date_preset="
  );
  // Null and undefined are the same "no value" as an empty string.
  assert.strictEqual(
    ArchiveState.serializeFilters([["q", null], ["vehicle", undefined], ["to", "2026-07-31"]]),
    "to=2026-07-31"
  );
});

test("the serializer is stable for the same filter state", () => {
  const build = (term) => [
    ["q", term],
    ["category", "business"],
    ["date_preset", "custom"],
    ["from", "2026-07-01"],
    ["to", ""],
  ];

  const applied = ArchiveState.serializeFilters(build("zephyr"));
  assert.strictEqual(ArchiveState.sameFilters(applied, ArchiveState.serializeFilters(build("zephyr"))), true);
  assert.strictEqual(ArchiveState.sameFilters(applied, ArchiveState.serializeFilters(build("zephyrr"))), false);
  assert.strictEqual(ArchiveState.sameFilters(applied, ArchiveState.serializeFilters(build(""))), false);
});

test("canonical archive queries keep resolved bounds and drop the applied preset", () => {
  const pairs = [
    ["q", "drive"],
    ["category", "business"],
    ["date_preset", "this_month"],
    ["from", "2026-09-01"],
    ["to", "2026-09-30"],
    ["vehicle", "3"],
    ["exclusion", ""],
  ];

  assert.strictEqual(
    ArchiveState.archiveFilterQuery(pairs),
    "q=drive&category=business&from=2026-09-01&to=2026-09-30&vehicle=3",
  );
  assert.strictEqual(
    ArchiveState.archiveFilterQuery(pairs, { transientPreset: true }),
    "q=drive&category=business&date_preset=this_month&vehicle=3",
  );
});

test("archive state queries retain fixed preset dates across a calendar rollover", () => {
  const september = ArchiveState.archiveStateQuery({
    q: "", category: "", from: "2026-09-01", to: "2026-09-30",
    vehicle: "", exclusion: "",
  });
  const octoberVehicleChange = ArchiveState.archiveFilterQuery([
    ["q", ""], ["category", ""], ["date_preset", "this_month"],
    ["from", "2026-09-01"], ["to", "2026-09-30"], ["vehicle", "7"],
    ["exclusion", ""],
  ]);

  assert.strictEqual(september, "from=2026-09-01&to=2026-09-30");
  assert.strictEqual(
    octoberVehicleChange,
    "from=2026-09-01&to=2026-09-30&vehicle=7",
  );

  const decemberVehicleChange = ArchiveState.archiveFilterQuery([
    ["q", ""], ["category", ""], ["date_preset", "this_month"],
    ["from", "2026-12-01"], ["to", "2026-12-31"], ["vehicle", "7"],
    ["exclusion", ""],
  ]);
  assert.strictEqual(
    decemberVehicleChange,
    "from=2026-12-01&to=2026-12-31&vehicle=7",
  );
});

test("selection payload validation accepts one complete explicit-id snapshot", () => {
  assert.deepStrictEqual(
    ArchiveState.validateSelectionPayload({ trip_ids: [7, 11, 19], count: 3 }),
    [7, 11, 19],
  );
  assert.deepStrictEqual(
    ArchiveState.validateSelectionPayload({ trip_ids: [], count: 0 }),
    [],
  );
});

test("selection payload validation rejects partial, duplicate, and unsafe ids atomically", () => {
  for (const payload of [
    null,
    { trip_ids: [1, 2], count: 1 },
    { trip_ids: [1, 1], count: 2 },
    { trip_ids: [1, 0], count: 2 },
    { trip_ids: [1, 2.5], count: 2 },
    { trip_ids: [1, Number.MAX_SAFE_INTEGER + 1], count: 2 },
    { trip_ids: "1,2", count: 2 },
  ]) {
    assert.strictEqual(ArchiveState.validateSelectionPayload(payload), null);
  }
});

test("write coordinator rejects overlapping writes and locks through refresh", () => {
  const coordinator = ArchiveState.createWriteCoordinator();

  assert.strictEqual(coordinator.begin("from=2026-09-01"), true);
  assert.strictEqual(coordinator.begin("from=2026-10-01"), false);
  assert.strictEqual(coordinator.isBusy(), true);
  assert.strictEqual(coordinator.deferDraft(), true);
  assert.strictEqual(coordinator.startRefresh(), "from=2026-09-01");
  assert.strictEqual(coordinator.isRefreshing(), true);
  assert.strictEqual(coordinator.begin("q=draft"), false);
  assert.strictEqual(coordinator.retryRefresh(), "from=2026-09-01");
  assert.strictEqual(coordinator.completeRefresh(), true);
  assert.strictEqual(coordinator.isBusy(), false);
});

test("write coordinator keeps refresh failures retryable and releases after retry", () => {
  const coordinator = ArchiveState.createWriteCoordinator();

  assert.strictEqual(coordinator.begin(""), true);
  assert.strictEqual(coordinator.startRefresh(), "");
  assert.strictEqual(coordinator.isRefreshing(), true);
  assert.strictEqual(coordinator.begin("q=blocked"), false);
  assert.strictEqual(coordinator.retryRefresh(), "");
  assert.strictEqual(coordinator.completeRefresh(), false);
  assert.strictEqual(coordinator.isBusy(), false);
});

test("failed writes release the gate and return deferred draft intent", () => {
  const coordinator = ArchiveState.createWriteCoordinator();

  assert.strictEqual(coordinator.begin(""), true);
  assert.strictEqual(coordinator.deferDraft(), true);
  assert.strictEqual(coordinator.failPost(), true);
  assert.strictEqual(coordinator.isBusy(), false);
  assert.strictEqual(coordinator.pendingDraft(), false);
});

test("history retry retains its canonical path until restore succeeds", () => {
  const retry = ArchiveState.createHistoryRetry();

  retry.start("/trips?from=2026-09-01&to=2026-09-30");
  assert.strictEqual(retry.pending(), true);
  assert.strictEqual(retry.path(), "/trips?from=2026-09-01&to=2026-09-30");
  retry.clear();
  assert.strictEqual(retry.pending(), false);
  assert.strictEqual(retry.path(), null);
});

test("out-of-order history misses keep the latest initiated path", () => {
  let deferred = "/trips?from=A";
  deferred = "/trips?from=B";

  // B resolves first, then A. Neither response can displace the latest
  // intent captured when B was initiated.
  deferred = ArchiveState.historyRestorePath(deferred, {
    path: "/trips?from=B", cacheMiss: true,
  });
  deferred = ArchiveState.historyRestorePath(deferred, {
    path: "/trips?from=A", cacheMiss: true,
  });
  assert.strictEqual(deferred, "/trips?from=B");

  // A cache hit has no preceding miss event and therefore establishes its
  // own destination.
  assert.strictEqual(
    ArchiveState.historyRestorePath(null, { path: "/trips?vehicle=7" }),
    "/trips?vehicle=7",
  );
});

test("the loaded-depth map keeps each month's rendered row count, including zero", () => {
  assert.deepStrictEqual(
    ArchiveState.loadedDepthMap([
      ["2026-07", 50],
      ["2026-06", 25],
      // A month whose rows all left the filtered set still has a rendered
      // depth, and it is not the same as never having been expanded.
      ["2026-05", 0],
    ]),
    { "2026-07": 50, "2026-06": 25, "2026-05": 0 }
  );
  assert.deepStrictEqual(ArchiveState.loadedDepthMap([]), {});
  // A list with no month key is not describable depth, so it is skipped
  // rather than sent as an empty key the server would reject.
  assert.deepStrictEqual(ArchiveState.loadedDepthMap([["", 3], [undefined, 4]]), {});
});

test("saved loaded depth accepts only safe local month counts", () => {
  assert.deepStrictEqual(
    ArchiveState.validateLoadedDepth({ "2026-07": 50, "2026-08": 0 }),
    { "2026-07": 50, "2026-08": 0 },
  );
  for (const value of [
    [], { "2026-00": 1 }, { "2026-07-32": 1 },
    { "July 2026": 1 }, { "2026-07": -1 },
    { "2026-07": 1.5 }, { "2026-07": Number.MAX_SAFE_INTEGER + 1 },
  ]) {
    assert.strictEqual(ArchiveState.validateLoadedDepth(value), null);
  }
  assert.deepStrictEqual(ArchiveState.validateLoadedDepth(undefined), {});
  assert.deepStrictEqual(
    ArchiveState.validateLoadedDepth({ "2026-07": 100000 }),
    { "2026-07": 100000 },
  );
  assert.strictEqual(
    ArchiveState.validateLoadedDepth({ "2026-07": 100001 }),
    null,
  );
});

test("loaded-depth restoration merges saved and rendered months and detects gaps", () => {
  assert.deepStrictEqual(
    ArchiveState.mergeLoadedDepth(
      { "2026-07": 50, "2026-08": 25 },
      { "2026-07": 75, "2026-09": 10 },
    ),
    { "2026-07": 75, "2026-09": 10, "2026-08": 25 },
  );
  assert.deepStrictEqual(
    ArchiveState.loadedDepthRestore(
      { "2026-07": 50, "2026-08": 25 },
      { "2026-07": 25, "2026-08": 25 },
    ),
    {
      valid: true,
      depth: { "2026-07": 50, "2026-08": 25 },
      needsRestore: true,
    },
  );
  assert.deepStrictEqual(
    ArchiveState.loadedDepthRestore({ "2026-07": 25 }, { "2026-07": 50 }),
    { valid: true, depth: { "2026-07": 50 }, needsRestore: false },
  );
  assert.deepStrictEqual(
    ArchiveState.loadedDepthRestore({ "not-a-month": 50 }, {}),
    { valid: false, depth: {}, needsRestore: false },
  );
});

test("filter disclosure hides controls only for a closed mobile view", () => {
  assert.deepStrictEqual(
    ArchiveState.filterDisclosureState(false, false),
    { showToggle: false, hideControls: false },
  );
  assert.deepStrictEqual(
    ArchiveState.filterDisclosureState(true, false),
    { showToggle: true, hideControls: true },
  );
  assert.deepStrictEqual(
    ArchiveState.filterDisclosureState(true, true),
    { showToggle: true, hideControls: false },
  );
  // The open choice is retained across a desktop round trip.
  assert.deepStrictEqual(
    ArchiveState.filterDisclosureState(false, true),
    { showToggle: false, hideControls: false },
  );
});

test("a refresh keeps selected ids whose rows are not rendered", () => {
  // A batch update can move a selected trip outside the current filters
  // without deleting it; it stays a valid target for the next batch write.
  const view = ArchiveState.reconcileSelection([7, 8, 9], [8]);

  assert.deepStrictEqual(view.selected, [7, 8, 9]);
  assert.deepStrictEqual(view.rendered, [8]);
  assert.deepStrictEqual(view.outside, [7, 9]);
});

test("an explicit refresh intent behaves the same as the default", () => {
  const view = ArchiveState.reconcileSelection(new Set([4, 5]), [5], { intent: "refresh" });

  assert.deepStrictEqual(view.selected, [4, 5]);
  assert.deepStrictEqual(view.outside, [4]);
});

test("a filter navigation clears the whole selection, rendered or not", () => {
  const view = ArchiveState.reconcileSelection(new Set([7, 8, 9]), [8], { intent: "navigate" });

  assert.deepStrictEqual(view.selected, []);
  assert.deepStrictEqual(view.rendered, []);
  assert.deepStrictEqual(view.outside, []);
});

test("the selection count names trips the current view is not showing", () => {
  assert.strictEqual(
    ArchiveState.selectionCountLabel({ total: 5, rendered: 3 }),
    "5 trips selected (2 outside this view)"
  );
  assert.strictEqual(
    ArchiveState.selectionCountLabel({ total: 2, rendered: 1 }),
    "2 trips selected (1 outside this view)"
  );
});

test("the selection count omits the parenthetical when every selected trip is rendered", () => {
  assert.strictEqual(
    ArchiveState.selectionCountLabel({ total: 4, rendered: 4 }),
    "4 trips selected"
  );
  assert.strictEqual(
    ArchiveState.selectionCountLabel({ total: 0, rendered: 0 }),
    "0 trips selected"
  );
});

test("the selection count keeps singular and plural trips", () => {
  assert.strictEqual(
    ArchiveState.selectionCountLabel({ total: 1, rendered: 1 }),
    "1 trip selected"
  );
  assert.strictEqual(
    ArchiveState.selectionCountLabel({ total: 1, rendered: 0 }),
    "1 trip selected (1 outside this view)"
  );
  assert.strictEqual(
    ArchiveState.selectionCountLabel({ total: 3, rendered: 3 }),
    "3 trips selected"
  );
});

test("merge is unavailable while any selected trip is outside the view", () => {
  // Eligibility is read off the rows on screen, so a selection reaching
  // trips this view is not showing cannot establish it.
  assert.strictEqual(ArchiveState.canMergeSelection({ total: 3, rendered: 2 }), false);
  assert.strictEqual(ArchiveState.canMergeSelection({ total: 2, rendered: 0 }), false);
});

test("merge needs at least two selected trips, all of them rendered", () => {
  assert.strictEqual(ArchiveState.canMergeSelection({ total: 0, rendered: 0 }), false);
  assert.strictEqual(ArchiveState.canMergeSelection({ total: 1, rendered: 1 }), false);
  assert.strictEqual(ArchiveState.canMergeSelection({ total: 2, rendered: 2 }), true);
  assert.strictEqual(ArchiveState.canMergeSelection({ total: 6, rendered: 6 }), true);
});
