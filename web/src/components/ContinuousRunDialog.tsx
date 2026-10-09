import { Fragment, useEffect, useMemo, useState } from "react";
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
    stage === "terminology_decision" ? "stage.terminologyDecision" : stage === "content_summary" ? "stage.contentSummary" : `stage.${stage}`,
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
  const [preflight, setPreflight] = useState<{ request: string; options: ContinuousTaskOptions } | null>(null);
  const [displayCache, setDisplayCache] = useState<{
    project: string;
    steps: Record<string, Pick<ContinuousOptionStep, "preset" | "selected">>;
  }>({ project, steps: {} });
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState<unknown>(null);
  const [submitError, setSubmitError] = useState<unknown>(null);
  const [finalReview, setFinalReview] = useState(false);
  const [applyTerminologyDecision, setApplyTerminologyDecision] = useState(false);
  const [resultPolicy, setResultPolicy] = useState<ContinuousResultPolicy>("pending");
  const [includeDraftTranslation, setIncludeDraftTranslation] = useState(false);
  const [includeSummaries, setIncludeSummaries] = useState(false);
  const [aggregateFullSummaries, setAggregateFullSummaries] = useState(false);
  const [runActions, setRunActions] = useState<Record<string, RunActionSelection>>({});
  const [submitting, setSubmitting] = useState(false);

  const stages = useMemo(() => chainFor(startStage, endIndex), [endIndex, startStage]);
  const terminalDecision = stages.at(-1) === "terminology_decision";
  const hasDecision = stages.includes("terminology_decision");
  const decisionInMiddle = hasDecision && !terminalDecision;
  const params = new URLSearchParams();
  for (const stage of stages) params.append("stages", stage);
  params.set("language", language);
  params.set("final_review", String(hasDecision && (terminalDecision ? finalReview : true)));
  params.set("apply_terminology_decision", String(decisionInMiddle && applyTerminologyDecision));
  params.set("include_draft_translation", String(includeDraftTranslation));
  params.set("include_summaries", String(includeSummaries));
  params.set("aggregate_full_summaries", String(aggregateFullSummaries));
  const request = `/api/v1/projects/${project}/task-options/continuous?${params.toString()}`;
  const options = preflight?.request === request ? preflight.options : null;
  const terminologyOptions = options?.terminology_options;
  const jointOptionsLocked = submitting || runActions.terminology?.action === "resume";
  const displaySteps = displayCache.project === project ? displayCache.steps : {};
  useEffect(() => {
    let active = true;
    setLoading(true);
    setPreflight(null);
    setDisplayCache((current) => current.project === project ? current : { project, steps: {} });
    setLoadError(null);
    void api<ContinuousTaskOptions>(request).then((value) => {
      if (!active) return;
      setPreflight({ request, options: value });
      setDisplayCache((current) => ({
        project,
        steps: {
          ...current.steps,
          ...Object.fromEntries(value.steps.map(({ stage, preset, selected }) => [stage, { preset, selected }])),
        },
      }));
    }).catch((reason) => {
      if (active) setLoadError(reason);
    }).finally(() => {
      if (active) setLoading(false);
    });
    return () => { active = false; };
  }, [project, request]);

  useEffect(() => {
    setRunActions((current) => {
      const scoped = Object.fromEntries(
        Object.entries(current).filter(([stage]) => stages.includes(stage as LLMStage)
          || (stage === "content_summary" && aggregateFullSummaries)),
      );
      return options ? reconcileRunActions(scoped, options.steps) : scoped;
    });
  }, [options, stages, aggregateFullSummaries]);

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
    if (next !== "terminology") {
      setIncludeDraftTranslation(false);
      setIncludeSummaries(false);
      setAggregateFullSummaries(false);
    }
  }

  function changeEnd(stageIndex: number, checked: boolean) {
    const nextEndIndex = toggleEndIndex(endIndex, stageIndex, checked);
    setEndIndex(nextEndIndex);
    setRunActions((current) => Object.fromEntries(
      Object.entries(current).filter(([stage]) => {
        if (stage === "content_summary") return aggregateFullSummaries;
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

  const runningSteps = (options?.steps ?? []).filter((step) => step.running_run);
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
        includeDraftTranslation,
        includeSummaries,
        aggregateFullSummaries,
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
              const displayStep = stage === "translation" && includeDraftTranslation ? undefined : displaySteps[stage];
              const stageBlocking = blocking.filter((item) => item.stage === stage);
              return (
                <Fragment key={stage}>
                  <label className={`continuous-run-stage ${stageState}`}>
                    <input
                      type="checkbox"
                      checked={selected}
                      disabled={!canToggle || submitting}
                      onChange={(event) => changeEnd(stageIndex, event.target.checked)}
                    />
                    <span className="continuous-run-stage-main">
                      <strong>{stageLabel(stage, language)}</strong>
                      {stage === "terminology_decision" && startStage === "terminology" && (
                        <small>{translate(includeDraftTranslation ? "continuousRun.decisionAfterDraft" : "continuousRun.decisionInserted", language)}</small>
                      )}
                      {displayStep?.preset && <small>{displayStep.preset.id} · {displayStep.preset.model} · {displayStep.selected}</small>}
                      {stage === "translation" && includeDraftTranslation
                        ? <small>{translate("continuousRun.jointDraftSkipped", language)}</small>
                        : step?.status === "skipped" && <small>{translate("continuousRun.skipped", language, { reason: step.reason ?? "" })}</small>}
                      {stageBlocking.map((item) => (
                        <small className="error-text" key={item.code}>
                          {item.code === "mismatched_fingerprint"
                            ? translate("continuousRun.stageFingerprintWarning", language)
                            : item.message}
                        </small>
                      ))}
                    </span>
                  </label>
                  {stage === "terminology" && aggregateFullSummaries && <label className="continuous-run-stage selected">
                    <input type="checkbox" checked disabled />
                    <span className="continuous-run-stage-main">
                      <strong>{translate("continuousRun.aggregateSummaries", language)}</strong>
                      {displaySteps.content_summary?.preset && <small>{displaySteps.content_summary.preset.id} · {displaySteps.content_summary.preset.model} · {displaySteps.content_summary.selected}</small>}
                      {blocking.filter((item) => item.stage === "content_summary").map((item) => <small className="error-text" key={item.code}>{item.message}</small>)}
                    </span>
                  </label>}
                </Fragment>
              );
            })}
          </fieldset>

          {startStage === "terminology" && <div className="run-decision-info run-generation-options">
            {terminologyOptions && <small>{translate("runDialog.jointModelHint", language, { model: terminologyOptions.preset.model })}</small>}
            <label className="config-toggle">
              <span><input type="checkbox" checked={includeDraftTranslation} disabled={jointOptionsLocked} onChange={(event) => setIncludeDraftTranslation(event.target.checked)} />{translate("runDialog.draftToggle", language)}</span>
            </label>
            <label className="config-toggle">
              <span><input type="checkbox" checked={includeSummaries} disabled={jointOptionsLocked} onChange={(event) => { setIncludeSummaries(event.target.checked); if (!event.target.checked) setAggregateFullSummaries(false); }} />{translate("runDialog.summaryToggle", language)}</span>
              <small>{translate("runDialog.summaryHint", language)}</small>
            </label>
            {includeSummaries && <>
              {terminologyOptions && <small>{translate("runDialog.summaryScope", language, { count: terminologyOptions.summary_selected_boundaries ?? 0 })}</small>}
              <label className="config-toggle">
                <span><input type="checkbox" checked={aggregateFullSummaries} disabled={submitting} onChange={(event) => setAggregateFullSummaries(event.target.checked)} />{translate("continuousRun.aggregateSummaries", language)}</span>
                <small>{translate("continuousRun.aggregateSummariesHint", language)}</small>
              </label>
            </>}
          </div>}

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
                  <span>{translate(includeDraftTranslation ? "continuousRun.applyDecisionAfterDraft" : "continuousRun.applyDecision", language)}</span>
                </label>
              )}
            </section>
          )}

          {stages.includes("proofreading") && stages.at(-1) === "polishing" && (
            <p className="muted continuous-run-auto-apply">
              {translate("continuousRun.proofreadingAutoApply", language)}
            </p>
          )}

          <fieldset className="decision-group">
            <legend>{translate("continuousRun.resultPolicy", language)}</legend>
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
          </fieldset>

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
            {loadError != null && Object.keys(displaySteps).length > 0 && <p className="muted">{translate("continuousRun.previousPreflight", language)}</p>}
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
