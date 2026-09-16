import { useState } from "react";
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
  language,
}: {
  options: TaskOptions;
  onClose: () => void;
  onStart: (decision: RunDecision) => void;
  onOpenOverview?: () => void;
  language: Language;
}) {
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
  const decisionMode = options.stage === "terminology_decision";
  const hybridSummary = options.stage === "terminology" && options.summary_selected_boundaries !== undefined;
  const summaryPromptBlocked = Boolean(options.summary_prompt_preflight && !options.summary_prompt_preflight.ok);
  const summaryConfigurationBlocked = hybridSummary && (
    !options.summary_selected_boundaries
    || summaryPromptBlocked
    || Boolean(options.summary_only_work)
  );
  const resuming = runAction === "resume";
  const ready = summaryConfigurationBlocked ? false : decisionMode
    ? !options.running_run || resuming || resultPolicy === "force"
    : resuming || resultPolicy !== null;

  function chooseRunAction(action: "resume" | "decline") {
    setRunAction(action);
    if (decisionMode && action === "decline") setResultPolicy("force");
  }

  function submit() {
    if (!ready) return;
    onStart({
      force: !resuming && resultPolicy === "force",
      reuse_mixed_fingerprints: !resuming && resultPolicy === "reuse",
      run_action: options.running_run ? runAction : null,
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
            <div><strong>{translate("runOptions.summaryTitle", language)}</strong>{onOpenOverview && <button className="link-button" onClick={onOpenOverview}>{translate("runOptions.openOverview", language)}</button>}</div>
            {options.document_adapter_run_options?.map((adapter) => (
              <p key={adapter.adapter_id}><code>{adapter.adapter_id}</code> · {translate("runOptions.fileCount", language, { count: adapter.file_count })}: {adapter.options.map((option) => `${option.label}: ${option.value ?? translate("runOptions.multiple", language)}`).join(" · ")}</p>
            ))}
          </section>
        )}
        {decisionMode && <div className="run-decision-info">
          <span>{translate("terms.decisionScope", language, { selected: options.selected, protected: options.protected ?? 0 })}</span>
          <span>{translate("terms.decisionEstimate", language, { requests: options.estimated_requests ?? 0, tokens: options.estimated_input_tokens ?? 0 })}</span>
          {options.overflow_policy && <span>{translate("terms.decisionOverflowPolicy", language, {
            soft: translate(options.overflow_policy.allow_soft_target_overflow ? "terms.decisionSoftAllowed" : "terms.decisionSoftBlocked", language),
            mode: overflowModeLabel(options.overflow_policy.anchor_overflow_mode, language),
          })}</span>}
        </div>}

        {hybridSummary && <div className="run-decision-info">
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
