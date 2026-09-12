import assert from "node:assert/strict";
import test from "node:test";

import {
  createTermDecisionWorkspaceState,
  restoreTermDecisionWorkspaceState,
  updateTermDecisionWorkspaceState,
} from "../src/termDecisionWorkspaceState.ts";

test("restores decision filters, page, tab, and scroll per project", () => {
  const cache = new Map();
  const state = updateTermDecisionWorkspaceState(createTermDecisionWorkspaceState("manual"), {
    search: "dragon",
    kind: "relationship",
    status: "rejected",
    page: 3,
    scrollTop: 420,
  });
  cache.set("project-a", state);

  assert.deepEqual(restoreTermDecisionWorkspaceState(cache, "project-a", "proposals"), state);
  assert.equal(restoreTermDecisionWorkspaceState(cache, "project-b", "proposals").tab, "proposals");
});
