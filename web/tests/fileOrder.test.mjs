import assert from "node:assert/strict";
import test from "node:test";

import { moveFileBlock, moveFilesByCommand } from "../src/fileOrder.ts";

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

test("moves one file with top, up, down, and bottom commands", () => {
  assert.deepEqual(moveFilesByCommand(ORDER, ["C"], "top"), ["C", "A", "B", "D", "E"]);
  assert.deepEqual(moveFilesByCommand(ORDER, ["C"], "up"), ["A", "C", "B", "D", "E"]);
  assert.deepEqual(moveFilesByCommand(ORDER, ["C"], "down"), ["A", "B", "D", "C", "E"]);
  assert.deepEqual(moveFilesByCommand(ORDER, ["C"], "bottom"), ["A", "B", "D", "E", "C"]);
});

test("keeps the current order for command boundaries and unknown files", () => {
  assert.deepEqual(moveFilesByCommand(ORDER, ["A"], "top"), ORDER);
  assert.deepEqual(moveFilesByCommand(ORDER, ["A"], "up"), ORDER);
  assert.deepEqual(moveFilesByCommand(ORDER, ["E"], "down"), ORDER);
  assert.deepEqual(moveFilesByCommand(ORDER, ["E"], "bottom"), ORDER);
  assert.strictEqual(moveFilesByCommand(ORDER, ["X"], "top"), ORDER);
});

test("moves a contiguous selection as a block with every command", () => {
  assert.deepEqual(moveFilesByCommand(ORDER, ["B", "C"], "top"), ["B", "C", "A", "D", "E"]);
  assert.deepEqual(moveFilesByCommand(ORDER, ["B", "C"], "up"), ["B", "C", "A", "D", "E"]);
  assert.deepEqual(moveFilesByCommand(ORDER, ["B", "C"], "down"), ["A", "D", "B", "C", "E"]);
  assert.deepEqual(moveFilesByCommand(ORDER, ["B", "C"], "bottom"), ["A", "D", "E", "B", "C"]);
});

test("collapses a non-contiguous selection while preserving visual order", () => {
  assert.deepEqual(moveFilesByCommand(ORDER, ["D", "B"], "top"), ["B", "D", "A", "C", "E"]);
  assert.deepEqual(moveFilesByCommand(ORDER, ["D", "B"], "up"), ["B", "D", "A", "C", "E"]);
  assert.deepEqual(moveFilesByCommand(ORDER, ["B", "D"], "down"), ["A", "C", "E", "B", "D"]);
  assert.deepEqual(moveFilesByCommand(ORDER, ["D", "B"], "bottom"), ["A", "C", "E", "B", "D"]);
});

test("keeps a multi-file order at its command boundary", () => {
  assert.strictEqual(moveFilesByCommand(ORDER, ["A", "C"], "up"), ORDER);
  assert.strictEqual(moveFilesByCommand(ORDER, ["C", "E"], "down"), ORDER);
  assert.strictEqual(moveFilesByCommand(ORDER, ORDER, "top"), ORDER);
  assert.strictEqual(moveFilesByCommand(ORDER, ORDER, "bottom"), ORDER);
});

test("rejects duplicate, unknown, and empty multi-file commands", () => {
  assert.strictEqual(moveFilesByCommand(ORDER, [], "top"), ORDER);
  assert.strictEqual(moveFilesByCommand(ORDER, ["B", "B"], "top"), ORDER);
  assert.strictEqual(moveFilesByCommand(ORDER, ["X"], "top"), ORDER);
});
