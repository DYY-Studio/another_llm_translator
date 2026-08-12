import assert from "node:assert/strict";
import test from "node:test";

import { moveFileBlock } from "../src/fileOrder.ts";

const ORDER = ["A", "B", "C", "D", "E"];

test("moves a non-contiguous selection before or after a target", () => {
  assert.deepEqual(
    moveFileBlock(ORDER, ["B", "D"], "E", "before"),
    ["A", "C", "B", "D", "E"],
  );
  assert.deepEqual(
    moveFileBlock(ORDER, ["D", "B"], "E", "after"),
    ["A", "C", "E", "B", "D"],
  );
});

test("preserves visual order while moving a contiguous block to an edge", () => {
  assert.deepEqual(
    moveFileBlock(ORDER, ["C", "D"], "A", "before"),
    ["C", "D", "A", "B", "E"],
  );
  assert.deepEqual(
    moveFileBlock(ORDER, ["C", "D"], "E", "after"),
    ["A", "B", "E", "C", "D"],
  );
});

test("returns the current order for group targets and unchanged placements", () => {
  assert.strictEqual(moveFileBlock(ORDER, ["B", "D"], "D", "after"), ORDER);
  assert.deepEqual(
    moveFileBlock(ORDER, ["B", "C"], "D", "before"),
    ORDER,
  );
});

test("rejects empty, duplicate, unknown, and missing move inputs", () => {
  assert.strictEqual(moveFileBlock(ORDER, [], "C", "before"), ORDER);
  assert.strictEqual(moveFileBlock(ORDER, ["B", "B"], "C", "before"), ORDER);
  assert.strictEqual(moveFileBlock(ORDER, ["X"], "C", "before"), ORDER);
  assert.strictEqual(moveFileBlock(ORDER, ["B"], "X", "before"), ORDER);
});
