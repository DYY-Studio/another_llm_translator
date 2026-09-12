import assert from "node:assert/strict";
import test from "node:test";

import {
  clearSegmentReadError,
  emptySegmentReadErrors,
  firstSegmentReadError,
  setSegmentReadError,
} from "../src/segmentReadState.ts";

test("clears only the read that succeeded", () => {
  let errors = emptySegmentReadErrors();
  errors = setSegmentReadError(errors, "index", "index failed");
  errors = setSegmentReadError(errors, "page", "page failed", "page-1");
  errors = setSegmentReadError(errors, "detail", "detail failed");

  errors = clearSegmentReadError(errors, "page", "page-1");

  assert.equal(errors.index, "index failed");
  assert.deepEqual(errors.pages, {});
  assert.equal(errors.detail, "detail failed");
  assert.equal(firstSegmentReadError(errors), "index failed");
});

test("reports a page failure when index and detail reads are healthy", () => {
  let errors = emptySegmentReadErrors();
  errors = setSegmentReadError(errors, "page", "page failed", "page-2");

  assert.equal(firstSegmentReadError(errors), "page failed");
});
