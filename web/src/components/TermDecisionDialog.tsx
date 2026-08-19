import { useEffect, useMemo, useState } from "react";
import { api } from "../api";
import { translate, type Language } from "../i18n";
import { filterDecisionProposals, summarizeDecisionProposals } from "../termDecision";
import type {
  TaskOptions,
  TaskState,
  TermDecisionReviewState,
  TermDecisionState,
  TermsResponse,
} from "../types";
import { Modal } from "./Modal";

const PAGE_SIZE = 50;

function stateText(value: TermDecisionState) {
  return [
    `${value.source}${value.preferred_translation ? ` → ${value.preferred_translation}` : ""}`,
    value.category ? `category: ${value.category}` : "",
    value.aliases.length ? `aliases: ${value.aliases.join(", ")}` : "",
    value.group_primary ? `group: ${value.group_primary}` : "",
    value.description ? `description: ${value.description}` : "",
    value.disabled ? "disabled" : "",
  ].filter(Boolean).join("\n");
}

export function TermDecisionDialog({
  project,
  language,
  task,
  onTask,
  onTerms,
  onClose,
}: {
  project: string;
  language: Language;
  task: TaskState | null;
  onTask: (task: TaskState) => void;
  onTerms: (terms: TermsResponse) => void;
  onClose: () => void;
}) {
  const [review, setReview] = useState<TermDecisionReviewState | null>(null);
  const [options, setOptions] = useState<TaskOptions | null>(null);
  const [search, setSearch] = useState("");
  const [kind, setKind] = useState("");
  const [page, setPage] = useState(0);
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState("");

  const running = Boolean(task && task.project === project
    && task.stage === "terminology_decision"
    && ["queued", "running", "cancelling"].includes(task.status));

  async function load() {
    const [nextReview, nextOptions] = await Promise.all([
      api<TermDecisionReviewState>(`/api/v1/projects/${project}/terms/decision`),
      api<TaskOptions>(`/api/v1/projects/${project}/task-options/terminology_decision`),
    ]);
    setReview(nextReview);
    setOptions(nextOptions);
  }

  useEffect(() => {
    void load().catch((error) => setMessage(String(error)));
  }, [project]);

  useEffect(() => {
    if (task?.project !== project || task.stage !== "terminology_decision") return;
    if (task.status === "completed") void load().catch((error) => setMessage(String(error)));
    if (task.status === "failed") setMessage(task.error ?? translate("common.requestFailed", language));
  }, [task?.task_id, task?.status]);

  const rejected = useMemo(
    () => new Set(review?.draft?.rejected_proposal_ids ?? []),
    [review],
  );
  const filtered = useMemo(
    () => filterDecisionProposals(review?.draft?.proposals ?? [], search, kind),
    [review, search, kind],
  );
  const summary = useMemo(
    () => summarizeDecisionProposals(review?.draft?.proposals ?? [], rejected),
    [review, rejected],
  );
  const pageCount = Math.max(1, Math.ceil(filtered.length / PAGE_SIZE));
  const visible = filtered.slice(page * PAGE_SIZE, (page + 1) * PAGE_SIZE);

  async function generate() {
    const replace = Boolean(review?.draft);
    if (replace && !window.confirm(translate("terms.decisionReplaceConfirm", language))) return;
    setBusy(true);
    setMessage("");
    try {
      const next = await api<TaskState>(`/api/v1/projects/${project}/tasks`, {
        method: "POST",
        body: JSON.stringify({
          stage: "terminology_decision",
          language,
          replace_draft: replace,
          force: false,
          reuse_mixed_fingerprints: false,
          run_action: null,
        }),
      });
      onTask(next);
    } catch (error) {
      setMessage(String(error));
    } finally {
      setBusy(false);
    }
  }

  async function setRejected(proposalId: string, value: boolean) {
    const next = new Set(rejected);
    if (value) next.add(proposalId); else next.delete(proposalId);
    setBusy(true);
    try {
      const result = await api<{ draft: NonNullable<TermDecisionReviewState["draft"]> }>(
        `/api/v1/projects/${project}/terms/decision/rejections`,
        { method: "PUT", body: JSON.stringify({ rejected_proposal_ids: [...next] }) },
      );
      setReview((current) => current ? { ...current, draft: result.draft } : current);
    } catch (error) {
      setMessage(String(error));
    } finally {
      setBusy(false);
    }
  }

  async function mutate(path: "apply" | "discard" | "rollback", confirmation: string) {
    if (!window.confirm(confirmation)) return;
    setBusy(true);
    setMessage("");
    try {
      const result = await api<{ terms?: TermsResponse }>(
        `/api/v1/projects/${project}/terms/decision/${path}`,
        { method: "POST", body: JSON.stringify({ confirm: true }) },
      );
      if (result.terms) onTerms(result.terms);
      await load();
    } catch (error) {
      setMessage(String(error));
    } finally {
      setBusy(false);
    }
  }

  return (
    <Modal ariaLabel={translate("terms.decisionTitle", language)}>
      <div className="term-decision-dialog">
        <header className="term-decision-heading">
          <div><h2>{translate("terms.decisionTitle", language)}</h2><p>{translate("terms.decisionHint", language)}</p></div>
          <button className="quiet-button" onClick={onClose}>{translate("common.cancel", language)}</button>
        </header>
        {options && <div className="term-decision-options">
          <span>{translate("terms.decisionPreset", language)} <strong>{options.preset.id}</strong> · {options.preset.model}</span>
          <span>{translate("terms.decisionScope", language, { selected: options.selected, protected: options.protected ?? 0 })}</span>
          <span>{translate("terms.decisionEstimate", language, { requests: options.estimated_requests ?? 0, tokens: options.estimated_input_tokens ?? 0 })}</span>
        </div>}
        {message && <p className="inline-message error-text">{message}</p>}
        {running && <p>{translate("terms.decisionRunning", language)} {task?.completed_segments ?? 0} / {task?.total_segments ?? 0}</p>}
        <div className="term-decision-actions">
          <button className="primary-button" disabled={busy || running || !options || options.selected === 0} onClick={generate}>
            {review?.draft ? translate("terms.decisionRegenerate", language) : translate("terms.decisionGenerate", language)}
          </button>
          {review?.rollback && <button disabled={busy || running} onClick={() => mutate("rollback", translate("terms.decisionRollbackConfirm", language))}>{translate("terms.decisionRollback", language)}</button>}
        </div>
        {review?.draft ? <>
          <div className="term-decision-summary">
            <span>{translate("terms.decisionAccepted", language)} {summary.accepted}</span>
            <span>{translate("terms.decisionRejected", language)} {summary.rejected}</span>
            <span>{translate("terms.decisionDisabled", language)} {summary.disabled}</span>
            <span>{translate("terms.decisionTranslations", language)} {summary.translations}</span>
            <span>{translate("terms.decisionStructural", language)} {summary.structural}</span>
          </div>
          <div className="term-decision-filters">
            <input value={search} onChange={(event) => { setSearch(event.target.value); setPage(0); }} placeholder={translate("terms.decisionSearch", language)} />
            <select value={kind} onChange={(event) => { setKind(event.target.value); setPage(0); }}>
              <option value="">{translate("terms.decisionAllKinds", language)}</option>
              <option value="term_update">{translate("terms.decisionTermUpdate", language)}</option>
              <option value="relationship">{translate("terms.decisionRelationship", language)}</option>
            </select>
          </div>
          <div className="term-decision-list">
            {visible.map((proposal) => <article className={`term-decision-proposal ${rejected.has(proposal.proposal_id) ? "rejected" : ""}`} key={proposal.proposal_id}>
              <div className="term-decision-proposal-heading"><strong>{proposal.before.map((term) => term.source).join(" + ")}</strong><code>{proposal.proposal_id}</code></div>
              <div className="term-decision-diff">
                <pre><b>{translate("terms.decisionBefore", language)}</b>{"\n"}{proposal.before.map(stateText).join("\n\n")}</pre>
                <pre><b>{translate("terms.decisionAfter", language)}</b>{"\n"}{proposal.after.map(stateText).join("\n\n")}</pre>
              </div>
              <p>{proposal.reason}</p>
              <small>{translate("terms.decisionHits", language)} {Object.values(proposal.evidence).reduce((count, value) => count + value.hit_count, 0)}</small>
              {Object.entries(proposal.evidence).map(([normalized, evidence]) => Object.keys(evidence.alias_hit_counts).length > 0 && <small key={normalized}>{translate("terms.decisionAliasHits", language)} {Object.entries(evidence.alias_hit_counts).map(([alias, count]) => `${alias}: ${count}`).join(" · ")}</small>)}
              <button disabled={busy || running} onClick={() => setRejected(proposal.proposal_id, !rejected.has(proposal.proposal_id))}>
                {rejected.has(proposal.proposal_id) ? translate("terms.decisionRestore", language) : translate("terms.decisionReject", language)}
              </button>
            </article>)}
          </div>
          {pageCount > 1 && <div className="term-decision-pages"><button disabled={page === 0} onClick={() => setPage(page - 1)}>‹</button><span>{page + 1} / {pageCount}</span><button disabled={page + 1 >= pageCount} onClick={() => setPage(page + 1)}>›</button></div>}
          {review.draft.needs_review.length > 0 && <section className="term-decision-review"><h3>{translate("terms.decisionNeedsReview", language)} ({review.draft.needs_review.length})</h3>{review.draft.needs_review.map((item) => <p key={item.normalized}><strong>{item.source}</strong> · {item.reason} · {translate("terms.decisionHits", language)} {item.evidence.hit_count}</p>)}</section>}
          <div className="modal-actions">
            <button className="danger-button" disabled={busy || running} onClick={() => mutate("discard", translate("terms.decisionDiscardConfirm", language))}>{translate("terms.decisionDiscard", language)}</button>
            <button className="primary-button" disabled={busy || running} onClick={() => mutate("apply", translate("terms.decisionApplyConfirm", language, summary))}>{translate("terms.decisionApply", language)}</button>
          </div>
        </> : !running && <p>{translate("terms.decisionNoDraft", language)}</p>}
      </div>
    </Modal>
  );
}
