import { useState } from "react";
import { translate, type Language } from "../i18n";
import type { RunDecision, TaskOptions } from "../types";

type ResultPolicy = "pending" | "reuse" | "force";

export function RunDialog({
  options,
  starting,
  onClose,
  onStart,
  language,
}: {
  options: TaskOptions;
  starting: boolean;
  onClose: () => void;
  onStart: (decision: RunDecision) => void;
  language: Language;
}) {
  const [runAction, setRunAction] = useState<"resume" | "decline" | null>(
    options.running_run ? "resume" : null,
  );
  const [resultPolicy, setResultPolicy] = useState<ResultPolicy | null>(
    options.mismatched_fingerprint_completed ? null : "pending",
  );
  const resuming = runAction === "resume";
  const ready = resuming || resultPolicy !== null;

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
            <h2 id="run-dialog-title">{translate("runDialog.title", language, { stage: translate(`stage.${options.stage}`, language) })}</h2>
            <p>{translate("runDialog.subtitle", language)}</p>
          </div>
        </div>
        <div className="run-counts">
          <span><strong>{options.selected}</strong>{translate("runDialog.total", language)}</span>
          <span><strong>{options.completed}</strong>{translate("runDialog.done", language)}</span>
          <span><strong>{options.pending}</strong>{translate("runDialog.pending", language)}</span>
          <span><strong>{options.failed}</strong>{translate("runDialog.failed", language)}</span>
        </div>

        {options.running_run && (
          <fieldset className="decision-group">
            <legend>{translate("runDialog.unfinishedRun", language)}</legend>
            <label className="radio-option decision-option">
              <input
                type="radio"
                checked={runAction === "resume"}
                onChange={() => setRunAction("resume")}
              />
              <span><strong>{translate("runDialog.resumeOriginal", language)}</strong><small>{translate("runDialog.resumeHint", language)}</small></span>
            </label>
            <label className="radio-option decision-option">
              <input
                type="radio"
                checked={runAction === "decline"}
                onChange={() => setRunAction("decline")}
              />
              <span><strong>{translate("runDialog.endOriginal", language)}</strong><small>{translate("runDialog.declinedHint", language, { runId: options.running_run.run_id })}</small></span>
            </label>
            <div className="run-details">
              <span>{translate("runDialog.originalScope", language)}<code>{JSON.stringify(options.running_run.scope)}</code></span>
              <span>{translate("runDialog.originalEndpoint", language)}{options.running_run.previous.model} · {options.running_run.previous.endpoint}</span>
              <span>{translate("runDialog.currentEndpoint", language)}{options.running_run.current.model} · {options.running_run.current.endpoint}</span>
            </div>
          </fieldset>
        )}

        {!resuming && (
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
          <button className="quiet-button" disabled={starting} onClick={onClose}>{translate("common.cancel", language)}</button>
          <button className="primary-button" disabled={!ready || starting} onClick={submit}>
            {starting ? translate("runDialog.starting", language) : translate("runDialog.run", language)}
          </button>
        </div>
      </div>
    </div>
  );
}
