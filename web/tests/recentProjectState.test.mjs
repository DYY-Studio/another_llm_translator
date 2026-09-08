import assert from "node:assert/strict";
import test from "node:test";

import { reconcileRecentProjectPaths } from "../src/recentProjectState.ts";

test("removes only confirmed invalid paths and retains transient failures", () => {
  assert.deepEqual(
    reconcileRecentProjectPaths([
      { path: "/old/a", openedPath: "/real/a" },
      { path: "/old/b", errorCode: "project_error" },
      { path: "/old/c", errorCode: "internal_error" },
      { path: "/old/d", errorCode: "storage_error" },
    ]),
    {
      paths: ["/real/a", "/old/c", "/old/d"],
      invalidCount: 1,
      transientFailureCount: 2,
    },
  );
});

test("keeps successful paths in their existing recency order", () => {
  assert.deepEqual(
    reconcileRecentProjectPaths([
      { path: "/new", openedPath: "/new" },
      { path: "/old", openedPath: "/old" },
    ]).paths,
    ["/new", "/old"],
  );
});
