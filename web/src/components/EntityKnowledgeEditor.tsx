import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { api } from "../api";
import { compactKnowledgeRanges } from "../termKnowledge";
import { errorMessage, translate, type Language } from "../i18n";
import type { EntityKnowledge, KnowledgeItem, TermsResponse } from "../types";

export function EntityKnowledgeEditor({ project, projectId, normalized, revision, language, onChanged, onFindSegment }: {
  project: string; projectId: string; normalized: string; revision: number; language: Language;
  onChanged: (value: TermsResponse) => Promise<void>; onFindSegment: (source: string, segmentId: string) => void;
}) {
  const query = useQuery({
    queryKey: ["entity-knowledge", projectId, normalized, revision],
    queryFn: ({ signal }) => api<EntityKnowledge>(`/api/v1/projects/${project}/terms/knowledge?normalized=${encodeURIComponent(normalized)}`, { signal }),
    retry: false,
  });
  if (query.isPending) return <p>{translate("common.loading", language)}</p>;
  if (query.error) return <p className="error-text">{errorMessage(query.error, language)}</p>;
  return <KnowledgeForm key={`${query.data.entity_key}:${query.data.terms_revision}`} project={project} view={query.data} language={language} onChanged={onChanged} onFindSegment={onFindSegment} />;
}

function KnowledgeForm({ project, view, language, onChanged, onFindSegment }: {
  project: string; view: EntityKnowledge; language: Language;
  onChanged: (value: TermsResponse) => Promise<void>; onFindSegment: (source: string, segmentId: string) => void;
}) {
  const [items, setItems] = useState<KnowledgeItem[]>(() => view.items.map(({ text, fragment_ids }) => ({ text, fragment_ids: [...fragment_ids] })));
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");
  const t = (key: string) => translate(`knowledge.${key}`, language);
  const replace = (index: number, item: KnowledgeItem) => setItems((previous) => previous.map((value, i) => i === index ? item : value));

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
    <header><strong>{t("title")}</strong><span className={view.status === "stale" ? "error-text" : "muted"}>{t(view.status)}</span></header>
    {view.members.length > 1 && <small>{t("shared")}</small>}
    {!items.length && <small>{t("noItems")}</small>}
    {items.map((item, index) => <fieldset className="knowledge-item" key={index} disabled={saving}>
      <legend>{t("title")} {index + 1}</legend>
      <textarea aria-label={`${t("title")} ${index + 1}`} value={item.text} onChange={(event) => replace(index, { ...item, text: event.target.value })} />
      <div><button className="quiet-button" type="button" onClick={() => replace(index, { ...item, fragment_ids: [] })}>{t("unsourced")}</button>{(item.fragment_ids.length === 0 || item.fragment_ids.every((id) => view.fragments.find((f) => f.id === id)?.source_range === null)) && <small>{t("noSource")}</small>}</div>
      <details>
        <summary>{t("references")} ({item.fragment_ids.length})</summary>
        {view.fragments.map((fragment) => {
          const checked = item.fragment_ids.includes(fragment.id);
          const available = fragment.state === "active" && fragment.valid;
          return <label className="knowledge-reference checkbox-label" key={fragment.id}>
            <input type="checkbox" checked={checked} disabled={!available && !checked} onChange={(event) => replace(index, { ...item, fragment_ids: event.target.checked ? [...item.fragment_ids, fragment.id] : item.fragment_ids.filter((id) => id !== fragment.id) })} />
            <span>{fragment.text}{!available && <small>{fragment.valid ? t(fragment.state) : t("expiredSource")}</small>}</span>
          </label>;
        })}
        {item.fragment_ids.filter((id) => !view.fragments.some((fragment) => fragment.id === id)).map((id) => <label className="checkbox-label" key={id}>
          <input type="checkbox" checked onChange={() => replace(index, { ...item, fragment_ids: item.fragment_ids.filter((ref) => ref !== id) })} />{t("stale")}
        </label>)}
      </details>
      <button className="quiet-button" type="button" onClick={() => setItems((previous) => previous.filter((_, i) => i !== index))}>{t("remove")}</button>
    </fieldset>)}
    <div className="editor-actions">
      <button className="quiet-button" type="button" disabled={saving} onClick={() => setItems((previous) => [...previous, { text: "", fragment_ids: [] }])}>{t("add")}</button>
      <button className="primary-button" type="button" disabled={saving || items.some((item) => !item.text.trim())} onClick={() => submit({ action: "save_items", items })}>{t("save")}</button>
      {view.status === "pending" && view.items.length > 0 && <button className="quiet-button" type="button" disabled={saving} onClick={() => submit({ action: "confirm_keep" })}>{t("keep")}</button>}
    </div>
    <details className="knowledge-fragments">
      <summary>{t("fragments")} ({view.fragments.length})</summary>
      {view.fragments.map((fragment) => <article key={fragment.id}>
        <div className="knowledge-fragment-content">
          <p>{fragment.text}</p>
          <small>{fragment.source ?? fragment.original_owner} / {t(fragment.state)}</small>
          {!fragment.valid && <p className="error-text">{t("expiredSource")}</p>}
          <div className="knowledge-fragment-sources">
            {fragment.source_range ? compactKnowledgeRanges(fragment).map((range) => <button className="quiet-button" type="button" key={range.key} disabled={!fragment.valid} onClick={() => onFindSegment(fragment.source ?? view.entity_key, range.segmentId)}>{t("openSource")}: {range.label}</button>) : <small>{t("noSource")}</small>}
          </div>
        </div>
        {fragment.state !== "superseded" && <button className={fragment.state === "active" ? "danger-button" : "quiet-button"} type="button" disabled={saving} onClick={() => submit({ action: "set_fragment_enabled", fragment_id: fragment.id, enabled: fragment.state !== "active" })}>{t(fragment.state === "active" ? "disable" : "restore")}</button>}
      </article>)}
    </details>
    {error && <p className="error-text" role="alert">{error}</p>}
  </section>;
}
