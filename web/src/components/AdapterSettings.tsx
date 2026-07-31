import { useEffect, useState } from "react";
import { api } from "../api";

interface AdapterRow {
  adapter_id: string;
  selected: boolean;
  valid: boolean;
  digest?: string;
  error?: string;
}

export function AdapterSettings({ project }: { project: string }) {
  const [adapters, setAdapters] = useState<AdapterRow[]>([]);
  const [selected, setSelected] = useState("");
  const [content, setContent] = useState("");
  const [preview, setPreview] = useState<Record<string, unknown> | null>(null);
  const [message, setMessage] = useState("");

  async function loadAdapters() {
    const value = await api<{ adapters: AdapterRow[] }>(`/api/v1/projects/${project}/adapters`);
    setAdapters(value.adapters);
    const current = value.adapters.find((item) => item.selected) ?? value.adapters[0];
    if (current) setSelected(current.adapter_id);
  }

  useEffect(() => { void loadAdapters(); }, [project]);
  useEffect(() => {
    if (!selected) return;
    void api<Record<string, unknown>>(`/api/v1/projects/${project}/adapters/${selected}`)
      .then((value) => setContent(JSON.stringify(value, null, 2)));
    void api<Record<string, unknown>>(`/api/v1/projects/${project}/adapter-preview`)
      .then(setPreview);
  }, [project, selected]);

  async function save() {
    const value = JSON.parse(content) as Record<string, unknown>;
    await api(`/api/v1/projects/${project}/adapters/${selected}`, {
      method: "PUT",
      body: JSON.stringify(value),
    });
    setMessage("配置有效并已保存");
    await loadAdapters();
  }

  async function copyAdapter() {
    const adapterId = window.prompt("新 Adapter ID（小写字母、数字和连字符）");
    if (!adapterId) return;
    const value = JSON.parse(content) as Record<string, unknown>;
    value.adapter_id = adapterId;
    await api(`/api/v1/projects/${project}/adapters/${adapterId}`, {
      method: "PUT",
      body: JSON.stringify(value),
    });
    await loadAdapters();
    setSelected(adapterId);
    setMessage(`已复制为 ${adapterId}；可在项目配置中选择`);
  }

  return (
    <div className="settings-layout">
      <section className="settings-main">
        <div className="page-heading settings-action-heading settings-sticky-heading">
          <div><h1>LLM Adapter</h1><p>使用完整 JSON 模板构造请求并映射响应正文。</p></div>
          <div className="button-group">
            <button className="quiet-button" onClick={copyAdapter}>复制</button>
            <button className="primary-button" onClick={save}>验证并保存</button>
          </div>
        </div>
        <div className="settings-row">
          <label>编辑 Adapter
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
        <h2>占位符</h2>
        {["${model}", "${messages}", "${temperature}", "${max_output_tokens}", "${stream}"].map((item) => <code key={item}>{item}</code>)}
        <div className="info-box">API Key 只能用于请求头模板，不会写入调试 payload。</div>
        <h2>当前项目请求预览（已脱敏）</h2>
        <pre>{preview ? JSON.stringify(preview, null, 2) : "正在加载…"}</pre>
      </aside>
    </div>
  );
}
