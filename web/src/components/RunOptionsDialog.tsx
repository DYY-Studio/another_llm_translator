import { useEffect, useMemo, useState } from "react";
import { api } from "../api";
import { errorMessage, type Language } from "../i18n";
import type { AdapterSummary } from "./ProjectInputs";

type FileValue = { file_id: string; name: string; values: Record<string, string> };
type SingleResponse = { adapter: AdapterSummary; file_id: string; values: Record<string, string> };
type BulkResponse = { adapter_id: string; files: FileValue[] };

export function RunOptionsDialog({ project, fileId, adapterId, language, onClose, onSaved }: {
  project: string; fileId?: string; adapterId?: string; language: Language;
  onClose: () => void; onSaved: () => Promise<void>;
}) {
  const [adapter, setAdapter] = useState<AdapterSummary | null>(null);
  const [files, setFiles] = useState<FileValue[]>([]);
  const [draft, setDraft] = useState<Record<string, string>>({});
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");
  const bulk = Boolean(adapterId);

  useEffect(() => {
    let active = true;
    const options = api<{ adapters: AdapterSummary[] }>("/api/v1/document-adapters");
    const values = fileId
      ? api<SingleResponse>(`/api/v1/projects/${project}/files/${fileId}/run-options`)
      : api<BulkResponse>(`/api/v1/projects/${project}/document-adapters/${adapterId}/run-options`);
    void Promise.all([options, values]).then(([catalog, value]) => {
      if (!active) return;
      const id = fileId ? (value as SingleResponse).adapter.adapter_id : adapterId!;
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
  }, [adapterId, fileId, language, project]);

  const changed = useMemo(() => Object.entries(draft).some(([id, value]) => value && files.some((file) => file.values[id] !== value)), [draft, files]);
  async function save() {
    const options = Object.fromEntries(Object.entries(draft).filter(([, value]) => value));
    if (!Object.keys(options).length) return;
    setSaving(true); setError("");
    try {
      await api(fileId
        ? `/api/v1/projects/${project}/files/${fileId}/run-options`
        : `/api/v1/projects/${project}/document-adapters/${adapterId}/run-options`,
      { method: "PUT", body: JSON.stringify({ options }) });
      await onSaved(); onClose();
    } catch (reason) { setError(errorMessage(reason, language)); }
    finally { setSaving(false); }
  }
  function defaults() {
    setDraft(Object.fromEntries((adapter?.run_options ?? []).map((option) => [option.option_id, option.default])));
  }
  return <div className="modal-backdrop" onMouseDown={() => !saving && onClose()}>
    <div className="modal run-options-modal" role="dialog" aria-modal="true" aria-label="运行格式设置" onMouseDown={(event) => event.stopPropagation()}>
      <h2>{bulk ? "统一运行格式设置" : "运行格式设置"}</h2>
      {loading ? <p>正在读取设置…</p> : <>
        {bulk && <><p>将应用于 {files.length} 个当前文件；“多个值”保持每个文件原值。</p><div className="run-options-files">{files.map((file) => <span key={file.file_id}>{file.file_id} · {file.name}</span>)}</div></>}
        {(adapter?.run_options ?? []).map((option) => <label key={option.option_id}>{option.label}
          <select value={draft[option.option_id] ?? ""} disabled={saving} onChange={(event) => setDraft({ ...draft, [option.option_id]: event.target.value })}>
            {bulk && <option value="">多个值（保持不变）</option>}
            {option.choices.map((choice) => <option key={choice.value} value={choice.value}>{choice.label}</option>)}
          </select>
        </label>)}
        {bulk && <button type="button" className="quiet-button" disabled={saving} onClick={defaults}>全部恢复 Adapter 默认</button>}
      </>}
      {error && <p className="error-banner">{error}</p>}
      <div className="modal-actions"><button className="quiet-button" disabled={saving} onClick={onClose}>取消</button><button className="primary-button" disabled={loading || saving || !changed} onClick={() => void save()}>{saving ? "应用中…" : "应用"}</button></div>
    </div>
  </div>;
}
