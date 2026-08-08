import { useEffect, useState } from "react";
import { api } from "../api";
import { errorMessage, translate, type Language } from "../i18n";

interface AdapterRow {
  adapter_id: string;
  valid: boolean;
  digest?: string;
  error?: string;
}

export function AdapterSettings({ language }: { language: Language }) {
  const [adapters, setAdapters] = useState<AdapterRow[]>([]);
  const [selected, setSelected] = useState("");
  const [content, setContent] = useState("");
  const [preview, setPreview] = useState<Record<string, unknown> | null>(null);
  const [message, setMessage] = useState("");
  const [error, setError] = useState("");

  async function loadAdapters() {
    const value = await api<{ adapters: AdapterRow[] }>("/api/v1/global/adapters");
    setAdapters(value.adapters);
    if (value.adapters.length > 0 && !value.adapters.some((item) => item.adapter_id === selected)) {
      setSelected(value.adapters[0].adapter_id);
    }
  }

  useEffect(() => { void loadAdapters().catch((reason) => setError(errorMessage(reason, language))); }, [language]);
  useEffect(() => {
    if (!selected) return;
    setError("");
    void api<Record<string, unknown>>(`/api/v1/global/adapters/${selected}`)
      .then((value) => setContent(JSON.stringify(value, null, 2)));
    void api<Record<string, unknown>>(`/api/v1/global/adapters/${selected}/preview`)
      .then(setPreview)
      .catch((reason) => setError(errorMessage(reason, language)));
  }, [selected, language]);

  async function save() {
    const value = JSON.parse(content) as Record<string, unknown>;
    const result = await api<{ digest: string }>(`/api/v1/global/adapters/${selected}`, {
      method: "PUT",
      body: JSON.stringify(value),
    });
    setMessage(translate("adapter.saved", language));
    setPreview(await api<Record<string, unknown>>(`/api/v1/global/adapters/${selected}/preview`));
    await loadAdapters();
    void result;
  }

  async function copyAdapter() {
    const adapterId = window.prompt(translate("adapter.newId", language));
    if (!adapterId) return;
    const value = JSON.parse(content) as Record<string, unknown>;
    value.adapter_id = adapterId;
    await api(`/api/v1/global/adapters/${adapterId}`, {
      method: "PUT",
      body: JSON.stringify(value),
    });
    await loadAdapters();
    setSelected(adapterId);
    setMessage(translate("adapter.copied", language, { id: adapterId }));
  }

  return (
    <div className="settings-layout">
      <section className="settings-main">
        <div className="page-heading settings-action-heading settings-sticky-heading">
          <div><h1>{translate("adapter.title", language)}</h1><p>{translate("adapter.description", language)}</p></div>
          <div className="button-group">
            <button className="quiet-button" onClick={copyAdapter}>{translate("adapter.duplicate", language)}</button>
            <button className="primary-button" onClick={save}>{translate("common.validateSave", language)}</button>
          </div>
        </div>
        {error && <div className="error-banner">{error}</div>}
        <div className="settings-row">
          <label>{translate("adapter.edit", language)}
            <select value={selected} onChange={(event) => setSelected(event.target.value)}>
              {adapters.map((item) => <option key={item.adapter_id}>{item.adapter_id}</option>)}
            </select>
          </label>
          <span className="success-text">{message}</span>
        </div>
        <label className="code-field">
          <span>Adapter JSON</span>
          <textarea spellCheck={false} value={content} onChange={(event) => setContent(event.target.value)} />
        </label>
      </section>
      <aside className="reference-rail">
        <h2>{translate("adapter.placeholders", language)}</h2>
        {["${model}", "${system}", "${messages}", "${temperature}", "${max_output_tokens}", "${stream}"].map((item) => <code key={item}>{item}</code>)}
        <div className="info-box">{translate("adapter.info", language)}</div>
        <h2>{translate("adapter.preview", language)}</h2>
        <pre>{preview ? JSON.stringify(preview, null, 2) : translate("common.loading", language)}</pre>
      </aside>
    </div>
  );
}
