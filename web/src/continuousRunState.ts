import type { LLMStage } from "./types";

export const CONTINUOUS_ORDER = [
  "terminology",
  "terminology_decision",
  "translation",
  "proofreading",
  "polishing",
] as const satisfies readonly LLMStage[];

export const CONTINUOUS_START_STAGES = [
  "terminology",
  "translation",
  "proofreading",
] as const satisfies readonly LLMStage[];

export type ContinuousStartStage = (typeof CONTINUOUS_START_STAGES)[number];

export type ContinuousRunAction = "resume" | "decline";
export type ContinuousResultPolicy = "pending" | "reuse" | "force" | null;

export interface RunActionSelection {
  action: ContinuousRunAction;
  runId: string;
}

interface RunningStepForAction {
  stage: string;
  running_run?: {
    run_id: string;
    resume_compatible?: boolean | null;
  } | null;
}

export function chainFor(
  start: ContinuousStartStage,
  endIndex: number,
): LLMStage[] {
  const startIndex = CONTINUOUS_ORDER.indexOf(start);
  const boundedEnd = Math.max(startIndex, Math.min(endIndex, CONTINUOUS_ORDER.length - 1));
  return [...CONTINUOUS_ORDER.slice(startIndex, boundedEnd + 1)];
}

export function toggleEndIndex(
  currentEndIndex: number,
  stageIndex: number,
  nextChecked: boolean,
): number {
  if (nextChecked) return Math.max(currentEndIndex, stageIndex);
  return stageIndex <= currentEndIndex ? stageIndex - 1 : currentEndIndex;
}

export function reconcileRunActions(
  selections: Record<string, RunActionSelection>,
  steps: readonly RunningStepForAction[],
): Record<string, RunActionSelection> {
  const runningByStage = new Map(
    steps
      .filter((step) => step.running_run)
      .map((step) => [step.stage, step.running_run!] as const),
  );
  return Object.fromEntries(
    Object.entries(selections).filter(([stage, selection]) => {
      const run = runningByStage.get(stage);
      return Boolean(
        run
        && run.run_id === selection.runId
        && !(selection.action === "resume" && run.resume_compatible === false),
      );
    }),
  );
}

export function runActionsPayload(
  selections: Record<string, RunActionSelection>,
): Record<string, ContinuousRunAction> {
  return Object.fromEntries(
    Object.entries(selections).map(([stage, selection]) => [stage, selection.action]),
  );
}

export function resultPolicyAfterRunAction(
  current: ContinuousResultPolicy,
  stage: string,
  action: ContinuousRunAction,
): ContinuousResultPolicy {
  if (action === "decline" && stage === "terminology_decision") return "force";
  if (action === "resume" && (current === "force" || current === "reuse")) return "pending";
  return current;
}

export function isContinuousBlockingResolved(
  code: string,
  stage: string | undefined,
  selections: Record<string, RunActionSelection>,
  policy: ContinuousResultPolicy,
): boolean {
  if (code === "run_action_required") return Boolean(stage && selections[stage]);
  if (code === "decision_decline_requires_force") {
    return selections.terminology_decision?.action === "decline" && policy === "force";
  }
  if (code === "resume_policy_conflict") {
    return Object.values(selections).some((selection) => selection.action === "resume")
      && policy === "pending";
  }
  return false;
}

export interface ContinuousPayloadOptions {
  finalReview: boolean;
  applyTerminologyDecision: boolean;
  force: boolean;
  reuseMixedFingerprints: boolean;
  runActions: Record<string, ContinuousRunAction>;
}

export function continuousPayload(
  stages: LLMStage[],
  options: ContinuousPayloadOptions,
) {
  const hasDecision = stages.includes("terminology_decision");
  const terminalDecision = stages.at(-1) === "terminology_decision";
  return {
    stage: "continuous" as const,
    stages,
    final_review: hasDecision && (terminalDecision ? options.finalReview : true),
    apply_terminology_decision: hasDecision && !terminalDecision
      ? options.applyTerminologyDecision
      : false,
    force: options.force,
    reuse_mixed_fingerprints: options.reuseMixedFingerprints,
    run_actions: options.runActions,
  };
}
