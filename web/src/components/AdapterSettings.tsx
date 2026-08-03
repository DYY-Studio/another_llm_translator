import { useEffect, useState } from "react";
import { api } from "../api";
import type { Language } from "../i18n";

interface AdapterRow {
  adapter_id: string;
  valid: boolean;
  digest?: string;
  error?: string;
}

export function AdapterSettings({ language }: { language: Language }) {
  const en = language === "en";
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

  useEffect(() => { void loadAdapters().catch((reason) => setError(errorMessage(reason, en))); }, [en]);
  useEffect(() => {
    if (!selected) return;
    setError("");
    void api<Record<string, unknown>>(`/api/v1/global/adapters/${selected}`)
      .then((value) => setContent(JSON.stringify(value, null, 2)));
    void api<Record<string, unknown>>(`/api/v1/global/adapters/${selected}/preview`)
      .then(setPreview)
      .catch((reason) => setError(errorMessage(reason, en)));
  }, [selected]);

  async function save() {
    const value = JSON.parse(content) as Record<string, unknown>;
    const result = await api<{ digest: string }>(`/api/v1/global/adapters/${selected}`, {
      method: "PUT",
      body: JSON.stringify(value),
    });
    setMessage(en ? "Configuration validated and saved; referenced Presets use it immediately" : "配置有效并已保存；所有引用 Preset 立即使用新内容");
    setPreview(await api<Record<string, unknown>>(`/api/v1/global/adapters/${selected}/preview`));
    await loadAdapters();
    void result;
  }

  async function copyAdapter() {
    const adapterId = window.prompt(en ? "New Adapter ID (lowercase letters, numbers, and hyphens)" : "新 Adapter ID（小写字母、数字和连字符）");
    if (!adapterId) return;
    const value = JSON.parse(content) as Record<string, unknown>;
    value.adapter_id = adapterId;
    await api(`/api/v1/global/adapters/${adapterId}`, {
      method: "PUT",
      body: JSON.stringify(value),
    });
    await loadAdapters();
    setSelected(adapterId);
    setMessage(en ? `Copied as ${adapterId}; it can now be selected in a Preset` : `已复制为 ${adapterId}；可在 Preset 中选择`);
  }

  return (
    <div className="settings-layout">
      <section className="settings-main">
        <div className="page-heading settings-action-heading settings-sticky-heading">
          <div><h1>LLM Adapter</h1><p>{en ? "Global request templates; projects reference them without copies." : "全局请求模板；项目直接引用，不再保存副本。"}</p></div>
          <div className="button-group">
            <button className="quiet-button" onClick={copyAdapter}>{en ? "Duplicate" : "复制"}</button>
            <button className="primary-button" onClick={save}>{en ? "Validate and save" : "验证并保存"}</button>
          </div>
        </div>
        {error && <div className="error-banner">{error}</div>}
        <div className="settings-row">
          <label>{en ? "Edit Adapter" : "编辑 Adapter"}
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
        <h2>{en ? "Placeholders" : "占位符"}</h2>
        {["${model}", "${system}", "${messages}", "${temperature}", "${max_output_tokens}", "${stream}"].map((item) => <code key={item}>{item}</code>)}
        <div className="info-box">{en ? "API keys are only used by request-header templates and never written to debug payloads. The URL and extra_body come from the Preset." : "API Key 只能用于请求头模板，不会写入调试 payload。URL 与 extra_body 由 Preset 决定。"}</div>
        <h2>{en ? "Rendered Adapter preview (redacted)" : "Adapter 模板渲染预览（已脱敏）"}</h2>
        <pre>{preview ? JSON.stringify(preview, null, 2) : (en ? "Loading…" : "正在加载…")}</pre>
      </aside>
    </div>
  );
}

function errorMessage(reason: unknown, en: boolean): string { return reason instanceof Error ? reason.message : (en ? "Request failed" : "请求失败"); }
