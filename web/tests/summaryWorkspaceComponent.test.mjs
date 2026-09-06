import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

const summarySource = readFileSync(new URL("../src/components/SummaryWorkspace.tsx", import.meta.url), "utf8");
const decisionSource = readFileSync(new URL("../src/components/TermDecisionWorkspace.tsx", import.meta.url), "utf8");
const termsSource = readFileSync(new URL("../src/components/TermsView.tsx", import.meta.url), "utf8");
const appSource = readFileSync(new URL("../src/App.tsx", import.meta.url), "utf8");
const i18nSource = readFileSync(new URL("../src/i18n.ts", import.meta.url), "utf8");

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

test("summary selection controls use the active scope and only show its actions", () => {
  assert.match(summarySource, /const hasFilter = normalized\.length > 0/);
  assert.match(summarySource, /hasFilter \? visibleSelection : null/);
  assert.match(summarySource, /hasFilter && !visible\.length/);
  assert.match(summarySource, /hasFilter \? "terms\.summarySelectFiltered" : "terms\.summarySelectAll"/);
  assert.match(summarySource, /hasFilter \? "terms\.summaryDeselectFiltered" : "terms\.summaryDeselectAll"/);
});

test("summary Part rows reuse classic selection and batch visible selected checkboxes", () => {
  assert.match(summarySource, /import \{ useClassicSelection \} from "\.\.\/useClassicSelection"/);
  assert.match(summarySource, /const partSelection = useClassicSelection\(\)/);
  assert.match(summarySource, /const visibleBoundaryKeys = visible\.map/);
  assert.match(summarySource, /partSelection\.select\(key, visibleBoundaryKeys, event\)/);
  assert.match(summarySource, /const selectedVisibleKeys = visibleBoundaryKeys\.filter/);
  assert.match(summarySource, /partSelection\.selectedKeys\.has\(key\) && selectedVisibleKeys\.length > 1/);
  assert.match(summarySource, /toggleFilteredSelection\(selectionBoundaries, participationStateRef\.current\.displayed, participationTargets\(key\)/);
  assert.match(summarySource, /rowSelected \? " selected" : ""/);
});

test("summary selection labels omit the project qualifier outside filtering", () => {
  assert.match(i18nSource, /"terms\.summarySelectAll": "全选"/);
  assert.match(i18nSource, /"terms\.summaryDeselectAll": "取消全选"/);
  assert.match(i18nSource, /"terms\.summarySelectFiltered": "筛选结果全选"/);
  assert.match(i18nSource, /"terms\.summaryDeselectFiltered": "筛选结果取消全选"/);
  assert.match(i18nSource, /"terms\.summarySelectAll": "Select all"/);
  assert.match(i18nSource, /"terms\.summaryDeselectAll": "Deselect all"/);
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

test("summary workspace keeps export errors inside the modal and distinguishes success text", () => {
  assert.match(summarySource, /error && <p className="error-text summary-message">\{error\}<\/p>/);
  assert.match(summarySource, /setDialogError\(errText\)/);
  assert.match(summarySource, /className=\{`inline-message \$\{message\.type === "success" \? "success-text" : "error-text"\}`\}/);
  assert.doesNotMatch(summarySource, /conflictError|setConflictError/);
});

test("summary fragment runs use task options and the standard run dialog", () => {
  assert.match(summarySource, /include_summaries=true&language=/);
  assert.match(summarySource, /setRunOptions\(options\)/);
  assert.match(summarySource, /<RunDialog/);
  assert.doesNotMatch(summarySource, /summary_prompt_preflight|preflight\.promptOk|preflight\.missing/);
});

test("summary runs reuse participation and the standard run dialog", () => {
  assert.match(summarySource, /import \{ RunDialog \} from "\.\/RunDialog"/);
  assert.match(summarySource, /task-options\/content_summary/);
  assert.match(summarySource, /summary_selection/);
  assert.match(summarySource, /include_summaries: true/);
  assert.match(summarySource, /<RunDialog/);
  assert.match(summarySource, /\.\.\.decision/);
  assert.doesNotMatch(summarySource, /aggregation-preflight|summaries\/aggregate/);
  assert.doesNotMatch(summarySource, /SelectionDialog mode="aggregate"/);
});

test("summary export keeps its independent selection dialog", () => {
  assert.match(summarySource, /dialog === "export" && <SelectionDialog/);
  assert.doesNotMatch(summarySource, /SelectionDialog mode=/);
});

test("expired full summaries remain visible with a warning and retry action", () => {
  assert.match(summarySource, /summaryArtifactIsExpired/);
  assert.match(summarySource, /summaryArtifactExpiryReason/);
  assert.match(summarySource, /summaryDependencyChanged/);
  assert.match(summarySource, /summaryProvenanceUnavailable/);
  assert.match(summarySource, /terms\.summaryArtifactExpired/);
  assert.match(summarySource, /summary-artifact-warning/);
  assert.match(summarySource, /artifact\.text/);
});

test("summary retry actions keep source first and the selection hint below the button row", () => {
  const cardSource = summarySource.slice(summarySource.indexOf("function SummaryArtifactCard"));
  assert.match(cardSource, /summary-artifact-actions[\s\S]*terms\.summarySource[\s\S]*terms\.summaryRetry[\s\S]*<\/div>\s*\{retryable && retryDisabled && <small>/);
  assert.match(cardSource, /summary-artifact-footer[\s\S]*summary-artifact-actions[\s\S]*terms\.summarySource[\s\S]*terms\.summaryRetry[\s\S]*<\/div>[\s\S]*<\/div>\s*\{expired && retryDisabled && <small>/);
});

test("full summary selection prefers a current result over an expired result", () => {
  assert.match(summarySource, /values\.filter\(\(item\) => \([\s\S]*!summaryArtifactIsExpired\(item\)/);
  assert.match(summarySource, /current\[current\.length - 1\] \?\? values\[values\.length - 1\]/);
});
