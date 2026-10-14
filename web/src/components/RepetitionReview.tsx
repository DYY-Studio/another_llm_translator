import { useEffect, useMemo, useRef, useState } from "react";
import { api } from "../api";
import { errorMessage, translate, type Language } from "../i18n";
import type { ProjectOverview, TaskState } from "../types";
import { RunDialog, type RepetitionRunOptions } from "./RunDialog";

interface Choice { accepted: boolean; target: string }
interface Occurrence {
  id: string; segment_id: string; file_id: string; part_id: string;
  variant: number; origin?: string; previous: string[]; next: string[];
}
interface Proposal { id: string; members: string[]; target: string; reason: string }
interface Group {
  id: string; source: string; translations: string[]; occurrences: Occurrence[];
  proposals: Proposal[]; screening: string; keep: string[]; uncertain: string[];
}
interface Draft {
  run_id: string; groups: Group[]; choices: Record<string, Choice>;
  evidence: Record<string, { source_key: string }>; sources: Record<string, string>;
}
interface ReviewState { draft: Draft | null; can_rollback: boolean; rollback_run_id: string | null }
type Action = "generate" | "apply" | "discard" | "rollback";
const PAGE_SIZE = 20;
const GROUP_PAGE_SIZE = 10;
const isRunning = (task: TaskState | null) => !!task && ["queued", "running", "cancelling"].includes(task.status);

export function RepetitionReview({ project, files, language, onClose, onChanged }: {
  project: string; files: ProjectOverview["files"]; language: Language;
  onClose: () => void; onChanged: () => Promise<void>;
}) {
  const base = `/api/v1/projects/${encodeURIComponent(project)}/repetition`;
  const t = (key: string, params: Record<string, string | number> = {}) => translate(`repetition.${key}`, language, params);
  const [state, setState] = useState<ReviewState | null>(null);
  const [task, setTask] = useState<TaskState | null>(null);
  const [onlyFile, setOnlyFile] = useState("");
  const [options, setOptions] = useState<RepetitionRunOptions | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [message, setMessage] = useState("");
  const [confirm, setConfirm] = useState<Action | null>(null);
  const [filter, setFilter] = useState("all");
  const [search, setSearch] = useState("");
  const [groupPage, setGroupPage] = useState(0);
  const contentRef = useRef<HTMLDivElement>(null);
  const active = isRunning(task);
  const draft = state?.draft;
  const groups = useMemo(() => (draft?.groups ?? []).filter(g => (
    (filter === "all" || (filter === "suggested" && g.proposals.length > 0)
      || (filter === "uncertain" && g.uncertain.length > 0) || (filter === "skip" && g.screening === "skip"))
    && (!search || g.source.includes(search) || g.translations.some(text => text.includes(search)))
  )), [draft, filter, search]);
  const acceptedCount = draft?.groups.reduce((n, g) => n + g.proposals.filter(p => draft.choices[p.id]?.accepted !== false).length, 0) ?? 0;

  useEffect(() => {
    let alive = true;
    Promise.all([api<ReviewState>(base), api<{ tasks: TaskState[] }>("/api/v1/tasks/active")])
      .then(([review, tasks]) => { if (alive) {
        setState(review); setTask(tasks.tasks.find(item => item.project === project && item.stage === "repetition_consistency") ?? null);
      } }).catch(value => { if (alive) setError(errorMessage(value, language)); });
    return () => { alive = false; };
  }, [base, language, project]);

  useEffect(() => {
    if (!task || !isRunning(task)) return;
    let alive = true;
    let timer: ReturnType<typeof setTimeout>;
    const poll = async () => {
      try {
        const next = await api<TaskState>(`/api/v1/tasks/${task.task_id}`);
        if (!alive) return;
        if (!isRunning(next)) {
          const review = await api<ReviewState>(base);
          if (!alive) return;
          setState(review);
          if (next.error) setError(errorMessage(next.error, language));
          if (Array.isArray(next.summary?.warnings)) setMessage(next.summary.warnings.filter(value => typeof value === "string").join("\n"));
        } else timer = setTimeout(poll, 1000);
        setTask(next);
      } catch (value) { if (alive) setError(errorMessage(value, language)); }
    };
    timer = setTimeout(poll, 500);
    return () => { alive = false; clearTimeout(timer); };
  }, [task?.task_id, active, base, language]);

  function changePage(value: number) { setGroupPage(value); contentRef.current?.scrollTo({ top: 0 }); }
  function resetPages() { changePage(0); }

  async function inspect() {
    setBusy(true); setError(""); setMessage("");
    try {
      setOptions(await api<RepetitionRunOptions>(`${base}/options?language=${language}${onlyFile ? `&only_file=${encodeURIComponent(onlyFile)}` : ""}`));
      setConfirm("generate");
    } catch (value) { setError(errorMessage(value, language)); }
    finally { setBusy(false); }
  }

  async function choose(proposal: Proposal, value: Choice) {
    if (!draft) return;
    setBusy(true); setError("");
    try {
      const result = await api<{ choices: Record<string, Choice>; warnings: string[] }>(`${base}/choices`, {
        method: "PUT", body: JSON.stringify({ run_id: draft.run_id, choices: { [proposal.id]: value } }),
      });
      setState(current => current?.draft ? { ...current, draft: { ...current.draft,
        choices: { ...current.draft.choices, ...result.choices } } } : current);
      if (result.warnings.length) setMessage(result.warnings.join("\n"));
    } catch (value) { setError(errorMessage(value, language)); }
    finally { setBusy(false); }
  }

  async function execute() {
    if (!confirm) return;
    setBusy(true); setError(""); setMessage("");
    try {
      if (confirm === "generate") {
        setTask(await api<TaskState>(`/api/v1/projects/${encodeURIComponent(project)}/tasks`, {
          method: "POST", body: JSON.stringify({ stage: "repetition_consistency", language, only_file: onlyFile || null,
            replace_draft: !!draft }),
        }));
      } else {
        const result = await api<{ changed?: number; warnings: string[]; validation_warning_count?: number }>(`${base}/${confirm}`, {
          method: "POST", body: JSON.stringify({ confirm: true,
            run_id: confirm === "rollback" ? state?.rollback_run_id : draft?.run_id,
            ...(confirm === "apply" ? { choices: draft?.choices } : {}) }),
        });
        setState(await api<ReviewState>(base));
        setMessage([result.changed !== undefined ? t("changed", { count: result.changed }) : t("discarded"),
          result.validation_warning_count ? t("validationWarnings", { count: result.validation_warning_count }) : "",
          ...result.warnings].filter(Boolean).join("\n"));
        await onChanged();
      }
      setConfirm(null); resetPages();
    } catch (value) { setError(errorMessage(value, language)); }
    finally { setBusy(false); }
  }

  const proposalCount = draft?.groups.reduce((n, g) => n + g.proposals.length, 0) ?? 0;
  const pageCount = Math.max(1, Math.ceil(groups.length / GROUP_PAGE_SIZE));

  return <section className="term-decision-workspace">
    <header className="page-heading settings-action-heading term-decision-action-heading">
      <div><p className="term-decision-back"><button className="link-button" onClick={onClose}>← {t("back")}</button></p>
        <h1>{t("title")}</h1><p>{t("hint")}</p></div>
      <div className="button-group">
        {draft && <button className="danger-button" disabled={busy || active || !!confirm} onClick={() => setConfirm("discard")}>{t("discard")}</button>}
        <button className={draft ? "quiet-button" : "primary-button"} disabled={busy || active || !!confirm} onClick={() => void inspect()}>{draft ? t("replace") : t("generate")}</button>
        {draft && <button className="primary-button" disabled={busy || active || !acceptedCount || !!confirm} onClick={() => setConfirm("apply")}>{t("apply", { count: acceptedCount })}</button>}
        {state?.can_rollback && <button className="quiet-button" disabled={busy || active || !!confirm} onClick={() => setConfirm("rollback")}>{t("rollback")}</button>}
      </div>
    </header>
    {error && <div className="error-banner" role="alert">{error}</div>}
    {message && <div className="warning-banner" role="status">{message}</div>}
    <div className="repetition-actions">
      <label htmlFor="repetition-scope">{t("scope")}</label>
      <select id="repetition-scope" value={onlyFile} disabled={busy || active || !!confirm} onChange={e => { setOnlyFile(e.target.value); setOptions(null); }}>
        <option value="">{translate("workspace.allFiles", language)}</option>
        {files.map(f => <option value={f.file_id} key={f.file_id}>{f.name}</option>)}
      </select>
    </div>
    {task && <div className="repetition-actions" role="status">
      <span>{t(`status.${task.status}`)} {task.completed_segments}/{task.total_segments}</span>
      <span>{t("usage", { input: task.usage.input_tokens, output: task.usage.output_tokens })}{!task.usage.available && ` (${t("partialUsage")})`}</span>
      {active && <button className="quiet-button" disabled={task.status === "cancelling"} onClick={() => {
        api<TaskState>(`/api/v1/tasks/${task.task_id}/cancel`, { method: "POST" }).then(setTask).catch(value => setError(errorMessage(value, language)));
      }}>{translate("common.cancel", language)}</button>}
    </div>}
    {confirm && confirm !== "generate" && <section className="warning-banner">
      <p>{t(`confirm.${confirm}`)}</p>
      <div className="repetition-actions"><button className="primary-button" disabled={busy} onClick={() => void execute()}>{t("confirm")}</button>
        <button className="quiet-button" disabled={busy} onClick={() => setConfirm(null)}>{translate("common.cancel", language)}</button></div>
    </section>}
    <div className="term-decision-content" ref={contentRef}>
    {!state ? <p>{translate("common.loading", language)}</p> : !draft ? <p>{t("noDraft")}</p> : <>
      <div className="term-decision-summary"><span>{t("groupCount", { count: draft.groups.length })}</span>
        <span>{t("acceptedCount", { count: acceptedCount })}</span><span>{t("rejectedCount", { count: proposalCount - acceptedCount })}</span></div>
      <div className="term-decision-filters term-decision-sticky repetition-filters">
        <input aria-label={t("search")} placeholder={t("search")} value={search} onChange={e => { setSearch(e.target.value); resetPages(); }} />
        <select aria-label={t("filter")} value={filter} onChange={e => { setFilter(e.target.value); resetPages(); }}>
          {["all", "suggested", "uncertain", "skip"].map(value => <option key={value} value={value}>{t(`filter.${value}`)}</option>)}
        </select>
        <nav className="term-decision-pages" aria-label={t("pagination")}>
          <button disabled={groupPage === 0} onClick={() => changePage(groupPage - 1)}>‹</button>
          <span>{groupPage + 1} / {pageCount}</span>
          <button disabled={groupPage + 1 >= pageCount} onClick={() => changePage(groupPage + 1)}>›</button>
        </nav>
      </div>
      <div className="term-decision-list">{groups.slice(groupPage * GROUP_PAGE_SIZE, (groupPage + 1) * GROUP_PAGE_SIZE).map(group =>
        <GroupCard key={`${draft.run_id}:${group.id}`} group={group} draft={draft} files={files} language={language}
          disabled={busy || active || !!confirm} onChoose={choose} />)}</div>
      {!groups.length && <p className="diagnostics-empty">{t("noMatch")}</p>}

    </>}
    </div>
    {confirm === "generate" && options && <RunDialog repetitionOptions={options} replaceDraft={!!draft} busy={busy} error={error}
      language={language} onClose={() => setConfirm(null)} onStart={() => void execute()} />}
  </section>;
}

function GroupCard({ group, draft, files, language, disabled, onChoose }: {
  group: Group; draft: Draft; files: ProjectOverview["files"]; language: Language; disabled: boolean;
  onChoose: (proposal: Proposal, choice: Choice) => Promise<void>;
}) {
  const t = (key: string, params: Record<string, string | number> = {}) => translate(`repetition.${key}`, language, params);
  const [page, setPage] = useState(0);
  const [openContexts, setOpenContexts] = useState<Record<string, boolean>>({});
  const occurrenceById = useMemo(() => new Map(group.occurrences.map(o => [o.id, o])), [group]);
  const proposalByMember = useMemo(() => new Map(group.proposals.flatMap(p => p.members.map(id => [id, p] as const))), [group]);
  const uncertain = useMemo(() => new Set(group.uncertain), [group]);
  return <section className="term-decision-proposal repetition-group" key={group.id}>
        <h3>{t("source")}</h3><pre className="repetition-text">{group.source}</pre>
        <p>{t(`screening.${group.screening}`)}</p>
        {group.proposals.map(proposal => {
          const choice = draft.choices[proposal.id] ?? { accepted: true, target: proposal.target };
          const members = proposal.members.map(id => occurrenceById.get(id)!);
          const variants = [...new Map(members.map(o => [o.variant, o])).values()];
          return <article className={`term-decision-proposal ${choice.accepted ? "" : "rejected"}`} key={proposal.id}>
            <div className="repetition-actions"><strong>{t("unify", { count: members.length })}</strong>
              <button className="quiet-button" disabled={disabled} onClick={() => void onChoose(proposal, { ...choice, accepted: !choice.accepted })}>
                {t(choice.accepted ? "reject" : "restore")}</button></div>
            <label>{t("target")}<select value={members.find(o => o.id === choice.target)?.variant} disabled={disabled}
              onChange={e => { const member = variants.find(o => o.variant === Number(e.target.value)); if (member) void onChoose(proposal, { ...choice, target: member.id }); }}>
              {variants.map(o => <option key={o.variant} value={o.variant}>{group.translations[o.variant]}</option>)}
            </select></label>
            <p>{proposal.reason}</p>
          </article>;
        })}
        <div className="repetition-actions"><strong>{t("occurrences")}</strong>
          <button className="quiet-button" disabled={page === 0} onClick={() => { setPage(n => n - 1); setOpenContexts({}); }}>{t("previous")}</button>
          <span>{page + 1}/{Math.ceil(group.occurrences.length / PAGE_SIZE)}</span>
          <button className="quiet-button" disabled={(page + 1) * PAGE_SIZE >= group.occurrences.length} onClick={() => { setPage(n => n + 1); setOpenContexts({}); }}>{t("next")}</button>
        </div>
        {group.occurrences.slice(page * PAGE_SIZE, (page + 1) * PAGE_SIZE).map(o => {
          const proposal = proposalByMember.get(o.id);
          const choice = proposal ? draft.choices[proposal.id] ?? { accepted: true, target: proposal.target } : null;
          const target = choice?.accepted ? occurrenceById.get(choice.target) : null;
          return <article className="term-decision-proposal" key={o.id}>
            <small>{files.find(f => f.file_id === o.file_id)?.name ?? o.file_id} / {o.part_id} / {o.segment_id}
              {o.origin === "web" && ` (${t("manual")})`}</small>
            <div className="decision-state-pair"><div><strong>{t("before")}</strong><pre className="repetition-text">{group.translations[o.variant]}</pre></div>
              <div><strong>{target ? t("after") : uncertain.has(o.id) ? t("needsReview") : t("keep")}</strong>
                <pre className="repetition-text">{group.translations[target?.variant ?? o.variant]}</pre></div></div>
            <details onToggle={e => { const open = e.currentTarget.open; setOpenContexts(current => ({ ...current, [o.id]: open })); }}>
              <summary>{t("context")}</summary>
              {openContexts[o.id] && <><strong>{t("previousContext")}</strong>{o.previous.map(id => <pre className="repetition-text" key={id}>{draft.sources[draft.evidence[id].source_key]}</pre>)}
                <strong>{t("nextContext")}</strong>{o.next.map(id => <pre className="repetition-text" key={id}>{draft.sources[draft.evidence[id].source_key]}</pre>)}</>}
            </details>
          </article>;
        })}
      </section>;
}
