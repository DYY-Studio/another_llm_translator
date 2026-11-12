import assert from "node:assert/strict";
import test from "node:test";

import {
  displayableFailureStage,
  displayableTaskStepStatus,
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

test("tracks queued, running, and revealed skipped steps across continuous progression", () => {
  const steps = [
    { stage: "terminology", status: "running" },
    { stage: "terminology_decision", status: "skipped" },
    { stage: "translation", status: "ready" },
  ];

  assert.deepEqual(
    steps.map((step) => displayableTaskStepStatus(step, steps, { current_stage: null, status: "queued" })),
    ["running", "queued", "queued"],
  );
  assert.deepEqual(
    steps.map((step) => displayableTaskStepStatus(step, steps, { current_stage: "terminology", status: "running" })),
    ["running", "queued", "queued"],
  );
  assert.equal(
    displayableTaskStepStatus(steps[1], steps, { current_stage: "terminology_decision", status: "running" }),
    "skipped",
  );
  assert.equal(
    displayableTaskStepStatus(steps[1], steps, { current_stage: "translation", status: "running" }),
    "skipped",
  );
  assert.equal(
    displayableTaskStepStatus(steps[1], steps, { current_stage: "terminology_decision", status: "completed" }),
    "skipped",
  );
});

test("marks future steps unexecuted after terminal failure or cancellation while preserving prior skips", () => {
  for (const status of ["cancelled", "failed"]) {
    const steps = [
      { stage: "terminology", status: "completed" },
      { stage: "terminology_decision", status: "skipped" },
      { stage: "content_summary", status },
      { stage: "translation", status: "skipped" },
      { stage: "proofreading", status: "queued" },
    ];
    assert.deepEqual(
      steps.map((step) => displayableTaskStepStatus(step, steps, { current_stage: "content_summary", status })),
      ["completed", "skipped", status, "not_executed", "not_executed"],
    );
  }
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

test("does not let an older terminal fetch overwrite or remove a newer project task", () => {
  const oldRunning = task("one", "T1", "running");
  const newerRunning = task("one", "T2", "running");
  const newerCompleted = task("one", "T2", "completed");
  const oldCompleted = task("one", "T1", "completed");

  const activeMerged = reconcileTaskCollection({ one: oldRunning }, [newerRunning], [oldRunning], { T1: oldCompleted });
  assert.equal(activeMerged.one.task_id, "T2");
  assert.equal(activeMerged.one.status, "running");

  const terminalMerged = reconcileTaskCollection({ one: newerCompleted }, [], [oldRunning], { T1: oldCompleted });
  assert.equal(terminalMerged.one.task_id, "T2");
  assert.equal(terminalMerged.one.status, "completed");

  const pendingMerged = reconcileTaskCollection({ one: newerRunning }, [], [oldRunning], { T1: null });
  assert.equal(pendingMerged.one.task_id, "T2");
  assert.equal(pendingMerged.one.status, "running");
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
