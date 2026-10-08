import { useEffect, useState } from "react";
import { translate, type Language } from "../i18n";
import type { RunDecision, TaskOptions } from "../types";

type ResultPolicy = "pending" | "reuse" | "force";

function overflowModeLabel(mode: "error" | "trim" | "compact", language: Language) {
  return translate(`terms.decisionOverflow${mode[0].toUpperCase()}${mode.slice(1)}`, language);
}

function stageLabelKey(stage: TaskOptions["stage"]): string {
  return stage === "content_summary" ? "stage.contentSummary" : `stage.${stage}`;
}

export function RunDialog({
  options,
  onClose,
  onStart,
  onOpenOverview,
  onDraftTranslationChange,
  onSummariesChange,
  language,
}: {
  options: TaskOptions;
  onClose: () => void;
  onStart: (decision: RunDecision) => void;
  onOpenOverview?: () => void;
  onDraftTranslationChange?: (enabled: boolean) => Promise<void>;
  onSummariesChange?: (enabled: boolean) => Promise<void>;
  language: Language;
}) {
  const [draftLoading, setDraftLoading] = useState(false);
  const [draftError, setDraftError] = useState("");
  const [runAction, setRunAction] = useState<"resume" | "decline" | null>(
    options.running_run
      ? options.running_run.resume_compatible === false ? "decline" : "resume"
      : null,
  );
  const [resultPolicy, setResultPolicy] = useState<ResultPolicy | null>(
    options.running_run?.resume_compatible === false
      ? "force"
      : options.mismatched_fingerprint_completed ? null : "pending",
  );
  useEffect(() => {
    if (options.stage === "terminology" && onDraftTranslationChange) {
      setResultPolicy(options.running_run?.resume_compatible === false
        ? "force"
        : options.mismatched_fingerprint_completed ? null : "pending");
    }
  }, [options.include_draft_translation, options.include_summaries, options.mismatched_fingerprint_completed]);
  const decisionMode = options.stage === "terminology_decision";
  const [finalReview, setFinalReview] = useState(
    options.running_run?.final_review ?? options.final_review ?? false,
  );
  const hybridSummary = options.stage === "terminology" && Boolean(options.include_summaries);
  const summaryPromptBlocked = Boolean(options.summary_prompt_preflight && !options.summary_prompt_preflight.ok);
  const summaryConfigurationBlocked = hybridSummary && (
    !options.summary_selected_boundaries
    || summaryPromptBlocked
    || Boolean(options.summary_only_work)
  );
  const resuming = runAction === "resume";
  const finalReviewLocked = decisionMode && Boolean(options.running_run && resuming);
  const draftEnabled = Boolean(options.include_draft_translation);
  const draftBlocked = draftEnabled && options.draft_prompt_preflight?.ok === false;
  const ready = summaryConfigurationBlocked || draftBlocked || draftLoading ? false : decisionMode
    ? !options.running_run || resuming || resultPolicy === "force"
    : resuming || resultPolicy !== null;

  async function changeDraft(enabled: boolean, summaries = false) {
    const change = summaries ? onSummariesChange : onDraftTranslationChange;
    if (!change) return;
    setDraftLoading(true);
    setDraftError("");
    try {
      await change(enabled);
    } catch (error) {
      setDraftError(error instanceof Error ? error.message : String(error));
    } finally {
      setDraftLoading(false);
    }
  }

  function chooseRunAction(action: "resume" | "decline") {
    setRunAction(action);
    if (decisionMode && action === "decline") {
      setResultPolicy("force");
      setFinalReview(options.final_review ?? false);
    } else if (decisionMode && action === "resume" && options.running_run) {
      setFinalReview(options.running_run.final_review ?? false);
    }
  }

  function submit() {
    if (!ready) return;
    onStart({
      force: !resuming && resultPolicy === "force",
      reuse_mixed_fingerprints: !resuming && resultPolicy === "reuse",
      run_action: options.running_run ? runAction : null,
      final_review: decisionMode && finalReview,
      ...(options.stage === "terminology" ? { include_draft_translation: draftEnabled, include_summaries: hybridSummary } : {}),
    });
  }

  return (
    <div className="modal-backdrop" onMouseDown={onClose}>
      <div
        aria-labelledby="run-dialog-title"
        aria-modal="true"
        className="modal run-dialog"
        role="dialog"
        onMouseDown={(event) => event.stopPropagation()}
      >
        <div className="page-heading">
          <div>
            <h2 id="run-dialog-title">{translate(decisionMode ? "runDialog.decisionTitle" : "runDialog.title", language, { stage: translate(stageLabelKey(options.stage), language) })}</h2>
            <p>{decisionMode ? translate("terms.decisionHint", language) : translate("runDialog.subtitle", language)}</p>
          </div>
        </div>
        <div className="run-preset" aria-label={translate("runDialog.currentPreset", language)}>
          <span>{translate("runDialog.currentPreset", language)}</span>
          <strong><code>{options.preset.id}</code></strong>
          <small>{options.preset.model}</small>
        </div>
        <div className="run-counts">
          <span><strong>{options.selected}</strong>{translate("runDialog.total", language)}</span>
          <span><strong>{options.completed}</strong>{translate("runDialog.done", language)}</span>
          <span><strong>{options.pending}</strong>{translate("runDialog.pending", language)}</span>
          <span><strong>{options.failed}</strong>{translate("runDialog.failed", language)}</span>
        </div>
        {(options.document_adapter_run_options?.length ?? 0) > 0 && (
          <section className="run-adapter-options" aria-label={translate("runOptions.summaryTitle", language)}>
            <div className="run-adapter-options-heading"><strong>{translate("runOptions.summaryTitle", language)}</strong>{onOpenOverview && <button className="link-button" onClick={onOpenOverview}>{translate("runOptions.openOverview", language)}</button>}</div>
            <div className="run-adapter-summary-list">
              {options.document_adapter_run_options?.map((adapter) => (
                <article className="run-adapter-summary" key={adapter.adapter_id}>
                  <header><code>{adapter.adapter_id}</code><span>{translate("runOptions.fileCount", language, { count: adapter.file_count })}</span></header>
                  <dl>{adapter.options.map((option) => <div key={option.option_id}><dt>{option.label}</dt><dd>{option.value ?? translate("runOptions.multiple", language)}</dd></div>)}</dl>
                </article>
              ))}
            </div>
          </section>
        )}
        {decisionMode && <div className="run-decision-info">
          <span>{translate("terms.decisionScope", language, { selected: options.selected, protected: options.protected ?? 0 })}</span>
          <span>{translate("terms.decisionEstimate", language, { requests: options.estimated_requests ?? 0, tokens: options.estimated_input_tokens ?? 0 })}</span>
          {options.overflow_policy && <span>{translate("terms.decisionOverflowPolicy", language, {
            soft: translate(options.overflow_policy.allow_soft_target_overflow ? "terms.decisionSoftAllowed" : "terms.decisionSoftBlocked", language),
            mode: overflowModeLabel(options.overflow_policy.anchor_overflow_mode, language),
          })}</span>}
          <label className="config-toggle">
            <span>
              <input
                type="checkbox"
                checked={finalReview}
                disabled={finalReviewLocked}
                onChange={(event) => setFinalReview(event.target.checked)}
              />
              {translate("terms.decisionFinalReview", language)}
            </span>
            <small>
              {finalReviewLocked
                ? translate("terms.decisionFinalReviewLocked", language)
                : translate("terms.decisionFinalReviewHint", language)}
            </small>
            {finalReview && (
              <small>{translate("terms.decisionFinalReviewEstimateHint", language)}</small>
            )}
          </label>
        </div>}

        {options.stage === "terminology" && onDraftTranslationChange && <div className="run-decision-info run-generation-options">
          <small>{translate("runDialog.jointModelHint", language, { model: options.preset.model })}</small>
          <label className="config-toggle">
            <span><input type="checkbox" checked={draftEnabled} disabled={draftLoading || Boolean(options.running_run && resuming)} onChange={(event) => { void changeDraft(event.target.checked); }} />{translate("runDialog.draftToggle", language)}</span>
          </label>
          {onSummariesChange && <label className="config-toggle">
            <span><input type="checkbox" checked={hybridSummary} disabled={draftLoading || Boolean(options.running_run && resuming)} onChange={(event) => { void changeDraft(event.target.checked, true); }} />{translate("runDialog.summaryToggle", language)}</span>
            <small>{translate("runDialog.summaryHint", language)}</small>
          </label>}
          {draftLoading && <small>{translate("runDialog.draftLoading", language)}</small>}
          {draftError && <p className="error-text">{draftError}</p>}
          {options.draft_progress && Object.entries(options.draft_progress).map(([stage, value]) => <span key={stage}>{translate(stage === "content_summary" ? "stage.contentSummary" : `stage.${stage}`, language)} · {translate("runDialog.draftCounts", language, { completed: value.completed, pending: value.total - value.completed, total: value.total })}</span>)}
          {draftBlocked && <p className="error-text">{translate("runDialog.draftMissingPrompt", language)} {options.draft_prompt_preflight?.missing.join(", ")}</p>}
          {hybridSummary && resultPolicy === "force" && !resuming && <div className="warning-banner run-warning">{translate("runDialog.summaryForce", language, { count: options.summary_selected_boundaries ?? 0 })}</div>}
          {draftEnabled && resultPolicy === "force" && !resuming && <div className="warning-banner run-warning">{translate("runDialog.draftForce", language, { total: options.selected, count: options.draft_progress?.translation.completed ?? 0 })}</div>}
        </div>}

        {hybridSummary && <div className="run-decision-info">
          {options.summary_progress && <span>{translate("stage.contentSummary", language)} · {translate("runDialog.draftCounts", language, { completed: options.summary_progress.completed, pending: options.summary_progress.total - options.summary_progress.completed, total: options.summary_progress.total })}</span>}
          <span>{translate("runDialog.summaryScope", language, { count: options.summary_selected_boundaries ?? 0 })}</span>
          {options.summary_only_work && <div className="warning-banner run-warning">{translate("terms.summaryPreflightConflict", language)}</div>}
          {summaryPromptBlocked && <div className="error-text summary-message">
            <p>{translate("terms.summaryPromptMissing", language, { language: options.summary_prompt_preflight?.language ?? language })}</p>
            <ul>{(options.summary_prompt_preflight?.missing ?? []).map((name) => <li key={name}><code>{name}</code></li>)}</ul>
          </div>}
        </div>}

        {options.running_run && (
          <fieldset className="decision-group">
            <legend>{translate("runDialog.unfinishedRun", language)}</legend>
            {options.running_run.resume_compatible === false && (
              <div className="warning-banner run-warning">
                {translate(options.stage === "content_summary" ? "runDialog.resumeUnavailable" : "terms.decisionResumeIncompatible", language)} {options.running_run.resume_incompatibility_reason}
              </div>
            )}
            <label className="radio-option decision-option">
              <input
                type="radio"
                checked={runAction === "resume"}
                disabled={options.running_run.resume_compatible === false}
                onChange={() => chooseRunAction("resume")}
              />
              <span><strong>{translate(decisionMode ? "terms.decisionResume" : "runDialog.resumeOriginal", language)}</strong><small>{decisionMode ? translate("terms.decisionResumeHint", language, { completed: options.running_run.completed_steps ?? 0, total: options.running_run.total_steps ?? options.selected * 2 }) : translate("runDialog.resumeHint", language)}</small></span>
            </label>
            <label className="radio-option decision-option">
              <input
                type="radio"
                checked={runAction === "decline"}
                onChange={() => chooseRunAction("decline")}
              />
              <span><strong>{translate(decisionMode ? "terms.decisionForce" : "runDialog.endOriginal", language)}</strong><small>{decisionMode ? translate("terms.decisionForceHint", language) : translate("runDialog.declinedHint", language, { runId: options.running_run.run_id })}</small></span>
            </label>
            <div className="run-details">
              <span>{translate("runDialog.originalScope", language)}<code>{JSON.stringify(options.running_run.scope)}</code></span>
              <span>{translate("runDialog.originalEndpoint", language)}{options.running_run.previous.model} · {options.running_run.previous.endpoint}</span>
              <span>{translate("runDialog.currentEndpoint", language)}{options.running_run.current.model} · {options.running_run.current.endpoint}</span>
            </div>
          </fieldset>
        )}

        {!resuming && !decisionMode && (
          <fieldset className="decision-group">
            <legend>{translate("runDialog.existingResults", language)}</legend>
            {options.mismatched_fingerprint_completed ? (
              <>
                <div className="warning-banner run-warning">
                  {translate("runDialog.mismatchedWarning", language, { count: options.mismatched_fingerprint_completed })}
                </div>
                <label className="radio-option decision-option">
                  <input
                    type="radio"
                    checked={resultPolicy === "reuse"}
                    onChange={() => setResultPolicy("reuse")}
                  />
                  <span><strong>{translate("runDialog.reuseResults", language)}</strong><small>{translate("runDialog.reuseHint", language)}</small></span>
                </label>
              </>
            ) : (
              <label className="radio-option decision-option">
                <input
                  type="radio"
                  checked={resultPolicy === "pending"}
                  onChange={() => setResultPolicy("pending")}
                />
                <span><strong>{translate("runDialog.processUnfinished", language)}</strong><small>{translate("runDialog.processHint", language)}</small></span>
              </label>
            )}
            <label className="radio-option decision-option">
              <input
                type="radio"
                checked={resultPolicy === "force"}
                onChange={() => setResultPolicy("force")}
              />
              <span>
                <strong>{translate("runDialog.redoEverything", language)}</strong>
                <small>
                  {options.stage === "terminology"
                    ? translate("runDialog.redoTerminologyHint", language)
                    : translate("runDialog.redoStageHint", language, { count: options.selected })}
                </small>
              </span>
            </label>
          </fieldset>
        )}
        <div className="modal-actions">
          <button className="quiet-button" onClick={onClose}>{translate("common.cancel", language)}</button>
          <button className="primary-button" disabled={!ready} onClick={submit}>
            {translate("runDialog.run", language)}
          </button>
        </div>
      </div>
    </div>
  );
}
