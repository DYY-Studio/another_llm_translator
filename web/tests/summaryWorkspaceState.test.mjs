import assert from "node:assert/strict";
import test from "node:test";

import {
  applySummaryParticipationFetch,
  beginSummaryParticipation,
  createSummaryWorkspaceState,
  createSummaryParticipationState,
  rejectSummaryParticipationPut,
  resolveSummaryParticipationPut,
  restoreSummaryWorkspaceState,
  summaryArtifactSegmentIds,
  summaryProgress,
  termsSubpageForTask,
  updateSummaryWorkspaceState,
} from "../src/summaryWorkspaceState.ts";
import { canAutoSelectProject, isCurrentProjectRequest } from "../src/requestState.ts";

test("restores the summary subpage state per project without sharing it", () => {
  const initial = createSummaryWorkspaceState();
  const changed = updateSummaryWorkspaceState(initial, {
    search: "chapter",
    focusedBoundary: "file-a:part-2",
    tab: "fragment",
    sourceOpen: true,
    scrollTop: 240,
  });
  const cache = new Map([["project-a", changed]]);

  assert.deepEqual(restoreSummaryWorkspaceState(cache, "project-a"), changed);
  assert.deepEqual(restoreSummaryWorkspaceState(cache, "project-b"), createSummaryWorkspaceState());
});

test("keeps unspecified view state when one control changes", () => {
  const state = updateSummaryWorkspaceState(createSummaryWorkspaceState(), {
    tab: "fragment",
    focusedBoundary: "file-a:part-1",
    sourceOpen: true,
  });
  const next = updateSummaryWorkspaceState(state, { search: "term" });

  assert.equal(next.tab, "fragment");
  assert.equal(next.focusedBoundary, "file-a:part-1");
  assert.equal(next.sourceOpen, true);
  assert.equal(next.search, "term");
});

test("opens the correct terms subpage for task-bar tasks", () => {
  assert.equal(termsSubpageForTask("terminology_decision"), "decision");
  assert.equal(termsSubpageForTask("content_summary"), "summary");
  assert.equal(termsSubpageForTask("terminology", true), "summary");
  assert.equal(termsSubpageForTask("terminology", false), "library");
  assert.equal(termsSubpageForTask("translation"), "library");
});

test("counts only selected boundaries with complete full-summary coverage", () => {
  const boundaries = [
    { key: "F0001:p1", fileId: "F0001", partId: "p1", segmentCount: 2 },
    { key: "F0001:p2", fileId: "F0001", partId: "p2", segmentCount: 1 },
    { key: "F0002:p1", fileId: "F0002", partId: "p1", segmentCount: 1 },
  ];
  const artifacts = [
    { kind: "full", status: "completed", source_changed: false, file_id: "F0001", part_id: "p1", source_range: { segments: [{ segment_id: "F0001-S1" }, { segment_id: "F0001-S2" }] } },
    { kind: "fragment", status: "completed", source_changed: false, file_id: "F0001", part_id: "p2", source_range: { segments: [{ segment_id: "F0001-S3" }] } },
    { kind: "full", status: "completed", source_changed: true, file_id: "F0002", part_id: "p1", source_range: { segments: [{ segment_id: "F0002-S1" }] } },
  ];

  assert.deepEqual(summaryProgress(boundaries, new Set(["F0001:p1", "F0001:p2"]), artifacts), { done: 1, total: 2 });
});

test("resolves numeric summary references to stable source segment ids", () => {
  const artifact = {
    refs: ["2", "F0001-S3"],
    source_range: { segments: [{ segment_id: "F0001-S2" }, { segment_id: "F0001-S3" }] },
  };
  assert.deepEqual(summaryArtifactSegmentIds(artifact), ["F0001-S3"]);
});

test("protects optimistic participation from stale reads and confirms PUT responses", () => {
  let state = createSummaryParticipationState(new Set(["[\"F0001\",\"p1\"]"]));
  const requestVersion = state.version;
  const started = beginSummaryParticipation(state, new Set(["[\"F0001\",\"p1\"]", "[\"F0001\",\"p2\"]"]));
  state = started.state;

  const staleRead = applySummaryParticipationFetch(state, requestVersion, false, [
    { file_id: "F0001", part_id: "p1", selected: true },
  ]);
  assert.deepEqual([...staleRead.displayed], ["[\"F0001\",\"p1\"]", "[\"F0001\",\"p2\"]"]);

  const pendingRead = applySummaryParticipationFetch(state, state.version, true, [
    { file_id: "F0001", part_id: "p1", selected: true },
  ]);
  assert.deepEqual([...pendingRead.displayed], ["[\"F0001\",\"p1\"]", "[\"F0001\",\"p2\"]"]);

  state = resolveSummaryParticipationPut(state, started.version, [
    { file_id: "F0001", part_id: "p1", selected: true },
    { file_id: "F0001", part_id: "p2", selected: true },
  ]);
  assert.deepEqual([...state.confirmed], ["[\"F0001\",\"p1\"]", "[\"F0001\",\"p2\"]"]);
  assert.deepEqual([...state.displayed], ["[\"F0001\",\"p1\"]", "[\"F0001\",\"p2\"]"]);
  assert.equal(state.dirty, false);
});

test("returns the latest known server selection after the newest PUT fails", () => {
  let state = createSummaryParticipationState(new Set(["[\"F0001\",\"p1\"]"]));
  const first = beginSummaryParticipation(state, new Set(["[\"F0001\",\"p1\"]", "[\"F0001\",\"p2\"]"]));
  state = resolveSummaryParticipationPut(first.state, first.version, [
    { file_id: "F0001", part_id: "p1", selected: true },
  ]);
  const second = beginSummaryParticipation(state, new Set(["[\"F0001\",\"p2\"]"]));
  state = rejectSummaryParticipationPut(second.state, second.version);
  assert.deepEqual([...state.confirmed], ["[\"F0001\",\"p1\"]"]);
  assert.deepEqual([...state.displayed], ["[\"F0001\",\"p1\"]"]);
  assert.equal(state.dirty, false);
});

test("rejects a late project response even when its request id is otherwise current", () => {
  assert.equal(isCurrentProjectRequest(3, 3, "project-a", "project-a"), true);
  assert.equal(isCurrentProjectRequest(3, 4, "project-a", "project-a"), false);
  assert.equal(isCurrentProjectRequest(3, 3, "project-a", "project-b"), false);
});

test("allows refresh selection after deletion but blocks a switched project", () => {
  assert.equal(canAutoSelectProject("project-a", ""), true);
  assert.equal(canAutoSelectProject("project-a", "project-a"), true);
  assert.equal(canAutoSelectProject("project-a", "project-b"), false);
});
