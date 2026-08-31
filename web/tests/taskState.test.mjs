import assert from "node:assert/strict";
import test from "node:test";

import {
  canCancelTaskStatus,
  isActiveTaskStatus,
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

test("only queued and running tasks expose a cancellation action", () => {
  assert.equal(canCancelTaskStatus("queued"), true);
  assert.equal(canCancelTaskStatus("running"), true);
  assert.equal(canCancelTaskStatus("cancelling"), false);
  assert.equal(canCancelTaskStatus("completed"), false);
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
