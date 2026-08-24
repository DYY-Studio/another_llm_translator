import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

test("restores active tasks and remembers the selected project identity", async () => {
  const source = await readFile(new URL("../src/App.tsx", import.meta.url), "utf8");

  assert.match(source, /\/api\/v1\/tasks\/active/);
  assert.match(source, /another-llm-translator\.selected-project\.v1/);
  assert.match(source, /tasks\[selectedProject\.project_id\]/);
  assert.match(source, /item\.project_id === storedProjectId/);
  assert.match(source, /updateTask\(value\)/);
});

test("marks other running projects without creating a multi-task status bar", async () => {
  const source = await readFile(new URL("../src/components/UtilityViews.tsx", import.meta.url), "utf8");

  assert.match(source, /runningProjectIds/);
  assert.match(source, /project\.otherRunning/);
  assert.match(source, /project-running-badge/);
  assert.doesNotMatch(source, /tasks\.map\(/);
});
