import assert from "node:assert/strict";
import test from "node:test";

import {
  boundarySelectionState,
  fileSelectionState,
  toggleFilteredSelection,
} from "../src/summarySelection.ts";

const boundaries = [
  { key: "a:one", fileId: "a", partId: "one" },
  { key: "a:two", fileId: "a", partId: "two" },
  { key: "b:one", fileId: "b", partId: "one" },
];

test("project selection ignores the active search filter and reports hidden selections", () => {
  const selected = new Set(["a:one"]);
  const result = boundarySelectionState(boundaries, selected, [boundaries[0], boundaries[1]]);

  assert.deepEqual(result, {
    selectedCount: 1,
    visibleCount: 2,
    hiddenSelectedCount: 0,
    allSelected: false,
    visibleAllSelected: false,
  });

  const all = toggleFilteredSelection(boundaries, selected, null, true);
  assert.deepEqual([...all].sort(), ["a:one", "a:two", "b:one"]);
  const visibleOnly = toggleFilteredSelection(boundaries, all, [boundaries[0], boundaries[1]], false);
  assert.deepEqual([...visibleOnly], ["b:one"]);
});

test("file checkboxes expose checked, unchecked, and partial states", () => {
  assert.equal(fileSelectionState(boundaries, new Set(), "a"), "unchecked");
  assert.equal(fileSelectionState(boundaries, new Set(["a:one"]), "a"), "partial");
  assert.equal(fileSelectionState(boundaries, new Set(["a:one", "a:two"]), "a"), "checked");
  assert.equal(fileSelectionState(boundaries, new Set(["a:one", "a:two"]), "b"), "unchecked");
});
