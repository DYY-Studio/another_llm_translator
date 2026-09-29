import type { LLMStage, TaskState, TaskStep } from "./types";

const activeStatuses = new Set(["queued", "running", "cancelling"]);
const terminalStatuses = new Set(["completed", "failed", "cancelled"]);
const failureStages = new Set<LLMStage>([
  "terminology",
  "translation",
  "proofreading",
  "polishing",
]);

export function isActiveTaskStatus(status: string): boolean {
  return activeStatuses.has(status);
}

export function isTerminalTaskStatus(status: string): boolean {
  return terminalStatuses.has(status);
}

export function canCancelTaskStatus(status: string): boolean {
  return status === "queued" || status === "running";
}

export function displayableFailureStage(
  task: Pick<TaskState, "stage" | "current_stage">,
): LLMStage | null {
  const candidate = task.stage === "continuous"
    ? task.current_stage
    : task.stage;
  return candidate && failureStages.has(candidate as LLMStage)
    ? candidate as LLMStage
    : null;
}

export function displayableTaskStepStatus(
  step: Pick<TaskStep, "stage" | "status">,
  steps: Array<Pick<TaskStep, "stage" | "status">>,
  task: Pick<TaskState, "current_stage" | "status">,
): string {
  if (isTerminalTaskStatus(task.status)) {
    return step.status;
  }
  if (step.status === "ready") return "queued";
  if (step.status !== "skipped") return step.status;
  const currentIndex = task.current_stage
    ? steps.findIndex((current) => current.stage === task.current_stage)
    : -1;
  const stepIndex = steps.findIndex((current) => current.stage === step.stage);
  return currentIndex >= 0 && stepIndex >= 0 && stepIndex <= currentIndex
    ? "skipped"
    : "queued";
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
  missing: TaskState[],
  terminal: Record<string, TaskState | null>,
): Record<string, TaskState> {
  const merged = mergeTaskCollection(current, active);
  const activeIds = new Set(active.map((task) => task.task_id));
  for (const task of missing) {
    if (!isActiveTaskStatus(task.status) || activeIds.has(task.task_id)) continue;
    const finalState = terminal[task.task_id];
    if (finalState) {
      const currentProject = merged[task.project_id];
      if (
        !currentProject
        || currentProject.task_id === task.task_id
      ) {
        merged[finalState.project_id] = finalState;
      }
    } else if (merged[task.project_id]?.task_id === task.task_id) {
      delete merged[task.project_id];
    }
  }
  return merged;
}
