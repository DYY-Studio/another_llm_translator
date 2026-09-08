import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

const appSource = readFileSync(new URL("../src/App.tsx", import.meta.url), "utf8");
const pickerSource = readFileSync(new URL("../src/components/ProjectPicker.tsx", import.meta.url), "utf8");

test("project activation opens the project before selecting it", () => {
  assert.match(appSource, /const openProject = useCallback/);
  assert.match(appSource, /\/api\/v1\/projects\/open/);
  assert.match(appSource, /onProject=\{selectProject\}/);
});

test("project activation keeps template repair warnings visible", () => {
  assert.match(appSource, /projectWarnings/);
  assert.match(appSource, /value\.warnings/);
  assert.match(appSource, /<button className="warning-banner warning-banner-sticky" onClick=\{\(\) => setProjectWarnings\(\[\]\)\}/);
});

test("project picker passes the selected project summary to activation", () => {
  assert.match(pickerSource, /onProject: \(value: ProjectSummary\) => void/);
  assert.match(pickerSource, /onProject\(item\)/);
});

test("task activation stops navigation when project opening fails", () => {
  const taskActivationIndex = appSource.indexOf("async function openTaskProject");
  const guardIndex = appSource.indexOf(
    "if (!(await openProject(summary, true))) return;",
    taskActivationIndex,
  );
  const navigationIndex = appSource.indexOf("setStage(destination);", taskActivationIndex);

  assert.ok(taskActivationIndex >= 0);
  assert.ok(guardIndex > taskActivationIndex);
  assert.ok(navigationIndex > guardIndex);
});

test("recent project restoration waits for auth and resumes after login", () => {
  const restoreIndex = appSource.indexOf("const restoreRecentProjects = useCallback");
  const restoreEnd = appSource.indexOf("// Warm the terminology", restoreIndex);
  const restoreSource = appSource.slice(restoreIndex, restoreEnd);

  assert.ok(restoreIndex >= 0);
  assert.match(restoreSource, /errorPayloadFrom\(result\.reason\)\?\.code === "auth_required"/);
  assert.match(restoreSource, /setRecentProjectsReady\(false\)/);
  assert.ok(restoreSource.indexOf("if (authRequired)") < restoreSource.indexOf("writeRecentProjectPaths"));
  assert.ok(restoreSource.indexOf("await loadProjects()") < restoreSource.indexOf("setRecentProjectsReady(true)"));

  const startupIndex = appSource.indexOf("if (!serverStatus || (serverStatus.auth.required && !serverStatus.authed)) return;");
  assert.ok(startupIndex > restoreIndex);

  const loginIndex = appSource.indexOf("onLoggedIn={() => {");
  const loginSource = appSource.slice(loginIndex, appSource.indexOf("}}", loginIndex));
  assert.match(loginSource, /restoreRecentProjects/);
});
