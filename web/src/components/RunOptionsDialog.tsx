import { useEffect, useMemo, useState } from "react";
import { api } from "../api";
import { errorMessage, translate, type Language } from "../i18n";
import type { AdapterSummary } from "./ProjectInputs";

type FileValue = { file_id: string; name: string; values: Record<string, string> };
type SingleResponse = { adapter: AdapterSummary; file_id: string; values: Record<string, string> };
type BulkResponse = { adapter_id: string; files: FileValue[] };

export function RunOptionsDialog({ project, fileId, bulkAdapterIds, language, onClose, onSaved }: {
  project: string; fileId?: string; bulkAdapterIds?: string[]; language: Language;
  onClose: () => void; onSaved: () => Promise<void>;
}) {
  const [adapter, setAdapter] = useState<AdapterSummary | null>(null);
  const [files, setFiles] = useState<FileValue[]>([]);
  const [draft, setDraft] = useState<Record<string, string>>({});
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");
  const bulk = Boolean(bulkAdapterIds);
  const [selectedAdapterId, setSelectedAdapterId] = useState(bulkAdapterIds?.[0] ?? "");

  useEffect(() => {
    if (bulk && !bulkAdapterIds?.includes(selectedAdapterId)) {
      setSelectedAdapterId(bulkAdapterIds?.[0] ?? "");
    }
  }, [bulk, bulkAdapterIds, selectedAdapterId]);

  useEffect(() => {
    if (bulk && !selectedAdapterId) {
      setLoading(false);
      return;
    }
    let active = true;
    setLoading(true);
    setError("");
    const options = api<{ adapters: AdapterSummary[] }>("/api/v1/document-adapters");
    const values = fileId
      ? api<SingleResponse>(`/api/v1/projects/${project}/files/${fileId}/run-options`)
      : api<BulkResponse>(`/api/v1/projects/${project}/document-adapters/${selectedAdapterId}/run-options`);
    void Promise.all([options, values]).then(([catalog, value]) => {
      if (!active) return;
      const id = fileId ? (value as SingleResponse).adapter.adapter_id : selectedAdapterId;
      const descriptor = catalog.adapters.find((item) => item.adapter_id === id) ?? (fileId ? (value as SingleResponse).adapter : null);
      setAdapter(descriptor);
      const loaded = fileId
        ? [{ file_id: (value as SingleResponse).file_id, name: fileId, values: (value as SingleResponse).values }]
        : (value as BulkResponse).files;
      setFiles(loaded);
      const next: Record<string, string> = {};
      for (const option of descriptor?.run_options ?? []) {
        const values = [...new Set(loaded.map((item) => item.values[option.option_id]))];
        next[option.option_id] = values.length === 1 ? values[0] : "";
      }
      setDraft(next);
    }).catch((reason) => { if (active) setError(errorMessage(reason, language)); })
      .finally(() => { if (active) setLoading(false); });
    return () => { active = false; };
  }, [bulk, fileId, language, project, selectedAdapterId]);

  const changed = useMemo(() => Object.entries(draft).some(([id, value]) => value && files.some((file) => file.values[id] !== value)), [draft, files]);
  async function save() {
    const options = Object.fromEntries(Object.entries(draft).filter(([, value]) => value));
    if (!Object.keys(options).length) return;
    setSaving(true); setError("");
    try {
      await api(fileId
        ? `/api/v1/projects/${project}/files/${fileId}/run-options`
        : `/api/v1/projects/${project}/document-adapters/${selectedAdapterId}/run-options`,
      { method: "PUT", body: JSON.stringify({ options }) });
      await onSaved(); onClose();
    } catch (reason) { setError(errorMessage(reason, language)); }
    finally { setSaving(false); }
  }
  function defaults() {
    setDraft(Object.fromEntries((adapter?.run_options ?? []).map((option) => [option.option_id, option.default])));
  }
  return <div className="modal-backdrop" onMouseDown={() => !saving && onClose()}>
    <div className="modal run-options-modal" role="dialog" aria-modal="true" aria-label={translate("runOptions.title", language)} onMouseDown={(event) => event.stopPropagation()}>
      <div className="run-options-content">
        <h2>{bulk ? translate("runOptions.bulkTitle", language) : translate("runOptions.title", language)}</h2>
        {bulk && <label>{translate("runOptions.adapter", language)}
          <select value={selectedAdapterId} disabled={saving} onChange={(event) => setSelectedAdapterId(event.target.value)}>
            {(bulkAdapterIds ?? []).map((id) => <option key={id} value={id}>{id.toUpperCase()}</option>)}
          </select>
        </label>}
        {loading ? <p>{translate("runOptions.loading", language)}</p> : <>
          {bulk && <><p>{translate("runOptions.applyHint", language, { count: files.length })}</p><div className="run-options-files">{files.map((file) => <span key={file.file_id}>{file.file_id} · {file.name}</span>)}</div></>}
          {(adapter?.run_options ?? []).map((option) => <label key={option.option_id}>{option.label}
            <select value={draft[option.option_id] ?? ""} disabled={saving} onChange={(event) => setDraft({ ...draft, [option.option_id]: event.target.value })}>
              {bulk && <option value="">{translate("runOptions.multipleValues", language)}</option>}
              {option.choices.map((choice) => <option key={choice.value} value={choice.value}>{choice.label}</option>)}
            </select>
          </label>)}
          {bulk && <button type="button" className="quiet-button" disabled={saving} onClick={defaults}>{translate("runOptions.restoreDefaults", language)}</button>}
        </>}
        {error && <p className="error-banner">{error}</p>}
      </div>
      <div className="modal-actions"><button className="quiet-button" disabled={saving} onClick={onClose}>{translate("common.cancel", language)}</button><button className="primary-button" disabled={loading || saving || !changed} onClick={() => void save()}>{saving ? translate("runOptions.applying", language) : translate("runOptions.apply", language)}</button></div>
    </div>
  </div>;
}
