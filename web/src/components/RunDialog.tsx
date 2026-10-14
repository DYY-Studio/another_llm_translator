import { useEffect, useState, type ReactNode } from "react";
import { translate, type Language } from "../i18n";
import type { RunDecision, RunningRun, TaskOptions } from "../types";

type ResultPolicy = "pending" | "reuse" | "force";

function overflowModeLabel(mode: "error" | "trim" | "compact", language: Language) {
  return translate(`terms.decisionOverflow${mode[0].toUpperCase()}${mode.slice(1)}`, language);
}

function stageLabelKey(stage: TaskOptions["stage"]): string {
  return stage === "content_summary" ? "stage.contentSummary" : `stage.${stage}`;
}

export interface RepetitionRunOptions {
  excluded_punctuation_groups: number; candidate_groups: number; selected: number; over_budget_groups: number;
  running_run: RunningRun | null; run_action: "resume" | "decline" | null; only_file: string | null; completed: number;
  screening_enabled: boolean; exclude_punctuation_only: boolean; preset: {id: string; model: string};
  estimated_requests: number; estimated_input_tokens: number; decision_model: string | null; model: string;
}

function RunDialogFrame({ title, subtitle, onClose, children }: {
  title: string; subtitle: string; onClose: () => void; children: ReactNode;
}) {
  return <div className="modal-backdrop" onMouseDown={onClose}>
    <div aria-labelledby="run-dialog-title" aria-modal="true" className="modal run-dialog" role="dialog" onMouseDown={event => event.stopPropagation()}>
      <div className="page-heading"><div><h2 id="run-dialog-title">{title}</h2><p>{subtitle}</p></div></div>
      {children}
    </div>
  </div>;
}

type StageRunDialogProps = {
  options: TaskOptions;
  onClose: () => void;
  onStart: (decision: RunDecision) => void;
  onOpenOverview?: () => void;
  onDraftTranslationChange?: (enabled: boolean) => Promise<void>;
  onSummariesChange?: (enabled: boolean) => Promise<void>;
  language: Language;
};

function UnfinishedRunChoice({ run, action, onChange, decisionMode = false, selected, language, busy = false }: {
  run: RunningRun; action: "resume" | "decline" | null; onChange: (action: "resume" | "decline") => void;
  decisionMode?: boolean; selected: number; language: Language; busy?: boolean;
}) {
  return (
          <fieldset className="decision-group">
            <legend>{translate("runDialog.unfinishedRun", language)}</legend>
            {run.resume_compatible === false && (
              <div className="warning-banner run-warning">
                {run.resume_incompatibility_reason}
              </div>
            )}
            <label className="radio-option decision-option">
              <input
                type="radio"
                checked={action === "resume"}
                disabled={busy || run.resume_compatible === false}
                onChange={() => onChange("resume")}
              />
              <span><strong>{translate(decisionMode ? "terms.decisionResume" : "runDialog.resumeOriginal", language)}</strong><small>{decisionMode ? translate("terms.decisionResumeHint", language, { completed: run.completed_steps ?? 0, total: run.total_steps ?? selected * 2 }) : translate("runDialog.resumeHint", language)}</small></span>
            </label>
            <label className="radio-option decision-option">
              <input
                type="radio"
                disabled={busy}
                checked={action === "decline"}
                onChange={() => onChange("decline")}
              />
              <span><strong>{translate(decisionMode ? "terms.decisionForce" : "runDialog.endOriginal", language)}</strong><small>{decisionMode ? translate("terms.decisionForceHint", language) : translate("runDialog.declinedHint", language, { runId: run.run_id })}</small></span>
            </label>
            <div className="run-details">
              <span>{translate("runDialog.originalScope", language)}<code>{JSON.stringify(run.scope)}</code></span>
              <span>{translate("runDialog.originalEndpoint", language)}{run.previous.model} · {run.previous.endpoint}</span>
              <span>{translate("runDialog.currentEndpoint", language)}{run.current.model} · {run.current.endpoint}</span>
            </div>
          </fieldset>
  );
}

export function RunDialog(props: StageRunDialogProps | {
  repetitionOptions: RepetitionRunOptions | null;
  excludePunctuationOnly: boolean; onExcludePunctuationOnly: (enabled: boolean) => void; replaceDraft: boolean; busy: boolean; error: string;
  onRunAction: (action: "resume" | "decline") => void;
  screeningEnabled: boolean; onScreeningEnabled: (enabled: boolean) => void;
  language: Language; onClose: () => void; onStart: () => void;
}) {
  if (!("repetitionOptions" in props)) return <StageRunDialog {...props} />;
  const { repetitionOptions: options, language, replaceDraft, busy, error, onClose, onStart, excludePunctuationOnly, onExcludePunctuationOnly, screeningEnabled, onScreeningEnabled, onRunAction } = props;
  const t = (key: string, params: Record<string, string | number> = {}) => translate(`repetition.${key}`, language, params);
  const resuming = options?.run_action === "resume";
  const generationOptions = <>
    <label className="config-toggle"><span><input type="checkbox" checked={screeningEnabled} disabled={busy || resuming} onChange={e => onScreeningEnabled(e.target.checked)} />{t("screeningEnabled")}</span></label>
    <label className="config-toggle"><span><input type="checkbox" checked={excludePunctuationOnly} disabled={busy || resuming} onChange={e => onExcludePunctuationOnly(e.target.checked)} />{t("excludePunctuationOnly")}</span><small>{t("punctuationFilterHint")}</small></label>
  </>;
  return <RunDialogFrame title={translate("runDialog.title", language, {stage: t("title")})} subtitle={translate("runDialog.subtitle", language)} onClose={busy ? () => {} : onClose}>
    {!options && generationOptions}
    {options ? <><div className="run-preset"><span>{translate("runDialog.currentPreset", language)}</span><strong><code>{options.preset.id}</code></strong><small>{options.preset.model}</small></div>
    <div className="run-counts">
      <span><strong>{options.selected}</strong>{translate("runDialog.total", language)}</span>
      <span><strong>{options.completed}</strong>{translate("runDialog.done", language)}</span>
      <span><strong>{options.selected - options.completed}</strong>{translate("runDialog.pending", language)}</span>
    </div>
    {generationOptions}
    {options.running_run && <UnfinishedRunChoice run={options.running_run} action={options.run_action} onChange={onRunAction} selected={options.selected} language={language} busy={busy} />}
    {resuming && options.running_run && <p className="muted">{t("resumeSteps", {completed: options.running_run.completed_steps ?? 0, total: options.running_run.total_steps ?? 0})}</p>}
    <div className="run-decision-info"><span>{options.decision_model ? t("models", { decision: options.decision_model, model: options.model }) : t("comparisonModel", {model: options.model})}</span>
      <span>{t("estimate", { groups: options.candidate_groups, count: options.selected, requests: options.estimated_requests, tokens: options.estimated_input_tokens })}</span>
      <span>{t("punctuationExcluded", { count: options.excluded_punctuation_groups })}</span>
      {!!options.over_budget_groups && <span>{t("overBudget", { count: options.over_budget_groups })}</span>}
    </div>
    </> : busy ? <p>{translate("common.loading", language)}</p> : null}
    {error && <p className="error-text" role="alert">{error}</p>}
    {replaceDraft && <div className="warning-banner run-warning">{t("confirmReplace")}</div>}
    <div className="modal-actions">
      <button className="quiet-button" disabled={busy} onClick={onClose}>{translate("common.cancel", language)}</button>
      <button className="primary-button" disabled={busy || !options?.candidate_groups || (resuming && options.running_run?.resume_compatible === false)} onClick={onStart}>{translate("runDialog.run", language)}</button>
    </div>
  </RunDialogFrame>;
}

function StageRunDialog({
  options,
  onClose,
  onStart,
  onOpenOverview,
  onDraftTranslationChange,
  onSummariesChange,
  language,
}: StageRunDialogProps) {
  const [knowledgeConfirmed, setKnowledgeConfirmed] = useState(false);
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
  const replacementNeeded = !resuming && resultPolicy === "force" && Boolean(options.knowledge_replacement_impacts?.length);
  const ready = (replacementNeeded && !knowledgeConfirmed) ? false : summaryConfigurationBlocked || draftBlocked || draftLoading ? false : decisionMode
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
      confirm_knowledge_replacement: replacementNeeded && knowledgeConfirmed,
      reuse_mixed_fingerprints: !resuming && resultPolicy === "reuse",
      run_action: options.running_run ? runAction : null,
      final_review: decisionMode && finalReview,
      ...(options.stage === "terminology" ? { include_draft_translation: draftEnabled, include_summaries: hybridSummary } : {}),
    });
  }

  return (
    <RunDialogFrame
      title={translate(decisionMode ? "runDialog.decisionTitle" : "runDialog.title", language, { stage: translate(stageLabelKey(options.stage), language) })}
      subtitle={decisionMode ? translate("terms.decisionHint", language) : translate("runDialog.subtitle", language)}
      onClose={onClose}>
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
          {replacementNeeded && <div className="warning-banner run-warning">
            <strong>{translate("knowledge.replacement", language, { count: options.knowledge_replacement_impacts?.length ?? 0 })}</strong>
            <details><summary>{translate("knowledge.fragments", language)}</summary>{options.knowledge_replacement_impacts?.map((fragment) => <p key={fragment.fragment_id}>{fragment.owner}: {fragment.text}</p>)}</details>
            <label className="checkbox-label"><input type="checkbox" checked={knowledgeConfirmed} onChange={(event) => setKnowledgeConfirmed(event.target.checked)} />{translate("knowledge.confirmReplacement", language)}</label>
          </div>}
          {hybridSummary && resultPolicy === "force" && !resuming && <div className="warning-banner run-warning">{translate("runDialog.summaryForce", language, { count: options.summary_selected_boundaries ?? 0 })}</div>}
          {draftEnabled && resultPolicy === "force" && !resuming && <div className="warning-banner run-warning">{translate("runDialog.draftForce", language, { total: options.selected, count: options.draft_progress?.translation?.completed ?? 0 })}</div>}
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

        {options.running_run && <UnfinishedRunChoice run={options.running_run} action={runAction} onChange={chooseRunAction}
          decisionMode={decisionMode} selected={options.selected} language={language} />}

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
    </RunDialogFrame>
  );
}
