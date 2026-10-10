import assert from "node:assert/strict";
import test from "node:test";
import {compactSegmentReferences} from "../src/segmentReferences.ts";
test("compresses consecutive references without hiding gaps or file changes", () => {
  assert.deepEqual(compactSegmentReferences(["F0001-S000001","F0001-S000002","F0001-S000003","F0001-S000005","F0002-S000006"]), ["F0001-S000001—F0001-S000003","F0001-S000005","F0002-S000006"]);
  assert.deepEqual(compactSegmentReferences(["1","2","4","4","5"]), ["1—2","4","4—5"]);
  assert.deepEqual(compactSegmentReferences(["SUMMARY-A1","SUMMARY-A2"]), ["SUMMARY-A1","SUMMARY-A2"]);
});
