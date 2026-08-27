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

test("reserves the measured mobile run status height for sticky content", async () => {
  const shell = await readFile(new URL("../src/components/AppShell.tsx", import.meta.url), "utf8");
  const styles = await readFile(new URL("../src/styles.css", import.meta.url), "utf8");

  assert.match(shell, /ResizeObserver/);
  assert.match(shell, /--mobile-run-status-height/);
  assert.match(shell, /\[\"queued\", \"running\"\]\.includes\(task\.status\)/);
  assert.match(styles, /\.settings-navigation \{ position: sticky; top: calc\(58px \+ var\(--mobile-run-status-height\)\)/);
  assert.match(styles, /\.term-decision-sticky \{ top: calc\(58px \+ var\(--mobile-run-status-height\)\)/);
  assert.match(styles, /\.warning-banner-sticky \{ top: calc\(58px \+ var\(--mobile-run-status-height\)\)/);
});
