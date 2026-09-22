import assert from "node:assert/strict";
import test from "node:test";

import {
  canCancelTaskStatus,
  displayableFailureStage,
  displayableTaskStepStatus,
  isActiveTaskStatus,
  isTerminalTaskStatus,
  mergeTaskCollection,
  reconcileTaskCollection,
} from "../src/taskState.ts";

function task(projectId, taskId, status) {
  return {
    task_id: taskId,
    project: projectId,
    project_id: projectId,
    stage: "translation",
    status,
    completed_segments: 0,
    failed_segments: 0,
    pending_segments: 1,
    total_segments: 1,
    failure_counts: {},
    usage: {
      input_tokens: 0,
      output_tokens: 0,
      total_tokens: 0,
      available: false,
      partial: false,
    },
  };
}

test("merges active tasks by project while retaining current-page terminal tasks", () => {
  const completed = task("one", "T1", "completed");
  const current = { one: completed, old: task("old", "T0", "failed") };
  const merged = mergeTaskCollection(current, [
    task("one", "T2", "running"),
    task("two", "T3", "queued"),
  ]);

  assert.equal(merged.one.task_id, "T2");
  assert.equal(merged.two.task_id, "T3");
  assert.equal(merged.old.task_id, "T0");
  assert.equal(Object.keys(merged).length, 3);
});

test("recognizes only queued, running, and cancelling tasks as active", () => {
  assert.equal(isActiveTaskStatus("queued"), true);
  assert.equal(isActiveTaskStatus("running"), true);
  assert.equal(isActiveTaskStatus("cancelling"), true);
  assert.equal(isActiveTaskStatus("completed"), false);
  assert.equal(isActiveTaskStatus("failed"), false);
  assert.equal(isActiveTaskStatus("cancelled"), false);
});

test("recognizes completed, failed, and cancelled tasks as terminal", () => {
  assert.equal(isTerminalTaskStatus("completed"), true);
  assert.equal(isTerminalTaskStatus("failed"), true);
  assert.equal(isTerminalTaskStatus("cancelled"), true);
  assert.equal(isTerminalTaskStatus("running"), false);
  assert.equal(isTerminalTaskStatus("unknown"), false);
});

test("only queued and running tasks expose a cancellation action", () => {
  assert.equal(canCancelTaskStatus("queued"), true);
  assert.equal(canCancelTaskStatus("running"), true);
  assert.equal(canCancelTaskStatus("cancelling"), false);
  assert.equal(canCancelTaskStatus("completed"), false);
});

test("maps continuous failures to a displayable current stage only", () => {
  assert.equal(
    displayableFailureStage({ stage: "continuous", current_stage: "proofreading" }),
    "proofreading",
  );
  assert.equal(
    displayableFailureStage({ stage: "continuous", current_stage: "terminology_decision" }),
    null,
  );
  assert.equal(displayableFailureStage({ stage: "content_summary" }), null);
});

test("keeps preflight-skipped continuous steps queued while terminology is running", () => {
  const steps = [
    { stage: "terminology", status: "running" },
    { stage: "terminology_decision", status: "skipped" },
    { stage: "translation", status: "queued" },
  ];

  assert.deepEqual(
    steps.map((step) => displayableTaskStepStatus(
      step,
      steps,
      { current_stage: "terminology", status: "running" },
    )),
    ["running", "queued", "queued"],
  );
});

test("keeps ready continuous steps queued after the current stage", () => {
  const steps = [
    { stage: "terminology", status: "running" },
    { stage: "terminology_decision", status: "ready" },
    { stage: "translation", status: "ready" },
  ];

  assert.equal(
    displayableTaskStepStatus(
      steps[2],
      steps,
      { current_stage: "terminology", status: "running" },
    ),
    "queued",
  );
});

test("keeps preflight steps queued before a task has a current stage", () => {
  const steps = [
    { stage: "terminology", status: "ready" },
    { stage: "terminology_decision", status: "skipped" },
  ];

  assert.deepEqual(
    steps.map((step) => displayableTaskStepStatus(
      step,
      steps,
      { current_stage: null, status: "queued" },
    )),
    ["queued", "queued"],
  );
});

test("reveals a skipped continuous step after execution advances past it", () => {
  const steps = [
    { stage: "terminology", status: "completed" },
    { stage: "terminology_decision", status: "skipped" },
    { stage: "translation", status: "running" },
  ];

  assert.equal(
    displayableTaskStepStatus(
      steps[1],
      steps,
      { current_stage: "translation", status: "running" },
    ),
    "skipped",
  );
});

test("keeps a currently executing step skipped when the service reports it", () => {
  const steps = [
    { stage: "terminology", status: "completed" },
    { stage: "terminology_decision", status: "skipped" },
    { stage: "translation", status: "queued" },
  ];

  assert.equal(
    displayableTaskStepStatus(
      steps[1],
      steps,
      { current_stage: "terminology_decision", status: "running" },
    ),
    "skipped",
  );
});

test("preserves terminal continuous step statuses", () => {
  const steps = [
    { stage: "terminology", status: "completed" },
    { stage: "terminology_decision", status: "skipped" },
  ];

  assert.equal(
    displayableTaskStepStatus(
      steps[1],
      steps,
      { current_stage: "terminology_decision", status: "completed" },
    ),
    "skipped",
  );
});

test("keeps a fetched terminal state once an observed task leaves the active list", () => {
  const running = task("one", "T1", "running");
  const completed = task("one", "T1", "completed");
  completed.completed_segments = 1;
  completed.pending_segments = 0;
  const merged = reconcileTaskCollection(
    { one: running },
    [],
    [running],
    { T1: completed },
  );
  assert.equal(merged.one.status, "completed");
  assert.equal(merged.one.task_id, "T1");
});

test("does not let an older terminal fetch replace a newer active project task", () => {
  const oldRunning = task("one", "T1", "running");
  const newerRunning = task("one", "T2", "running");
  const oldCompleted = task("one", "T1", "completed");
  const merged = reconcileTaskCollection(
    { one: oldRunning },
    [newerRunning],
    [oldRunning],
    { T1: oldCompleted },
  );
  assert.equal(merged.one.task_id, "T2");
  assert.equal(merged.one.status, "running");
});

test("does not let an older terminal fetch replace a newer terminal project task", () => {
  const oldRunning = task("one", "T1", "running");
  const newerCompleted = task("one", "T2", "completed");
  const oldCompleted = task("one", "T1", "completed");
  const merged = reconcileTaskCollection(
    { one: newerCompleted },
    [],
    [oldRunning],
    { T1: oldCompleted },
  );
  assert.equal(merged.one.task_id, "T2");
  assert.equal(merged.one.status, "completed");
});

test("does not remove a newer task while an older terminal fetch is pending", () => {
  const oldRunning = task("one", "T1", "running");
  const newerRunning = task("one", "T2", "running");
  const merged = reconcileTaskCollection(
    { one: newerRunning },
    [],
    [oldRunning],
    { T1: null },
  );
  assert.equal(merged.one.task_id, "T2");
  assert.equal(merged.one.status, "running");
});

test("removes only the task captured as missing when terminal fetch fails", () => {
  const missing = task("one", "T1", "running");
  const unrelated = task("two", "T2", "running");
  const merged = reconcileTaskCollection(
    { one: missing, two: unrelated },
    [],
    [missing],
    { T1: null },
  );
  assert.equal(merged.one, undefined);
  assert.equal(merged.two.task_id, "T2");
});
