// Category selection decisions for the review page.
//
// This is deliberately separate from the page's inline script: it isolates
// the two rules that decide what a click on the category pair means and when
// the advancing action is allowed, so they can be exercised without a
// browser. Nothing here touches the DOM, htmx, or the clock. It loads as a
// classic browser script and as a CommonJS module, so the same file runs in
// both places with no build step.
(() => {
  "use strict";

  // Review only ever offers Business and Personal, so re-activating the
  // committed option is the only way back to no category short of Skip. The
  // caller compares against the value it tracked before the browser's
  // pre-click activation, because by then the radio is already checked.
  const categoryAfterActivation = (committed, activated) =>
    (committed === activated ? null : activated);

  // Advancing writes a category, so neither "nothing chosen" nor the
  // unclassified placeholder the other category surfaces still offer can
  // enable it.
  const ADVANCING_CATEGORIES = ["business", "personal"];
  const canAdvance = (committed) => ADVANCING_CATEGORIES.indexOf(committed) !== -1;

  // Review writes share one form, so every request can carry the other
  // visible fields. Keep them in one lane instead of letting an older
  // autosave finish after a newer edit or an advancing request. The caller
  // replays the next entry from its live source after the prior request
  // settles, which makes the request collect current form values.
  const createWriteCoordinator = () => {
    let active = null;
    const queued = [];

    return {
      offer(entry) {
        if (active !== null) {
          queued.push(entry);
          return false;
        }
        active = entry;
        return true;
      },
      settle(entry) {
        if (active !== entry) return null;
        active = queued.shift() || null;
        return active;
      },
      skip() {
        if (active === null) return null;
        active = queued.shift() || null;
        return active;
      },
      active() {
        return active;
      },
      isInputLocked() {
        return active !== null && ["advance", "undo"].indexOf(active.kind) !== -1;
      },
      isBusy() {
        return active !== null;
      },
      pending() {
        return queued.length;
      },
    };
  };

  const ReviewState = {
    categoryAfterActivation,
    canAdvance,
    createWriteCoordinator,
  };

  if (typeof window !== "undefined") window.ReviewState = ReviewState;
  if (typeof module !== "undefined" && module.exports) module.exports = ReviewState;
})();
