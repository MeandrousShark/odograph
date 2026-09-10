// Behavioral tests for static/review_state.js, run by Node's built-in test
// runner. The module is loaded the same way the browser loads it (a classic
// script with a guarded CommonJS export), so these exercise the real file
// rather than a copy of its logic.
const test = require("node:test");
const assert = require("node:assert");
const path = require("node:path");

const ReviewState = require(
  path.join(__dirname, "..", "..", "static", "review_state.js")
);

test("activating an option with nothing committed commits that option", () => {
  assert.strictEqual(ReviewState.categoryAfterActivation(null, "business"), "business");
  assert.strictEqual(ReviewState.categoryAfterActivation(null, "personal"), "personal");
});

test("re-activating the committed option clears the selection", () => {
  assert.strictEqual(ReviewState.categoryAfterActivation("business", "business"), null);
  assert.strictEqual(ReviewState.categoryAfterActivation("personal", "personal"), null);
});

test("activating the other option switches instead of clearing", () => {
  assert.strictEqual(ReviewState.categoryAfterActivation("business", "personal"), "personal");
  assert.strictEqual(ReviewState.categoryAfterActivation("personal", "business"), "business");
});

test("advancing stays blocked while nothing is committed", () => {
  assert.strictEqual(ReviewState.canAdvance(null), false);
  assert.strictEqual(ReviewState.canAdvance(undefined), false);
  assert.strictEqual(ReviewState.canAdvance(""), false);
});

test("the unclassified placeholder never enables advancing", () => {
  assert.strictEqual(ReviewState.canAdvance("unclassified"), false);
});

test("advancing is allowed once a real category is committed", () => {
  assert.strictEqual(ReviewState.canAdvance("business"), true);
  assert.strictEqual(ReviewState.canAdvance("personal"), true);
});

test("clearing a selection leaves advancing blocked again", () => {
  const cleared = ReviewState.categoryAfterActivation("business", "business");

  assert.strictEqual(cleared, null);
  assert.strictEqual(ReviewState.canAdvance(cleared), false);
});

test("review writes serialize and an advancing request reads newer visible values", () => {
  const coordinator = ReviewState.createWriteCoordinator();
  const sent = [];
  let visible = { purpose: "old", notes: "first" };
  const autosave = {
    kind: "field",
    values: () => ({ ...visible }),
  };
  const advance = {
    kind: "advance",
    values: () => ({ ...visible }),
  };

  assert.strictEqual(coordinator.offer(autosave), true);
  sent.push(autosave.values());
  visible = { purpose: "new", notes: "latest" };
  assert.strictEqual(coordinator.offer(advance), false);
  assert.strictEqual(coordinator.pending(), 1);

  const next = coordinator.settle(autosave);
  assert.strictEqual(next, advance);
  sent.push(next.values());
  assert.deepStrictEqual(sent, [
    { purpose: "old", notes: "first" },
    { purpose: "new", notes: "latest" },
  ]);
});

test("a failed autosave still releases the lane for Next or Skip", () => {
  const coordinator = ReviewState.createWriteCoordinator();
  const failed = { kind: "field" };
  const advance = { kind: "advance" };

  assert.strictEqual(coordinator.offer(failed), true);
  assert.strictEqual(coordinator.offer(advance), false);
  // The queue does not treat a failed response as cancellation. The caller
  // can surface the error while the next request remains retryable.
  assert.strictEqual(coordinator.settle(failed, false), advance);
  assert.strictEqual(coordinator.isBusy(), true);
  assert.strictEqual(coordinator.settle(advance, true), null);
  assert.strictEqual(coordinator.isBusy(), false);
});

test("an active Next, Skip, or Undo locks fields until its response settles", () => {
  const coordinator = ReviewState.createWriteCoordinator();
  const advance = { kind: "advance" };
  const autosave = { kind: "field" };

  assert.strictEqual(coordinator.offer(advance), true);
  assert.strictEqual(coordinator.isInputLocked(), true);
  // A field event that was already queued cannot unlock or overtake the
  // advancing write.
  assert.strictEqual(coordinator.offer(autosave), false);
  assert.strictEqual(coordinator.isInputLocked(), true);
  assert.strictEqual(coordinator.settle(advance, false), autosave);
  assert.strictEqual(coordinator.isInputLocked(), false);

  const undo = { kind: "undo" };
  assert.strictEqual(coordinator.settle(autosave, true), null);
  assert.strictEqual(coordinator.offer(undo), true);
  assert.strictEqual(coordinator.isInputLocked(), true);
  assert.strictEqual(coordinator.settle(undo, false), null);
  assert.strictEqual(coordinator.isInputLocked(), false);
});
