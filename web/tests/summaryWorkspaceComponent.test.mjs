import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

const summarySource = readFileSync(new URL("../src/components/SummaryWorkspace.tsx", import.meta.url), "utf8");
const decisionSource = readFileSync(new URL("../src/components/TermDecisionWorkspace.tsx", import.meta.url), "utf8");
const termsSource = readFileSync(new URL("../src/components/TermsView.tsx", import.meta.url), "utf8");
const appSource = readFileSync(new URL("../src/App.tsx", import.meta.url), "utf8");

test("summary tabs expose linked tab and tabpanel semantics", () => {
  assert.match(summarySource, /role="tablist" aria-label=\{translate\("terms\.summaryTabs"/);
  assert.match(summarySource, /role="tab" id="summary-full-tab" aria-selected=\{tab === "full"\} aria-controls="summary-full-panel"/);
  assert.match(summarySource, /role="tabpanel" aria-labelledby=\{tab === "full" \? "summary-full-tab" : "summary-fragment-tab"\}/);
});

test("decision tabs expose linked tab and tabpanel semantics", () => {
  assert.match(decisionSource, /role="tablist" aria-label=\{translate\("terms\.decisionTabs"/);
  assert.match(decisionSource, /role="tab" id="decision-proposals-tab" aria-selected=\{tab === "proposals"\} aria-controls="decision-proposals-panel"/);
  assert.match(decisionSource, /role="tabpanel" aria-labelledby=\{`decision-\$\{tab\}-tab`\}/);
});

test("summary UI keeps segment wording and separators in localized messages", () => {
  assert.doesNotMatch(summarySource, /segment_count\} Segment/);
  assert.doesNotMatch(summarySource, /labels\.join\("[、,]"\)/);
  assert.match(summarySource, /terms\.summaryBoundaryStats/);
  assert.match(summarySource, /terms\.summaryReferenceSeparator/);
});

test("decision prefetch keeps failures visible and retryable", () => {
  assert.match(termsSource, /decisionPrefetchError/);
  assert.match(termsSource, /setDecisionPrefetchAttempt\(\(value\) => value \+ 1\)/);
  assert.match(termsSource, /terms\.decisionPrefetchError/);
});

test("async workspace requests invalidate late project responses", () => {
  assert.match(decisionSource, /decisionRequestRef/);
  assert.match(decisionSource, /isCurrentProjectRequest\(/);
  assert.doesNotMatch(decisionSource, /const optionsRequest = api/);
  assert.match(termsSource, /loadMoreHits[\s\S]*isCurrentProjectRequest\(requestId/);
  assert.match(appSource, /await syncActiveTasks\(\);[\s\S]*isCurrentProjectRequest\(requestId/);
});

test("project refresh publishes the list before guarding automatic selection", () => {
  const setProjectsIndex = appSource.indexOf("setProjects(value.projects);");
  const syncIndex = appSource.indexOf("await syncActiveTasks();", setProjectsIndex);
  const projectGuardIndex = appSource.indexOf(
    "canAutoSelectProject(requestProject, activeProjectRef.current)",
  );
  const setProjectIndex = appSource.indexOf("setProject((current)", projectGuardIndex);

  assert.ok(setProjectsIndex >= 0);
  assert.ok(syncIndex > setProjectsIndex);
  assert.ok(projectGuardIndex > syncIndex);
  assert.ok(setProjectIndex > projectGuardIndex);
});
