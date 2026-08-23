import { useEffect, useMemo, useRef, useState } from "react";
import { api } from "../api";
import { translate, type Language } from "../i18n";
import {
  decisionAliasChanges,
  decisionProposalChanges,
  decisionRelationshipRole,
  decisionRelationshipSummary,
  filterDecisionProposals,
  filterManualReviewItems,
  summarizeDecisionProposals,
  type DecisionProposalStatus,
} from "../termDecision";
import { RunDialog } from "./RunDialog";
import type {
  RunDecision,
  TaskOptions,
  TaskState,
  TermDecisionEvidence,
  TermDecisionManualReviewItem,
  TermDecisionProposal,
  TermDecisionReviewState,
  TermDecisionState,
  TermsResponse,
} from "../types";

const PAGE_SIZE = 20;
type DecisionTab = "proposals" | "manual";

function valueOrDash(value: string) {
  return value || "—";
}

function fieldLabel(field: string, language: Language) {
  const keys: Record<string, string> = {
    preferred_translation: "terms.decisionFieldTranslation",
    category: "terms.decisionFieldCategory",
    description: "terms.decisionFieldDescription",
    group_primary: "terms.decisionFieldGroup",
    disabled: "terms.decisionFieldStatus",
  };
  return translate(keys[field] ?? field, language);
}

function groupValue(state: TermDecisionState, states: TermDecisionState[], language: Language) {
  if (!state.group_primary) return translate("terms.decisionStandalone", language);
  const primary = states.find((item) => item.normalized === state.group_primary);
  return translate("terms.decisionMemberOf", language, { source: primary?.source ?? state.group_primary });
}

function changeValue(field: string, raw: string, state: TermDecisionState, states: TermDecisionState[], language: Language) {
  if (field === "group_primary") return raw ? groupValue(state, states, language) : translate("terms.decisionStandalone", language);
  if (field === "disabled") return raw ? translate("terms.decisionDisabledState", language) : translate("terms.decisionEnabledState", language);
  return valueOrDash(raw);
}

function StateDetails({ title, state, states, language }: { title: string; state: TermDecisionState; states: TermDecisionState[]; language: Language }) {
  return <details className="decision-state-details">
    <summary>{title}</summary>
    <dl>
      <div><dt>{translate("terms.decisionFieldTranslation", language)}</dt><dd>{valueOrDash(state.preferred_translation ?? "")}</dd></div>
      <div><dt>{translate("terms.decisionFieldCategory", language)}</dt><dd>{valueOrDash(state.category ?? "")}</dd></div>
      <div><dt>{translate("terms.decisionFieldDescription", language)}</dt><dd>{valueOrDash(state.description ?? "")}</dd></div>
      <div><dt>{translate("terms.decisionFieldGroup", language)}</dt><dd>{groupValue(state, states, language)}</dd></div>
      <div><dt>{translate("terms.decisionFieldAliases", language)}</dt><dd>{state.aliases.length ? state.aliases.join(" · ") : "—"}</dd></div>
      <div><dt>{translate("terms.decisionFieldStatus", language)}</dt><dd>{state.disabled ? translate("terms.decisionDisabledState", language) : translate("terms.decisionEnabledState", language)}</dd></div>
    </dl>
  </details>;
}

function EvidenceDetails({ evidence, language }: { evidence: Record<string, TermDecisionEvidence>; language: Language }) {
  return <details className="decision-evidence">
    <summary>{translate("terms.decisionEvidence", language)}</summary>
    <div className="decision-evidence-list">
      {Object.entries(evidence).map(([normalized, value]) => <div className="decision-evidence-term" key={normalized}>
        <strong>{normalized}</strong>
        <span>{translate("terms.decisionHits", language)} {value.hit_count} · {translate("terms.decisionSourceHits", language)} {value.source_hit_count}</span>
        {Object.entries(value.alias_hit_counts).map(([alias, count]) => <span key={alias}>{translate("terms.decisionAliasHits", language)} {alias}: {count}</span>)}
        {value.samples.length > 0 && <div className="decision-samples">{value.samples.map((sample, index) => <div className="decision-sample" key={`${sample.file_id}:${sample.segment_id}:${index}`}>
          <small>{sample.file_id} · {sample.segment_id}{sample.match_view ? ` · ${sample.match_view}` : ""}</small>
          {sample.matched_forms?.length ? <small>{sample.matched_forms.map((form) => `${form.kind}: ${form.value}`).join(" · ")}</small> : null}
          <p>{sample.source}</p>
        </div>)}</div>}
      </div>)}
    </div>
  </details>;
}

function ProposalCard({ proposal, rejected, busy, running, language, onToggle }: { proposal: TermDecisionProposal; rejected: boolean; busy: boolean; running: boolean; language: Language; onToggle: () => void }) {
  const changes = proposal.before.flatMap((before, index) => {
    const after = proposal.after[index];
    if (!after) return [];
    return {
      before,
      after,
      fields: decisionProposalChanges(before, after),
      aliases: decisionAliasChanges(before, after),
      role: proposal.kind === "relationship" ? decisionRelationshipRole(after, proposal.after) : null,
    };
  }).sort((left, right) => {
    if (proposal.kind !== "relationship") return 0;
    const roleOrder = { primary: 0, member: 1 } as const;
    return (roleOrder[left.role ?? "member"] ?? 2) - (roleOrder[right.role ?? "member"] ?? 2);
  });
  const relationshipGroups = proposal.kind === "relationship"
    ? decisionRelationshipSummary(proposal.after)
    : [];
  return <article className={`term-decision-proposal ${rejected ? "rejected" : ""}`}>
    <header className="term-decision-proposal-heading">
      <div className="decision-proposal-title">
        <span className={`decision-kind ${proposal.kind}`}>{proposal.kind === "relationship" ? translate("terms.decisionRelationship", language) : translate("terms.decisionTermUpdate", language)}</span>
        <strong>{proposal.before.map((term) => term.source).join(" · ")}</strong>
        <small>{proposal.proposal_id}</small>
      </div>
      <div className="decision-proposal-state">
        <span className={rejected ? "decision-status rejected" : "decision-status accepted"}>{rejected ? translate("terms.decisionRejected", language) : translate("terms.decisionWillAccept", language)}</span>
        <button disabled={busy || running} onClick={onToggle}>{rejected ? translate("terms.decisionRestore", language) : translate("terms.decisionReject", language)}</button>
      </div>
    </header>
    {proposal.kind === "relationship" && relationshipGroups.length > 0 && <div className="decision-relationship-summary">
      {relationshipGroups.map((group) => <div className="decision-relationship-group" key={group.primary}>
        <span><strong>{translate("terms.decisionRelationshipPrimary", language, { source: group.primary })}</strong></span>
        <span><strong>{translate("terms.decisionRelationshipMembers", language, { sources: group.members.join(language === "zh-CN" ? "、" : ", ") })}</strong></span>
      </div>)}
    </div>}
    <div className="decision-change-list">{changes.map(({ before, after, fields, aliases, role }) => <div className="decision-term-change" key={before.normalized}>
      <div className="decision-term-change-heading"><div><strong>{before.source}</strong>{role && <span className={`decision-relation-role ${role}`}>{translate(role === "primary" ? "terms.groupPrimary" : "terms.groupMember", language)}</span>}</div><span>{after.disabled ? translate("terms.decisionDisabledState", language) : translate("terms.decisionEnabledState", language)}</span></div>
      {fields.map((change) => <div className="decision-field-change" key={change.field}>
        <span className="decision-field-label">{fieldLabel(change.field, language)}</span>
        <span className="decision-old-value">{changeValue(change.field, change.before, before, proposal.before, language)}</span>
        <span className="decision-arrow">→</span>
        <span className="decision-new-value">{changeValue(change.field, change.after, after, proposal.after, language)}</span>
      </div>)}
      {(aliases.added.length > 0 || aliases.removed.length > 0) && <div className="decision-alias-change"><span className="decision-field-label">{translate("terms.decisionFieldAliases", language)}</span><div>{aliases.removed.map((alias) => <span className="alias-chip removed" key={`removed:${alias}`}>− {alias}</span>)}{aliases.added.map((alias) => <span className="alias-chip added" key={`added:${alias}`}>+ {alias}</span>)}</div></div>}
      {fields.length === 0 && aliases.added.length === 0 && aliases.removed.length === 0 && <span className="decision-unchanged">{translate("terms.decisionNoVisibleChanges", language)}</span>}
      <div className="decision-state-pair"><StateDetails title={translate("terms.decisionBefore", language)} state={before} states={proposal.before} language={language} /><StateDetails title={translate("terms.decisionAfter", language)} state={after} states={proposal.after} language={language} /></div>
    </div>)}</div>
    <p className="decision-reason">{proposal.reason}</p>
    <EvidenceDetails evidence={proposal.evidence} language={language} />
  </article>;
}

function Pagination({ page, pageCount, language, onPage }: { page: number; pageCount: number; language: Language; onPage: (page: number) => void }) {
  if (pageCount <= 1) return null;
  return <nav className="term-decision-pages" aria-label={translate("terms.decisionPagination", language)}><button disabled={page === 0} onClick={() => onPage(page - 1)}>‹</button><span>{page + 1} / {pageCount}</span><button disabled={page + 1 >= pageCount} onClick={() => onPage(page + 1)}>›</button></nav>;
}

export function TermDecisionWorkspace({ project, language, task, onTask, onTerms, onClose, initialTab = "proposals", onReviewState, onNavigateToEditor }: {
  project: string;
  language: Language;
  task: TaskState | null;
  onTask: (task: TaskState) => void;
  onTerms: (terms: TermsResponse) => void;
  onClose: () => void;
  initialTab?: DecisionTab;
  onReviewState: (review: TermDecisionReviewState) => void;
  onNavigateToEditor: (item: TermDecisionManualReviewItem, tab: "edit" | "group") => void;
}) {
  const [review, setReview] = useState<TermDecisionReviewState | null>(null);
  const [options, setOptions] = useState<TaskOptions | null>(null);
  const [reviewLoading, setReviewLoading] = useState(true);
  const [tab, setTab] = useState<DecisionTab>(initialTab);
  const [search, setSearch] = useState("");
  const [kind, setKind] = useState("");
  const [status, setStatus] = useState<DecisionProposalStatus>("all");
  const [manualStatus, setManualStatus] = useState<"all" | "open" | "resolved">("open");
  const [page, setPage] = useState(0);
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState("");
  const [runDialogOpen, setRunDialogOpen] = useState(false);
  const [replaceDraft, setReplaceDraft] = useState(false);
  const contentRef = useRef<HTMLDivElement>(null);
  const running = Boolean(task && task.project === project && task.stage === "terminology_decision" && ["queued", "running", "cancelling"].includes(task.status));

  async function load() {
    const reviewRequest = api<TermDecisionReviewState>(`/api/v1/projects/${project}/terms/decision`);
    const optionsRequest = api<TaskOptions>(`/api/v1/projects/${project}/task-options/terminology_decision`);
    const nextReview = await reviewRequest;
    setReview(nextReview);
    setReviewLoading(false);
    onReviewState(nextReview);
    try {
      const nextOptions = await optionsRequest;
      setOptions(nextOptions);
    } catch (error) {
      setMessage(String(error));
    }
    return nextReview;
  }

  useEffect(() => {
    setReviewLoading(true);
    void load().catch((error) => { setReviewLoading(false); setMessage(String(error)); });
  }, [project]);
  useEffect(() => {
    if (task?.project !== project || task.stage !== "terminology_decision") return;
    if (["completed", "cancelled", "failed"].includes(task.status)) void load().catch((error) => setMessage(String(error)));
    if (task.status === "failed") setMessage(task.error ?? translate("common.requestFailed", language));
  }, [task?.task_id, task?.status]);

  const rejected = useMemo(() => new Set(review?.draft?.rejected_proposal_ids ?? []), [review]);
  const filtered = useMemo(() => filterDecisionProposals(review?.draft?.proposals ?? [], search, kind, status, rejected), [review, search, kind, status, rejected]);
  const summary = useMemo(() => summarizeDecisionProposals(review?.draft?.proposals ?? [], rejected), [review, rejected]);
  const manualItems = useMemo(() => review?.manual_review.items ?? [], [review]);
  const visible = filtered.slice(page * PAGE_SIZE, (page + 1) * PAGE_SIZE);
  const pageCount = Math.max(1, Math.ceil(filtered.length / PAGE_SIZE));
  const filteredManual = useMemo(() => filterManualReviewItems(manualItems, search, manualStatus), [manualItems, search, manualStatus]);
  const manualVisible = filteredManual.slice(page * PAGE_SIZE, (page + 1) * PAGE_SIZE);
  const manualPageCount = Math.max(1, Math.ceil(filteredManual.length / PAGE_SIZE));
  const progress = review?.manual_review ?? { total: 0, resolved: 0, remaining: 0 };
  const progressValues = { total: progress.total, resolved: progress.resolved, remaining: progress.remaining };
  const hasDraft = Boolean(review?.draft);
  const showManualTab = !hasDraft && manualItems.length > 0;

  useEffect(() => {
    if (tab === "manual" && !showManualTab) setTab("proposals");
  }, [tab, showManualTab]);

  function changeTab(next: DecisionTab) {
    if (next === "manual" && !showManualTab) return;
    setTab(next);
    setPage(0);
    setSearch("");
    contentRef.current?.scrollTo({ top: 0 });
  }
  function changePage(next: number) {
    setPage(next);
    contentRef.current?.scrollTo({ top: 0 });
  }
  function openRunDialog(replace: boolean) {
    if (!options || options.selected === 0) return;
    setReplaceDraft(replace);
    setRunDialogOpen(true);
  }
  async function startDecision(decision: RunDecision) {
    const resuming = decision.run_action === "resume";
    if (replaceDraft && !window.confirm(translate("terms.decisionReplaceConfirm", language))) return;
    const force = decision.force;
    if (force && !window.confirm(translate("terms.decisionForceConfirm", language))) return;
    if (!resuming && (review?.manual_review.remaining ?? 0) > 0 && !window.confirm(translate("terms.decisionManualReplaceConfirm", language, { remaining: review?.manual_review.remaining ?? 0, total: review?.manual_review.total ?? 0 }))) return;
    setRunDialogOpen(false);
    setBusy(true);
    setMessage("");
    try {
      const next = await api<TaskState>(`/api/v1/projects/${project}/tasks`, { method: "POST", body: JSON.stringify({ stage: "terminology_decision", language, replace_draft: replaceDraft, force, reuse_mixed_fingerprints: false, acknowledge_manual_review: !resuming, run_action: decision.run_action }) });
      onTask(next);
    } catch (error) { setMessage(String(error)); } finally { setBusy(false); }
  }
  async function setRejected(proposalId: string, value: boolean) {
    const next = new Set(rejected);
    if (value) next.add(proposalId); else next.delete(proposalId);
    setBusy(true);
    try {
      const result = await api<{ draft: NonNullable<TermDecisionReviewState["draft"]> }>(`/api/v1/projects/${project}/terms/decision/rejections`, { method: "PUT", body: JSON.stringify({ rejected_proposal_ids: [...next] }) });
      setReview((current) => current ? { ...current, draft: result.draft } : current);
    } catch (error) { setMessage(String(error)); } finally { setBusy(false); }
  }
  async function setManualResolved(item: TermDecisionManualReviewItem, value: boolean) {
    if (!review?.manual_review.items.some((entry) => entry.run_id === item.run_id && entry.normalized === item.normalized)) return;
    setBusy(true);
    try {
      const result = await api<{ manual_review: TermDecisionReviewState["manual_review"] }>(`/api/v1/projects/${project}/terms/decision/manual-review`, { method: "PUT", body: JSON.stringify({ run_id: item.run_id, normalized: item.normalized, resolved: value }) });
      const nextReview = review ? { ...review, manual_review: result.manual_review } : { draft: null, rollback: null, manual_review: result.manual_review };
      setReview(nextReview);
      onReviewState(nextReview);
    } catch (error) { setMessage(String(error)); } finally { setBusy(false); }
  }
  async function mutate(path: "apply" | "discard" | "rollback", confirmation: string) {
    if (!window.confirm(confirmation)) return;
    setBusy(true);
    setMessage("");
    try {
      const result = await api<{ terms?: TermsResponse }>(`/api/v1/projects/${project}/terms/decision/${path}`, { method: "POST", body: JSON.stringify({ confirm: true }) });
      if (result.terms) onTerms(result.terms);
      const next = await load();
      if (path === "apply") {
        if (next.manual_review.remaining > 0) {
          setTab("manual");
          setPage(0);
          setSearch("");
        }
        else onClose();
      }
    } catch (error) { setMessage(String(error)); } finally { setBusy(false); }
  }

  return (
    <section className="term-decision-workspace">
      <header className="page-heading settings-action-heading term-decision-action-heading">
        <div><p className="term-decision-back"><button className="link-button" onClick={onClose}>← {translate("terms.decisionBackToLibrary", language)}</button></p><h1>{translate("terms.decisionTitle", language)}</h1><p>{translate("terms.decisionHint", language)}</p></div>
        <div className="button-group">
          {review?.draft && <button className="danger-button" disabled={busy || running} onClick={() => mutate("discard", translate("terms.decisionDiscardConfirm", language))}>{translate("terms.decisionDiscard", language)}</button>}
          {review?.draft && <button className="quiet-button" disabled={busy || running || !options} onClick={() => openRunDialog(true)}>{translate("terms.decisionRegenerate", language)}</button>}
          {review?.draft && <button className="primary-button" disabled={busy || running} onClick={() => mutate("apply", translate("terms.decisionApplyConfirm", language, summary))}>{translate("terms.decisionApply", language)}</button>}
          {!review?.draft && <button className="primary-button" disabled={busy || running || !options || options.selected === 0} onClick={() => openRunDialog(false)}>{translate("terms.decisionGenerate", language)}</button>}
          {!review?.draft && review?.rollback && <button disabled={busy || running} onClick={() => mutate("rollback", translate("terms.decisionRollbackConfirm", language))}>{translate("terms.decisionRollback", language)}</button>}
        </div>
      </header>
      {review?.draft && <div className="term-decision-warning"><strong>{translate("terms.decisionRevisionWarning", language)}</strong><span>{translate("terms.decisionRevisionWarningHint", language)}</span></div>}
      {message && <p className="inline-message error-text">{message}</p>}
      {reviewLoading ? <p className="diagnostics-empty">{translate("terms.decisionLoading", language)}</p> : <>
        {running && <div className="term-decision-running"><strong>{translate("terms.decisionRunning", language)} {task?.completed_segments ?? 0} / {task?.total_segments ?? 0}</strong><span>{translate("terms.decisionCloseHint", language)}</span></div>}
        {(review?.draft || showManualTab) && <div className="term-decision-tabs" role="tablist"><button className={tab === "proposals" ? "active" : ""} onClick={() => changeTab("proposals")}>{translate("terms.decisionProposalTab", language)} {review?.draft?.proposals.length ?? 0}</button>{showManualTab && <button className={tab === "manual" ? "active" : ""} onClick={() => changeTab("manual")}>{translate("terms.decisionManualTab", language)} {progress.remaining}/{progress.total}</button>}</div>}
        <div className="term-decision-content" ref={contentRef}>
          {tab === "proposals" && review?.draft && <><div className="term-decision-summary"><span>{translate("terms.decisionAccepted", language)} {summary.accepted}</span><span>{translate("terms.decisionRejected", language)} {summary.rejected}</span><span>{translate("terms.decisionDisabled", language)} {summary.disabled}</span><span>{translate("terms.decisionTranslations", language)} {summary.translations}</span><span>{translate("terms.decisionStructural", language)} {summary.structural}</span></div>{review.draft.needs_review.length > 0 && <div className="term-decision-review-preview"><strong>{translate("terms.decisionNeedsReview", language)} {review.draft.needs_review.length}</strong><span>{translate("terms.decisionNeedsReviewHint", language)}</span><div>{review.draft.needs_review.slice(0, 3).map((item) => <span key={item.normalized}>{item.source}</span>)}</div></div>}<div className="term-decision-filters term-decision-sticky"><input value={search} onChange={(event) => { setSearch(event.target.value); setPage(0); }} placeholder={translate("terms.decisionSearch", language)} /><select value={kind} onChange={(event) => { setKind(event.target.value); setPage(0); }}><option value="">{translate("terms.decisionAllKinds", language)}</option><option value="term_update">{translate("terms.decisionTermUpdate", language)}</option><option value="relationship">{translate("terms.decisionRelationship", language)}</option></select><select value={status} onChange={(event) => { setStatus(event.target.value as DecisionProposalStatus); setPage(0); }}><option value="all">{translate("terms.decisionAllStatus", language)}</option><option value="accepted">{translate("terms.decisionAcceptedStatus", language)}</option><option value="rejected">{translate("terms.decisionRejectedStatus", language)}</option></select><Pagination page={page} pageCount={pageCount} language={language} onPage={changePage} /></div><div className="term-decision-list">{visible.map((proposal) => <ProposalCard key={proposal.proposal_id} proposal={proposal} rejected={rejected.has(proposal.proposal_id)} busy={busy} running={running} language={language} onToggle={() => void setRejected(proposal.proposal_id, !rejected.has(proposal.proposal_id))} />)}</div>{!visible.length && <p className="diagnostics-empty">{translate("terms.decisionNoMatch", language)}</p>}<Pagination page={page} pageCount={pageCount} language={language} onPage={changePage} /></>}
          {tab === "manual" && showManualTab && <><div className="manual-review-summary"><strong>{translate("terms.decisionManualProgress", language, progressValues)}</strong><span>{translate("terms.decisionManualHint", language)}</span></div><div className="term-decision-filters term-decision-sticky"><input value={search} onChange={(event) => { setSearch(event.target.value); setPage(0); }} placeholder={translate("terms.decisionManualSearch", language)} /><select value={manualStatus} onChange={(event) => { setManualStatus(event.target.value as typeof manualStatus); setPage(0); }}><option value="open">{translate("terms.decisionManualOpen", language)}</option><option value="resolved">{translate("terms.decisionManualResolved", language)}</option><option value="all">{translate("terms.decisionAllStatus", language)}</option></select><Pagination page={page} pageCount={manualPageCount} language={language} onPage={changePage} /></div><div className="manual-review-list">{manualVisible.map((item) => <article className={`manual-review-card ${item.resolved ? "resolved" : ""}`} key={`${item.run_id}:${item.normalized}`}><header><div><strong>{item.source}</strong><small>{item.normalized}</small></div><span>{item.resolved ? translate("terms.decisionManualResolvedBadge", language) : translate("terms.decisionManualOpenBadge", language)}</span></header><p>{item.reason}</p><small>{translate("terms.decisionHits", language)} {item.evidence.hit_count}</small><EvidenceDetails evidence={{ [item.normalized]: item.evidence }} language={language} /><div className="manual-review-actions"><button disabled={busy} onClick={() => onNavigateToEditor(item, "edit")}>{translate("terms.decisionEditTerm", language)}</button><button disabled={busy} onClick={() => onNavigateToEditor(item, "group")}>{translate("terms.decisionViewRelation", language)}</button><button disabled={busy} onClick={() => void setManualResolved(item, !item.resolved)}>{item.resolved ? translate("terms.decisionRestoreManual", language) : translate("terms.decisionMarkHandled", language)}</button></div></article>)}</div>{!manualVisible.length && <p className="diagnostics-empty">{translate("terms.decisionManualEmpty", language)}</p>}<Pagination page={page} pageCount={manualPageCount} language={language} onPage={changePage} /></>}
          {!review?.draft && tab === "proposals" && !running && <p className="diagnostics-empty">{translate("terms.decisionNoDraft", language)}</p>}
        </div>
      </>}
      {runDialogOpen && options && <RunDialog
        options={options}
        language={language}
        onClose={() => { setRunDialogOpen(false); setReplaceDraft(false); }}
        onStart={(decision) => { void startDecision(decision); }}
      />}
    </section>
  );
}
