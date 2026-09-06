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
