import { useMemo, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { api } from "../api";
import { compactKnowledgeRanges } from "../termKnowledge";
import { errorMessage, translate, type Language } from "../i18n";
import type { EntityKnowledge, KnowledgeItem, TermsResponse } from "../types";

export function EntityKnowledgeEditor({ project, projectId, normalized, revision, language, onChanged, onFindSegment }: {
  project: string; projectId: string; normalized: string; revision: number; language: Language;
  onChanged: (value: TermsResponse) => Promise<void>; onFindSegment: (source: string, segmentId: string) => void;
}) {
  const [explanationsExpanded, setExplanationsExpanded] = useState(false);
  const [fragmentsExpanded, setFragmentsExpanded] = useState(false);
  const expanded = explanationsExpanded || fragmentsExpanded;
  const query = useQuery({
    queryKey: ["entity-knowledge", projectId, normalized, revision],
    queryFn: ({ signal }) => api<EntityKnowledge>(`/api/v1/projects/${project}/terms/knowledge?normalized=${encodeURIComponent(normalized)}`, { signal }),
    enabled: expanded,
    staleTime: Infinity,
    retry: false,
  });
  if (query.data) return <KnowledgeForm key={`${query.data.entity_key}:${query.data.terms_revision}`} project={project} view={query.data} language={language} onChanged={onChanged} onFindSegment={onFindSegment} explanationsExpanded={explanationsExpanded} fragmentsExpanded={fragmentsExpanded} setExplanationsExpanded={setExplanationsExpanded} setFragmentsExpanded={setFragmentsExpanded} />;
  const t = (key: string) => translate(`knowledge.${key}`, language);
  const loading = query.isPending && expanded;
  const error = query.error ? errorMessage(query.error, language) : "";
  return <section className="entity-knowledge" aria-label={t("title")}>
    <details open={explanationsExpanded} onToggle={(event) => setExplanationsExpanded(event.currentTarget.open)}>
      <summary><strong>{t("title")}</strong></summary>
      {explanationsExpanded && (loading ? <p>{translate("common.loading", language)}</p> : error ? <p className="error-text">{error}</p> : null)}
    </details>
    <details open={fragmentsExpanded} onToggle={(event) => setFragmentsExpanded(event.currentTarget.open)}>
      <summary><strong>{t("fragments")}</strong></summary>
      {fragmentsExpanded && (loading ? <p>{translate("common.loading", language)}</p> : error ? <p className="error-text">{error}</p> : null)}
    </details>
  </section>;
}

function KnowledgeForm({ project, view, language, onChanged, onFindSegment, explanationsExpanded, fragmentsExpanded, setExplanationsExpanded, setFragmentsExpanded }: {
  project: string; view: EntityKnowledge; language: Language;
  onChanged: (value: TermsResponse) => Promise<void>; onFindSegment: (source: string, segmentId: string) => void;
  explanationsExpanded: boolean; fragmentsExpanded: boolean;
  setExplanationsExpanded: (expanded: boolean) => void; setFragmentsExpanded: (expanded: boolean) => void;
}) {
  const [items, setItems] = useState<KnowledgeItem[]>(() => view.items.map(({ text, fragment_ids }) => ({ text, fragment_ids: [...fragment_ids] })));
  const [expandedReferences, setExpandedReferences] = useState<Set<number>>(() => new Set());
  const fragments = useMemo(() => new Map(view.fragments.map((fragment) => [fragment.id, fragment])), [view.fragments]);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");
  const t = (key: string) => translate(`knowledge.${key}`, language);
  const replace = (index: number, item: KnowledgeItem) => setItems((previous) => previous.map((value, i) => i === index ? item : value));

  function reference(item: KnowledgeItem, index: number, id: string) {
    const fragment = fragments.get(id);
    const checked = item.fragment_ids.includes(id);
    const available = fragment?.state === "active" && fragment.valid;
    return <label className="knowledge-reference checkbox-label" key={id}>
      <input type="checkbox" checked={checked} disabled={!available && !checked} onChange={(event) => replace(index, { ...item, fragment_ids: event.target.checked ? [...item.fragment_ids, id] : item.fragment_ids.filter((ref) => ref !== id) })} />
      <span>{fragment?.text ?? t("stale")}{fragment && !available && <small>{fragment.valid ? t(fragment.state) : t("expiredSource")}</small>}</span>
    </label>;
  }

  async function submit(operation: Record<string, unknown>) {
    setSaving(true);
    setError("");
    try {
      const value = await api<TermsResponse>(`/api/v1/projects/${project}/terms/knowledge`, {
        method: "PUT", body: JSON.stringify({ normalized: view.entity_key, expected_revision: view.terms_revision, ...operation }),
      });
      await onChanged(value);
    } catch (cause) {
      setError(errorMessage(cause, language));
    } finally {
      setSaving(false);
    }
  }

  return <section className="entity-knowledge" aria-label={t("title")}>
    <details className="knowledge-explanations" open={explanationsExpanded} onToggle={(event) => {
      if (event.target === event.currentTarget) {
        setExplanationsExpanded(event.currentTarget.open);
      }
    }}>
      <summary><strong>{t("title")} ({items.length})</strong><span className={view.status === "stale" ? "error-text" : "muted"}>{t(view.status)}</span></summary>
      {explanationsExpanded && <div className="knowledge-explanation-content">
    {view.members.length > 1 && <small>{t("shared")}</small>}
    {!items.length && <small>{t("noItems")}</small>}
    <div className="knowledge-items knowledge-scroll">
    {items.map((item, index) => <fieldset className="knowledge-item" key={index} disabled={saving}>
      <legend>{t("title")} {index + 1}</legend>
      <textarea aria-label={`${t("title")} ${index + 1}`} value={item.text} onChange={(event) => replace(index, { ...item, text: event.target.value })} />
      <div className="knowledge-item-actions">
        {(item.fragment_ids.length === 0 || item.fragment_ids.every((id) => fragments.get(id)?.source_range === null)) && <small>{t("noSource")}</small>}
        <button className="quiet-button" type="button" onClick={() => { replace(index, { ...item, fragment_ids: [] }); setExpandedReferences(new Set()); }}>{t("unsourced")}</button>
        <button className="danger-button" type="button" onClick={() => { setItems((previous) => previous.filter((_, i) => i !== index)); setExpandedReferences(new Set()); }}>{t("remove")}</button>
      </div>
      <details open={expandedReferences.has(index)} onToggle={(event) => {
        const open = event.currentTarget.open;
        setExpandedReferences((previous) => {
          if (previous.has(index) === open) return previous;
          const next = new Set(previous);
          if (open) next.add(index); else next.delete(index);
          return next;
        });
      }}>
        <summary>{t("references")} ({item.fragment_ids.length})</summary>
        {expandedReferences.has(index) && <div className="knowledge-reference-list">
          {view.fragments.map((fragment) => reference(item, index, fragment.id))}
          {item.fragment_ids.filter((id) => !fragments.has(id)).map((id) => reference(item, index, id))}
        </div>}
      </details>
    </fieldset>)}
    </div>
    <div className="editor-actions">
      <button className="quiet-button" type="button" disabled={saving} onClick={() => setItems((previous) => [...previous, { text: "", fragment_ids: [] }])}>{t("add")}</button>
      <button className="primary-button" type="button" disabled={saving || items.some((item) => !item.text.trim())} onClick={() => submit({ action: "save_items", items })}>{t("save")}</button>
      {view.status === "pending" && view.items.length > 0 && <button className="quiet-button" type="button" disabled={saving} onClick={() => submit({ action: "confirm_keep" })}>{t("keep")}</button>}
    </div>
      </div>}
    </details>
    <details className="knowledge-fragments" open={fragmentsExpanded} onToggle={(event) => {
      setFragmentsExpanded(event.currentTarget.open);
    }}>
      <summary>{t("fragments")} ({view.fragments.length})</summary>
      {fragmentsExpanded && <div className="knowledge-fragment-list knowledge-scroll">
      {view.fragments.map((fragment) => <article key={fragment.id}>
        <div className="knowledge-fragment-content">
          <p>{fragment.text}</p>
          {!fragment.valid && <p className="error-text">{t("expiredSource")}</p>}
          <small className="knowledge-fragment-sources">
            {fragment.source_range ? compactKnowledgeRanges(fragment).map((range, index) => <span key={range.key}>
              {index > 0 && <span aria-hidden="true"> · </span>}
              <button type="button" disabled={!fragment.valid} onClick={() => onFindSegment(fragment.source ?? view.entity_key, range.segmentId)}>{range.label}</button>
            </span>) : t("noSource")}
          </small>
        </div>
        {fragment.state !== "superseded" && <button className={fragment.state === "active" ? "danger-button" : "quiet-button"} type="button" disabled={saving} onClick={() => submit({ action: "set_fragment_enabled", fragment_id: fragment.id, enabled: fragment.state !== "active" })}>{t(fragment.state === "active" ? "disable" : "restore")}</button>}
      </article>)}
      </div>}
    </details>
    {error && <p className="error-text" role="alert">{error}</p>}
  </section>;
}
