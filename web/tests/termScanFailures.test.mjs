import assert from "node:assert/strict";
import test from "node:test";
import { compactScanFailures } from "../src/termScanFailures.ts";

const failure = (segment_id, message = "timeout") => ({
  segment_id, error_class: "request_error", error_message: message,
});

test("merges adjacent failures in document order and summarizes their errors", () => {
  const ranges = compactScanFailures([
    failure("F1-S000002", "invalid response"), failure("F1-S000001"), failure("F1-S000009"),
  ], ["F1-S000001", "F1-S000009", "F1-S000002"]);
  assert.equal(ranges.length, 1);
  assert.equal(ranges[0].first, "F1-S000001");
  assert.equal(ranges[0].last, "F1-S000002");
  assert.equal(ranges[0].count, 3);
  assert.deepEqual(ranges[0].errors, [
    { error_class: "request_error", error_message: "timeout", count: 2 },
    { error_class: "request_error", error_message: "invalid response", count: 1 },
  ]);
});

test("keeps successful gaps and different files out of a failure range", () => {
  const ranges = compactScanFailures([
    failure("F1-S000001"), failure("F1-S000003"), failure("F2-S000001"),
  ], ["F1-S000001", "F1-S000002", "F1-S000003", "F2-S000001"]);
  assert.deepEqual(ranges.map(({ first, last, count }) => [first, last, count]), [
    ["F1-S000001", "F1-S000001", 1],
    ["F1-S000003", "F1-S000003", 1],
    ["F2-S000001", "F2-S000001", 1],
  ]);
});

test("reports unavailable Segment positions instead of inventing adjacency", () => {
  assert.throws(() => compactScanFailures([failure("F1-S000001")], []), /F1-S000001/);
  assert.deepEqual(compactScanFailures([], []), []);
});
