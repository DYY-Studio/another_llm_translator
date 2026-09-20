import { useEffect, useMemo, useState } from "react";
import { api } from "../api";
import {
  CONTINUOUS_ORDER,
  CONTINUOUS_START_STAGES,
  chainFor,
  continuousPayload,
  isContinuousBlockingResolved,
  reconcileRunActions,
  resultPolicyAfterRunAction,
  runActionsPayload,
  toggleEndIndex,
  type ContinuousResultPolicy,
  type RunActionSelection,
  type ContinuousStartStage,
} from "../continuousRunState";
import { errorMessage, translate, type Language } from "../i18n";
import type {
  ContinuousRunDecision,
  ContinuousOptionStep,
  LLMStage,
  ContinuousTaskOptions,
} from "../types";

function stageLabel(stage: string, language: Language): string {
  return translate(
    stage === "terminology_decision" ? "stage.terminologyDecision" : `stage.${stage}`,
    language,
  );
}

function stepFor(options: ContinuousTaskOptions | null, stage: string): ContinuousOptionStep | undefined {
  return options?.steps?.find((step) => step.stage === stage);
}

export function ContinuousRunDialog({
  project,
  language,
  onClose,
  onStart,
}: {
  project: string;
  language: Language;
  onClose: () => void;
  onStart: (decision: ContinuousRunDecision) => Promise<void>;
}) {
  const [startStage, setStartStage] = useState<ContinuousStartStage>("translation");
  const [endIndex, setEndIndex] = useState(CONTINUOUS_ORDER.indexOf("translation"));
  const [options, setOptions] = useState<ContinuousTaskOptions | null>(null);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState<unknown>(null);
  const [submitError, setSubmitError] = useState<unknown>(null);
  const [finalReview, setFinalReview] = useState(false);
  const [applyTerminologyDecision, setApplyTerminologyDecision] = useState(false);
  const [resultPolicy, setResultPolicy] = useState<ContinuousResultPolicy>("pending");
  const [runActions, setRunActions] = useState<Record<string, RunActionSelection>>({});
  const [submitting, setSubmitting] = useState(false);

  const stages = useMemo(() => chainFor(startStage, endIndex), [endIndex, startStage]);
  const terminalDecision = stages.at(-1) === "terminology_decision";
  const hasDecision = stages.includes("terminology_decision");
  const decisionInMiddle = hasDecision && !terminalDecision;
  useEffect(() => {
    let active = true;
    setLoading(true);
    setOptions(null);
    setLoadError(null);
    const params = new URLSearchParams();
    for (const stage of stages) params.append("stages", stage);
    params.set("language", language);
    params.set("final_review", String(hasDecision && (terminalDecision ? finalReview : true)));
    params.set("apply_terminology_decision", String(decisionInMiddle && applyTerminologyDecision));
    void api<ContinuousTaskOptions>(
      `/api/v1/projects/${project}/task-options/continuous?${params.toString()}`,
    ).then((value) => {
      if (active) setOptions(value);
    }).catch((reason) => {
      if (active) setLoadError(reason);
    }).finally(() => {
      if (active) setLoading(false);
    });
    return () => { active = false; };
  }, [applyTerminologyDecision, decisionInMiddle, finalReview, hasDecision, language, project, stages, terminalDecision]);

  useEffect(() => {
    setRunActions((current) => {
      const scoped = Object.fromEntries(
        Object.entries(current).filter(([stage]) => stages.includes(stage as LLMStage)),
      );
      return options ? reconcileRunActions(scoped, options.steps) : scoped;
    });
  }, [options, stages]);

  useEffect(() => {
    if (options?.steps?.some((step) => step.mismatched_fingerprint_completed)) {
      setResultPolicy((current) => current ?? null);
    } else if (resultPolicy === null) {
      setResultPolicy("pending");
    }
  }, [options, resultPolicy]);

  function changeStartStage(next: ContinuousStartStage) {
    setStartStage(next);
    setEndIndex(CONTINUOUS_ORDER.indexOf(next));
    setRunActions({});
    setApplyTerminologyDecision(false);
    setFinalReview(false);
    setResultPolicy("pending");
  }

  function changeEnd(stageIndex: number, checked: boolean) {
    const nextEndIndex = toggleEndIndex(endIndex, stageIndex, checked);
    setEndIndex(nextEndIndex);
    setRunActions((current) => Object.fromEntries(
      Object.entries(current).filter(([stage]) => {
        const index = CONTINUOUS_ORDER.indexOf(stage as LLMStage);
        return index >= CONTINUOUS_ORDER.indexOf(startStage) && index <= nextEndIndex;
      }),
    ));
  }

  function chooseRunAction(stage: string, action: "resume" | "decline") {
    const run = stepFor(options, stage)?.running_run;
    if (!run) return;
    setRunActions((current) => ({ ...current, [stage]: { action, runId: run.run_id } }));
    setResultPolicy((current) => resultPolicyAfterRunAction(current, stage, action));
  }

  const runningSteps = stages
    .map((stage) => stepFor(options, stage))
    .filter((step): step is ContinuousOptionStep => Boolean(step?.running_run));
  const missingActions = runningSteps.filter((step) => !runActions[step.stage]);
  const hasResume = Object.values(runActions).some((selection) => selection.action === "resume");
  const decisionDeclineNeedsForce = runActions.terminology_decision?.action === "decline"
    && resultPolicy !== "force";
  const blocking = (options?.blocking ?? []).filter((item) => (
    (item.code !== "mismatched_fingerprint"
      || (resultPolicy !== "reuse" && resultPolicy !== "force"))
    && !isContinuousBlockingResolved(item.code, item.stage, runActions, resultPolicy)
  ));
  const localBlocking: Array<{ code: string; message: string; stage?: string }> = [
    ...missingActions.map((step) => ({
      code: "run_action_required",
      message: translate("continuousRun.runActionRequired", language, {
        stage: stageLabel(step.stage, language),
      }),
    })),
    ...(decisionDeclineNeedsForce ? [{
      code: "decision_decline_requires_force",
      message: translate("continuousRun.decisionDeclineForce", language),
    }] : []),
    ...(hasResume && (resultPolicy === "force" || resultPolicy === "reuse") ? [{
      code: "resume_policy_conflict",
      message: translate("continuousRun.resumePolicyConflict", language),
    }] : []),
  ];
  const allBlocking = [...blocking, ...localBlocking];
  const globalBlocking = allBlocking.filter((item) => !item.stage);
  const canStart = Boolean(
    options
      && !loading
      && !submitting
      && resultPolicy !== null
      && loadError == null
      && !allBlocking.length
      && (!decisionInMiddle || applyTerminologyDecision),
  );

  async function submit() {
    if (!canStart) return;
    setSubmitting(true);
    setSubmitError(null);
    try {
      await onStart(continuousPayload(stages, {
        finalReview,
        applyTerminologyDecision,
        force: resultPolicy === "force",
        reuseMixedFingerprints: resultPolicy === "reuse",
        runActions: runActionsPayload(runActions),
      }));
    } catch (reason) {
      setSubmitError(reason);
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <div className="modal-backdrop" onMouseDown={onClose}>
      <div
        className="modal continuous-run-dialog"
        role="dialog"
        aria-modal="true"
        aria-labelledby="continuous-run-dialog-title"
        onMouseDown={(event) => event.stopPropagation()}
      >
        <div className="continuous-run-dialog-heading page-heading">
          <div>
            <h2 id="continuous-run-dialog-title">{translate("continuousRun.title", language)}</h2>
            <p>{translate("continuousRun.subtitle", language)}</p>
          </div>
        </div>

        <div className="continuous-run-dialog-content">
          <label className="continuous-run-start">
            {translate("continuousRun.startStage", language)}
            <select
              value={startStage}
              disabled={submitting}
              onChange={(event) => changeStartStage(event.target.value as ContinuousStartStage)}
            >
              {CONTINUOUS_START_STAGES.map((stage) => (
                <option key={stage} value={stage}>{stageLabel(stage, language)}</option>
              ))}
            </select>
          </label>

          <fieldset className="continuous-run-stages">
            <legend>{translate("continuousRun.stages", language)}</legend>
            <p className="muted">{translate("continuousRun.stagesHint", language)}</p>
            {CONTINUOUS_ORDER.slice(CONTINUOUS_ORDER.indexOf(startStage)).map((stage) => {
              const stageIndex = CONTINUOUS_ORDER.indexOf(stage);
              const selected = stageIndex <= endIndex;
              const canToggle = stageIndex === CONTINUOUS_ORDER.indexOf(startStage)
                ? false
                : stageIndex <= endIndex + 1;
              const stageState = selected ? "selected" : canToggle ? "next" : "disabled";
              const step = stepFor(options, stage);
              const stageBlocking = blocking.filter((item) => item.stage === stage);
              return (
                <label className={`continuous-run-stage ${stageState}`} key={stage}>
                  <input
                    type="checkbox"
                    checked={selected}
                    disabled={!canToggle || submitting}
                    onChange={(event) => changeEnd(stageIndex, event.target.checked)}
                  />
                  <span className="continuous-run-stage-main">
                    <strong>{stageLabel(stage, language)}</strong>
                    {stage === "terminology_decision" && startStage === "terminology" && (
                      <small>{translate("continuousRun.decisionInserted", language)}</small>
                    )}
                    {step?.preset && <small>{step.preset.id} · {step.preset.model} · {step.selected}</small>}
                    {step?.status === "skipped" && <small>{translate("continuousRun.skipped", language, { reason: step.reason ?? "" })}</small>}
                    {stageBlocking.map((item) => (
                      <small className="error-text" key={item.code}>
                        {item.code === "mismatched_fingerprint"
                          ? translate("continuousRun.stageFingerprintWarning", language)
                          : item.message}
                      </small>
                    ))}
                  </span>
                </label>
              );
            })}
          </fieldset>

          {hasDecision && (
            <section className="continuous-run-section">
              <h3>{translate("continuousRun.decisionSettings", language)}</h3>
              {decisionInMiddle ? (
                <label className="check-row">
                  <input type="checkbox" checked disabled />
                  <span>{translate("continuousRun.middleFinalReview", language)}</span>
                </label>
              ) : (
                <label className="check-row">
                  <input type="checkbox" checked={finalReview} onChange={(event) => setFinalReview(event.target.checked)} disabled={submitting} />
                  <span>{translate("continuousRun.terminalFinalReview", language)}</span>
                </label>
              )}
              {decisionInMiddle && (
                <label className="check-row">
                  <input type="checkbox" checked={applyTerminologyDecision} onChange={(event) => setApplyTerminologyDecision(event.target.checked)} disabled={submitting} />
                  <span>{translate("continuousRun.applyDecision", language)}</span>
                </label>
              )}
            </section>
          )}

          {stages.includes("proofreading") && stages.at(-1) === "polishing" && (
            <p className="muted continuous-run-auto-apply">
              {translate("continuousRun.proofreadingAutoApply", language)}
            </p>
          )}

          <section className="continuous-run-section">
            <h3>{translate("continuousRun.resultPolicy", language)}</h3>
            <div className="continuous-run-policy">
              {(["pending", "reuse", "force"] as const).map((policy) => (
                <label className="radio-option decision-option" key={policy}>
                  <input
                    type="radio"
                    checked={resultPolicy === policy}
                    disabled={submitting || (hasResume && policy !== "pending")}
                    onChange={() => setResultPolicy(policy)}
                  />
                  <span><strong>{translate(`continuousRun.policy.${policy}`, language)}</strong><small>{translate(`continuousRun.policy.${policy}Hint`, language)}</small></span>
                </label>
              ))}
            </div>
          </section>

          {runningSteps.length > 0 && (
            <section className="continuous-run-section">
              <h3>{translate("continuousRun.unfinishedRuns", language)}</h3>
              {runningSteps.map((step) => {
                const run = step.running_run!;
                return (
                  <fieldset className="continuous-run-running" key={step.stage}>
                    <legend>{stageLabel(step.stage, language)} · {run.run_id}</legend>
                    <label className="radio-option decision-option">
                      <input type="radio" checked={runActions[step.stage]?.action === "resume"} disabled={run.resume_compatible === false || submitting} onChange={() => chooseRunAction(step.stage, "resume")} />
                      <span><strong>{translate("continuousRun.resume", language)}</strong><small>{run.resume_compatible === false ? run.resume_incompatibility_reason : translate("continuousRun.resumeHint", language)}</small></span>
                    </label>
                    <label className="radio-option decision-option">
                      <input type="radio" checked={runActions[step.stage]?.action === "decline"} disabled={submitting} onChange={() => chooseRunAction(step.stage, "decline")} />
                      <span><strong>{translate("continuousRun.decline", language)}</strong><small>{translate("continuousRun.declineHint", language)}</small></span>
                    </label>
                  </fieldset>
                );
              })}
            </section>
          )}

          <div className="continuous-run-feedback">
            {globalBlocking.length > 0 && (
              <div className="error-text continuous-run-blocking" role="alert">
                {globalBlocking.map((item) => (
                  <p key={`${item.code}-${item.stage ?? ""}`}>{item.message}</p>
                ))}
              </div>
            )}
            {loading && <p className="muted">{translate("common.loading", language)}</p>}
            {loadError != null && <p className="error-text" role="alert">{errorMessage(loadError, language)}</p>}
            {submitError != null && <p className="error-text" role="alert">{errorMessage(submitError, language)}</p>}
          </div>
        </div>

        <div className="modal-actions">
          <button className="quiet-button" onClick={onClose} disabled={submitting}>{translate("common.cancel", language)}</button>
          <button className="primary-button" onClick={() => { void submit(); }} disabled={!canStart}>{submitting ? translate("run.buttonStarting", language) : translate("continuousRun.start", language)}</button>
        </div>
      </div>
    </div>
  );
}
