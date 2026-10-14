import assert from "node:assert/strict";
import test from "node:test";
import { compactKnowledgeRanges } from "../src/termKnowledge.ts";

const range = (id, file = "F1", part = "body", start = 0, end = 10) => ({segment_id: id, file_id: file, part_id: part, start, end});

test("compacts adjacent source coverage and navigates to the first Segment", () => {
  const result = compactKnowledgeRanges({
    source_range: [range("S4"), range("S9"), range("S10")],
    _positions: {S4: 0, S9: 1, S10: 2},
  });
  assert.equal(result.length, 1);
  assert.equal(result[0].label, "S4 — S10");
  assert.equal(result[0].segmentId, "S4");
});

test("keeps gaps and content boundaries separate while combining slices of one Segment", () => {
  const result = compactKnowledgeRanges({
    source_range: [range("S1", "F1", "body", 2, 5), range("S1", "F1", "body", 5, 8),
      range("S3"), range("S4", "F1", "appendix"), range("S5", "F2")],
    _positions: {S1: 0, S3: 2, S4: 3, S5: 4},
  });
  assert.equal(result.length, 4);
  assert.equal(result[0].label, "S1 [2–8]");
  assert.deepEqual(result.map((item) => item.segmentId), ["S1", "S3", "S4", "S5"]);
});
