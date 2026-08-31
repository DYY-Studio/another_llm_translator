import type { TaskState } from "./types";

const activeStatuses = new Set(["queued", "running", "cancelling"]);

export function isActiveTaskStatus(status: string): boolean {
  return activeStatuses.has(status);
}

export function canCancelTaskStatus(status: string): boolean {
  return status === "queued" || status === "running";
}

export function mergeTaskCollection(
  current: Record<string, TaskState>,
  incoming: TaskState[],
): Record<string, TaskState> {
  const merged = { ...current };
  for (const task of incoming) merged[task.project_id] = task;
  return merged;
}

export function reconcileTaskCollection(
  current: Record<string, TaskState>,
  active: TaskState[],
  terminal: Record<string, TaskState | null>,
): Record<string, TaskState> {
  const merged = mergeTaskCollection(current, active);
  const activeIds = new Set(active.map((task) => task.task_id));
  for (const task of Object.values(current)) {
    if (!isActiveTaskStatus(task.status) || activeIds.has(task.task_id)) continue;
    const finalState = terminal[task.task_id];
    if (finalState) {
      merged[finalState.project_id] = finalState;
    } else if (merged[task.project_id]?.task_id === task.task_id) {
      delete merged[task.project_id];
    }
  }
  return merged;
}
