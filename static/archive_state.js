// Archive request bookkeeping for the trips page controller.
//
// This is deliberately separate from the page's inline script: it isolates
// the request-generation, debounce, comparison, and selection rules so they
// can be exercised without a browser. Nothing here touches the DOM, htmx, or
// the clock. It loads as a classic browser script and as a CommonJS module,
// so the same file runs in both places with no build step.
(() => {
  "use strict";

  // One generation counter for every archive request. Starting a generation
  // supersedes the ones before it, so a slow older response (a previous
  // filter, an obsolete pager page, a superseded history refetch) can
  // recognize itself as stale instead of overwriting a newer view.
  //
  // Retained requests are keyed by the request itself, because a generation
  // can hold several at once: each month paginates on its own, so two
  // outstanding "Load more" requests both belong to the view on screen and
  // must both swap. Only an intent that replaces that view (a filter change,
  // Clear, retry, a history restore) starts a new generation, which makes
  // every request still holding the old one stale together.
  const createRequestScope = () => {
    let current = 0;
    const inFlight = new Map();

    return {
      start() {
        current += 1;
        return current;
      },
      // For a request that belongs to the view already on screen instead of
      // replacing it.
      currentToken() {
        return current;
      },
      isCurrent(token) {
        return token === current;
      },
      retain(token, request) {
        if (request) inFlight.set(request, token);
      },
      release(request) {
        inFlight.delete(request);
      },
      // Hands back every retained request that is no longer current and
      // forgets them, so the caller can abort them exactly once.
      takeSuperseded() {
        const stale = [];
        for (const entry of Array.from(inFlight)) {
          if (entry[1] !== current) {
            stale.push(entry[0]);
            inFlight.delete(entry[0]);
          }
        }
        return stale;
      },
      pending() {
        return inFlight.size;
      },
    };
  };

  // Scheduling is injected rather than calling setTimeout directly so tests
  // can drive a fake clock instead of waiting on a real one.
  const createDebouncer = (options) => {
    const run = options.run;
    const delay = options.delay;
    const schedule = options.schedule;
    const cancelScheduled = options.cancel;
    let handle = null;

    const fire = () => {
      handle = null;
      run();
    };

    return {
      request() {
        if (handle !== null) cancelScheduled(handle);
        handle = schedule(fire, delay);
      },
      flush() {
        if (handle === null) return false;
        cancelScheduled(handle);
        handle = null;
        run();
        return true;
      },
      cancel() {
        if (handle === null) return false;
        cancelScheduled(handle);
        handle = null;
        return true;
      },
      isPending() {
        return handle !== null;
      },
    };
  };

  // htmx emits historyCacheMiss before it opens and sends its XHR. Deferring
  // an abort until the current event stack finishes lets htmx complete that
  // setup while still preventing the obsolete response from loading.
  const defer = (run) => {
    if (typeof queueMicrotask === "function") {
      queueMicrotask(run);
    } else {
      Promise.resolve().then(run);
    }
  };

  // Canonical query string for an ordered list of name/value pairs. Empty
  // values are dropped, except for names the caller keeps deliberately, so
  // two identical filter states always serialize to the same string.
  const serializeFilters = (pairs, options) => {
    const keepEmpty = (options && options.keepEmpty) || [];
    const parts = [];
    for (const pair of pairs) {
      const name = pair[0];
      const raw = pair[1];
      const value = raw === undefined || raw === null ? "" : String(raw);
      if (value === "" && keepEmpty.indexOf(name) === -1) continue;
      parts.push(`${encodeURIComponent(name)}=${encodeURIComponent(value)}`);
    }
    return parts.join("&");
  };

  // Archive URLs describe the applied range with concrete bounds. A selected
  // preset is only a request-time instruction, because its resolved dates
  // must not change when a later filter is applied after a month rollover.
  const archiveFilterQuery = (pairs, options) => {
    const values = new Map(pairs.map((pair) => [pair[0], pair[1]]));
    const transientPreset = Boolean(options && options.transientPreset);
    const names = transientPreset
      ? ["q", "category", "date_preset", "from", "to", "vehicle", "exclusion"]
      : ["q", "category", "from", "to", "vehicle", "exclusion"];
    return serializeFilters(
      names.map((name) => [
        name,
        transientPreset && (name === "from" || name === "to")
          ? ""
          : values.get(name),
      ]),
      transientPreset ? { keepEmpty: ["date_preset"] } : undefined,
    );
  };

  const archiveStateQuery = (state) => archiveFilterQuery([
    ["q", state && state.q],
    ["category", state && state.category],
    ["from", state && state.from],
    ["to", state && state.to],
    ["vehicle", state && state.vehicle],
    ["exclusion", state && state.exclusion],
  ]);

  // A write owns the archive until its applied-view refresh succeeds. Draft
  // filter intent can be recorded while that gate is held, but no second
  // write or refresh may start ahead of it.
  const createWriteCoordinator = () => {
    let phase = "idle";
    let writeQuery = null;
    let draftPending = false;

    const takeDraft = () => {
      const pending = draftPending;
      draftPending = false;
      return pending;
    };

    return {
      begin(query) {
        if (phase !== "idle") return false;
        phase = "posting";
        writeQuery = query;
        return true;
      },
      deferDraft() {
        if (phase === "idle") return false;
        draftPending = true;
        return true;
      },
      startRefresh() {
        if (phase !== "posting") return null;
        phase = "refreshing";
        return writeQuery;
      },
      retryRefresh() {
        return phase === "refreshing" ? writeQuery : null;
      },
      completeRefresh() {
        if (phase !== "refreshing") return false;
        phase = "idle";
        writeQuery = null;
        return takeDraft();
      },
      failPost() {
        if (phase !== "posting") return false;
        phase = "idle";
        writeQuery = null;
        return takeDraft();
      },
      isBusy() {
        return phase !== "idle";
      },
      isRefreshing() {
        return phase === "refreshing";
      },
      pendingDraft() {
        return draftPending;
      },
      query() {
        return writeQuery;
      },
    };
  };

  const createHistoryRetry = () => {
    let intendedPath = null;
    return {
      start(path) {
        intendedPath = path || "/trips";
      },
      clear() {
        intendedPath = null;
      },
      pending() {
        return intendedPath !== null;
      },
      path() {
        return intendedPath;
      },
    };
  };

  // A cache-miss restore can arrive out of order with a newer miss. The path
  // captured when the miss began is the user's latest intent, so its restore
  // must not replace that path. A cache hit has no initiation event and can
  // establish the path from its restore event instead.
  const historyRestorePath = (currentPath, detail) => {
    const path = detail && detail.path;
    if (detail && detail.cacheMiss && currentPath) return currentPath;
    return path || currentPath || "/trips";
  };

  const sameFilters = (a, b) => a === b;

  // Transient per-month depth for a refresh: how many rows each month has
  // already rendered, so the refresh redraws the same depth instead of
  // collapsing every month back to its first page. Entries are
  // [month key, rendered row count] pairs. Zero is meaningful (a month whose
  // rows were all removed), so it is kept rather than dropped.
  const loadedDepthMap = (entries) => {
    const depths = {};
    for (const entry of entries) {
      const month = entry[0];
      const rendered = entry[1];
      if (!month) continue;
      depths[month] = rendered;
    }
    return depths;
  };

  // History state is user-controlled browser data. Keep malformed values out
  // of the transient server query rather than allowing an arbitrary object,
  // negative count, or unsafe integer to become a pagination depth.
  const validateLoadedDepth = (value) => {
    if (value === undefined || value === null) return {};
    if (typeof value !== "object" || Array.isArray(value)) return null;
    const depths = {};
    for (const [month, rendered] of Object.entries(value)) {
      if (!/^\d{4}-(0[1-9]|1[0-2])$/.test(month)) return null;
      if (!Number.isSafeInteger(rendered) || rendered < 0 || rendered > 100000) {
        return null;
      }
      depths[month] = rendered;
    }
    return depths;
  };

  // Merge depth from a restored document with the saved per-entry depth. A
  // restored snapshot may already contain more rows than the state captured
  // before it, and reducing that count would cause a duplicate pager request.
  const mergeLoadedDepth = (saved, rendered) => {
    const savedDepth = validateLoadedDepth(saved);
    const renderedDepth = validateLoadedDepth(rendered);
    if (savedDepth === null || renderedDepth === null) return null;
    const merged = { ...renderedDepth };
    for (const [month, depth] of Object.entries(savedDepth)) {
      merged[month] = Math.max(merged[month] || 0, depth);
    }
    return merged;
  };

  const loadedDepthRestore = (saved, rendered) => {
    const savedDepth = validateLoadedDepth(saved);
    const renderedDepth = validateLoadedDepth(rendered);
    if (savedDepth === null || renderedDepth === null) {
      return { valid: false, depth: {}, needsRestore: false };
    }
    const depth = mergeLoadedDepth(savedDepth, renderedDepth);
    const needsRestore = Object.entries(depth).some(([month, count]) => (
      count > (renderedDepth[month] || 0)
    ));
    return { valid: true, depth, needsRestore };
  };

  const filterDisclosureState = (isMobile, isOpen) => ({
    showToggle: Boolean(isMobile),
    hideControls: Boolean(isMobile && !isOpen),
  });

  const SELECTION_REFRESH = "refresh";
  const SELECTION_NAVIGATE = "navigate";

  // The one place that decides which selected ids survive a change to the
  // rows on screen.
  //
  // A refresh keeps every selected id, including ids with no rendered row: a
  // batch update can move a trip outside the current filters without
  // deleting it, and it stays a valid target for the next batch write. A
  // deliberate filter navigation ends the whole selection instead. Deletion
  // is the only case where a missing row means the id is gone, and it prunes
  // itself explicitly rather than being inferred here.
  const reconcileSelection = (selectedIds, renderedIds, options) => {
    const intent = (options && options.intent) || SELECTION_REFRESH;
    const rendered = new Set(renderedIds);
    const selected = intent === SELECTION_NAVIGATE ? [] : Array.from(selectedIds);
    return {
      selected,
      rendered: selected.filter((id) => rendered.has(id)),
      outside: selected.filter((id) => !rendered.has(id)),
    };
  };

  // The action bar counts the whole selection, so it has to say when part of
  // it is not on screen rather than implying every selected trip is visible.
  const selectionCountLabel = (counts) => {
    const total = counts.total;
    const outside = Math.max(total - counts.rendered, 0);
    const label = `${total} ${total === 1 ? "trip" : "trips"} selected`;
    return outside > 0 ? `${label} (${outside} outside this view)` : label;
  };

  // Merge eligibility is read off the rows currently on screen, so a
  // selection reaching outside the view cannot establish it. Remembered row
  // metadata must never stand in for a row that is not rendered.
  const canMergeSelection = (counts) => counts.total >= 2 && counts.rendered === counts.total;

  const ArchiveState = {
    createRequestScope,
    createDebouncer,
    defer,
    serializeFilters,
    archiveFilterQuery,
    archiveStateQuery,
    createWriteCoordinator,
    createHistoryRetry,
    historyRestorePath,
    sameFilters,
    loadedDepthMap,
    validateLoadedDepth,
    mergeLoadedDepth,
    loadedDepthRestore,
    filterDisclosureState,
    reconcileSelection,
    selectionCountLabel,
    canMergeSelection,
  };

  if (typeof window !== "undefined") window.ArchiveState = ArchiveState;
  if (typeof module !== "undefined" && module.exports) module.exports = ArchiveState;
})();
