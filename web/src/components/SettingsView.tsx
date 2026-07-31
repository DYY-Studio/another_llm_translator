import { useEffect, useState, type ReactNode } from "react";
import { api } from "../api";
import type { ProjectConfig } from "../types";
import { AdapterSettings } from "./AdapterSettings";

type SettingsTab = "adapter" | "config" | "prompts";
type ContextStage = keyof ProjectConfig["context"];

interface AdapterRow {
  adapter_id: string;
}

export function SettingsView({ project }: { project: string }) {
  const [tab, setTab] = useState<SettingsTab>("adapter");
  return (
    <div className="settings-page">
      <div className="settings-tabs" aria-label="项目设置">
        <button className={tab === "adapter" ? "active" : ""} onClick={() => setTab("adapter")}>LLM Adapter</button>
        <button className={tab === "config" ? "active" : ""} onClick={() => setTab("config")}>项目配置</button>
        <button className={tab === "prompts" ? "active" : ""} onClick={() => setTab("prompts")}>Prompt</button>
      </div>
      {tab === "adapter" && <AdapterSettings project={project} />}
      {tab === "config" && <ProjectConfigSettings project={project} />}
      {tab === "prompts" && <PromptSettings project={project} />}
    </div>
  );
}

function ProjectConfigSettings({ project }: { project: string }) {
  const [config, setConfig] = useState<ProjectConfig | null>(null);
  const [adapters, setAdapters] = useState<AdapterRow[]>([]);
  const [message, setMessage] = useState("");
  const [error, setError] = useState("");
  const [saving, setSaving] = useState(false);

  useEffect(() => {
    let active = true;
    setError("");
    void Promise.all([
      api<{ config: ProjectConfig }>(`/api/v1/projects/${project}/config`),
      api<{ adapters: AdapterRow[] }>(`/api/v1/projects/${project}/adapters`),
    ]).then(([configResponse, adapterResponse]) => {
      if (!active) return;
      setConfig(configResponse.config);
      setAdapters(adapterResponse.adapters);
    }).catch((reason: unknown) => {
      if (active) setError(errorMessage(reason));
    });
    return () => { active = false; };
  }, [project]);

  function update(change: (draft: ProjectConfig) => void) {
    setMessage("");
    setError("");
    setConfig((current) => {
      if (!current) return current;
      const next = structuredClone(current);
      change(next);
      return next;
    });
  }

  async function save() {
    if (!config) return;
    setSaving(true);
    setMessage("");
    setError("");
    try {
      await api(`/api/v1/projects/${project}/config`, {
        method: "PUT",
        body: JSON.stringify({ config }),
      });
      const response = await api<{ config: ProjectConfig }>(`/api/v1/projects/${project}/config`);
      setConfig(response.config);
      setMessage("已验证并保存");
    } catch (reason) {
      setError(errorMessage(reason));
    } finally {
      setSaving(false);
    }
  }

  if (!config) {
    return <section className="text-settings"><p className={error ? "error-text" : "muted"}>{error || "正在加载项目配置…"}</p></section>;
  }

  const contextLabels: Array<[ContextStage, string]> = [
    ["terminology", "术语"],
    ["translation", "翻译"],
    ["proofreading", "校对"],
    ["polishing", "润色"],
  ];

  return (
    <section className="config-settings">
      <div className="page-heading config-heading">
        <div>
          <h1>项目配置</h1>
          <p>按用途修改当前项目设置；保存时会整体校验并写入规范 TOML。</p>
        </div>
        <button className="primary-button" disabled={saving} onClick={save}>{saving ? "保存中…" : "验证并保存"}</button>
      </div>
      {error && <div className="error-banner" role="alert">{error}</div>}
      {message && <p className="success-text">{message}</p>}

      <div className="config-form">
        <ConfigSection title="项目与输入" description="目标语言、TXT 输出和源文件编码策略。">
          <Field label="目标语言"><input value={config.project.target_language} onChange={(event) => update((draft) => { draft.project.target_language = event.target.value; })} /></Field>
          <Field label="TXT 输出编码"><input value={config.project.output_encoding} onChange={(event) => update((draft) => { draft.project.output_encoding = event.target.value; })} /></Field>
          <NumberField label="编码置信度阈值" value={config.input.encoding_confidence_threshold} min={0} max={1} step={0.05} onChange={(value) => update((draft) => { draft.input.encoding_confidence_threshold = value; })} />
          <Field label="备用输入编码"><input value={config.input.fallback_encoding} onChange={(event) => update((draft) => { draft.input.fallback_encoding = event.target.value; })} /></Field>
        </ConfigSection>

        <ConfigSection title="LLM 连接" description="四个阶段当前共用的 Adapter、端点和模型。API Key 只填写环境变量名。">
          <Field label="LLM Adapter">
            <select value={config.llm.adapter} onChange={(event) => update((draft) => { draft.llm.adapter = event.target.value; })}>
              {adapters.map((item) => <option key={item.adapter_id} value={item.adapter_id}>{item.adapter_id}</option>)}
            </select>
          </Field>
          <Field label="Base URL"><input value={config.llm.base_url} onChange={(event) => update((draft) => { draft.llm.base_url = event.target.value; })} /></Field>
          <Field label="Endpoint"><input value={config.llm.endpoint} onChange={(event) => update((draft) => { draft.llm.endpoint = event.target.value; })} /></Field>
          <Field label="模型标识"><input value={config.llm.model} onChange={(event) => update((draft) => { draft.llm.model = event.target.value; })} /></Field>
          <Field label="API Key 环境变量"><input value={config.llm.api_key_env} onChange={(event) => update((draft) => { draft.llm.api_key_env = event.target.value; })} /></Field>
          <Field label="代理 URL" help="留空时使用 HTTPX 默认环境变量。"><input placeholder="http://127.0.0.1:7890" value={config.llm.proxy_url} onChange={(event) => update((draft) => { draft.llm.proxy_url = event.target.value; })} /></Field>
        </ConfigSection>

        <ConfigSection title="模型与采样" description="温度、输出上限和上下文窗口。">
          <NumberField label="术语温度" value={config.llm.temperature_terminology} min={0} step={0.1} onChange={(value) => update((draft) => { draft.llm.temperature_terminology = value; })} />
          <NumberField label="翻译温度" value={config.llm.temperature_translation} min={0} step={0.1} onChange={(value) => update((draft) => { draft.llm.temperature_translation = value; })} />
          <NumberField label="校对温度" value={config.llm.temperature_proofreading} min={0} step={0.1} onChange={(value) => update((draft) => { draft.llm.temperature_proofreading = value; })} />
          <NumberField label="润色温度" value={config.llm.temperature_polishing} min={0} step={0.1} onChange={(value) => update((draft) => { draft.llm.temperature_polishing = value; })} />
          <NumberField label="最大输出 Token" value={config.llm.max_output_tokens} min={1} step={1} onChange={(value) => update((draft) => { draft.llm.max_output_tokens = value; })} />
          <NumberField label="上下文窗口 Token" value={config.llm.context_window_tokens} min={1} step={1} onChange={(value) => update((draft) => { draft.llm.context_window_tokens = value; })} />
          <NumberField label="上下文安全余量" value={config.llm.context_safety_margin_tokens} min={0} step={1} onChange={(value) => update((draft) => { draft.llm.context_safety_margin_tokens = value; })} />
        </ConfigSection>

        <ConfigSection title="执行与分块" description="并发、限速、Token 估算和超长 Segment 行为。">
          <NumberField label="最大并发请求" value={config.execution.max_parallel} min={1} step={1} onChange={(value) => update((draft) => { draft.execution.max_parallel = value; })} />
          <NumberField label="每分钟请求数（RPM）" value={config.execution.requests_per_minute} min={0} step={1} help="0 表示禁用 RPM 限速。" onChange={(value) => update((draft) => { draft.execution.requests_per_minute = value; })} />
          <NumberField label="每分钟输入 Token（ITPM）" value={config.execution.input_tokens_per_minute} min={0} step={1} help="0 表示禁用 ITPM 限速。" onChange={(value) => update((draft) => { draft.execution.input_tokens_per_minute = value; })} />
          <NumberField label="请求超时（秒）" value={config.execution.request_timeout_seconds} min={0.01} step={1} onChange={(value) => update((draft) => { draft.execution.request_timeout_seconds = value; })} />
          <Field label="调度模式"><select value={config.execution.scheduling_mode} onChange={(event) => update((draft) => { draft.execution.scheduling_mode = event.target.value as ProjectConfig["execution"]["scheduling_mode"]; })}><option value="ordered_by_file">文件内有序</option><option value="parallel">全部并发</option></select></Field>
          <NumberField label="Token 安全系数" value={config.execution.token_safety_factor} min={0.01} step={0.05} help="小于 1 可提高利用率，但增加上下文或 ITPM 超限风险。" onChange={(value) => update((draft) => { draft.execution.token_safety_factor = value; })} />
          <NumberField label="目标 Chunk 输入 Token" value={config.chunking.target_chunk_input_tokens} min={1} step={1} onChange={(value) => update((draft) => { draft.chunking.target_chunk_input_tokens = value; })} />
          <ToggleField label="允许拆分超长 Segment" checked={config.chunking.allow_split_oversized_segment} onChange={(value) => update((draft) => { draft.chunking.allow_split_oversized_segment = value; })} />
        </ConfigSection>

        <ConfigSection title="参考上下文" description="各阶段携带同文件前文的数量。">
          {contextLabels.map(([stage, label]) => (
            <div className="context-config-row" key={stage}>
              <ToggleField label={`${label}携带前文`} checked={config.context[stage].enabled} onChange={(value) => update((draft) => { draft.context[stage].enabled = value; })} />
              <NumberField label="前文 Segment 数" value={config.context[stage].previous_segments} min={0} step={1} onChange={(value) => update((draft) => { draft.context[stage].previous_segments = value; })} />
            </div>
          ))}
        </ConfigSection>

        <ConfigSection title="术语" description="术语匹配与注入规则。">
          <Field label="Unicode 归一化" help="当前行为固定为 NFKC。"><input disabled value={config.terminology.unicode_normalization} /></Field>
          <ToggleField label="忽略大小写" checked={config.terminology.case_insensitive} disabled help="当前行为固定启用 casefold。" onChange={() => undefined} />
          <NumberField label="每个 Segment 最大术语数" value={config.terminology.max_terms_per_segment} min={1} step={1} onChange={(value) => update((draft) => { draft.terminology.max_terms_per_segment = value; })} />
          <Field label="别名与主条目冲突"><select value={config.terminology.alias_primary_collision} onChange={(event) => update((draft) => { draft.terminology.alias_primary_collision = event.target.value as ProjectConfig["terminology"]["alias_primary_collision"]; })}><option value="conflict">要求人工裁决</option><option value="merge">确定性合并</option></select></Field>
        </ConfigSection>

        <ConfigSection title="翻译校验与重试" description="文字检查、HTTP/格式重试及退避。">
          <ToggleField label="检查日语 Kana 残留" checked={config.validation.translation.japanese_kana} onChange={(value) => update((draft) => { draft.validation.translation.japanese_kana = value; })} />
          <ToggleField label="检查韩语 Hangul 残留" checked={config.validation.translation.korean_hangul} onChange={(value) => update((draft) => { draft.validation.translation.korean_hangul = value; })} />
          <NumberField label="文字校验修复次数" value={config.validation.translation.max_retry_attempts} min={0} step={1} onChange={(value) => update((draft) => { draft.validation.translation.max_retry_attempts = value; })} />
          <Field label="校验耗尽行为"><select value={config.validation.translation.exhausted_mode} onChange={(event) => update((draft) => { draft.validation.translation.exhausted_mode = event.target.value as ProjectConfig["validation"]["translation"]["exhausted_mode"]; })}><option value="fail">标记失败</option><option value="warning">接受并警告</option></select></Field>
          <NumberField label="HTTP 总尝试上限" value={config.retry.http_max_attempts} min={1} step={1} onChange={(value) => update((draft) => { draft.retry.http_max_attempts = value; })} />
          <NumberField label="格式修正次数" value={config.retry.format_max_attempts} min={0} step={1} onChange={(value) => update((draft) => { draft.retry.format_max_attempts = value; })} />
          <NumberField label="退避初始秒数" value={config.retry.base_delay_seconds} min={0} step={0.1} onChange={(value) => update((draft) => { draft.retry.base_delay_seconds = value; })} />
          <NumberField label="退避最大秒数" value={config.retry.max_delay_seconds} min={0} step={0.1} onChange={(value) => update((draft) => { draft.retry.max_delay_seconds = value; })} />
          <NumberField label="随机抖动秒数" value={config.retry.jitter_seconds} min={0} step={0.1} onChange={(value) => update((draft) => { draft.retry.jitter_seconds = value; })} />
        </ConfigSection>

        <ConfigSection title="调试与故障注入" description="仅用于本地确定性调试；普通运行应保持关闭。" warning>
          <ToggleField label="启用调试与故障注入" checked={config.debug.enabled} onChange={(value) => update((draft) => { draft.debug.enabled = value; })} />
          <NumberField label="每 N 次注入 HTTP 429" value={config.debug.inject_429_every} min={0} step={1} onChange={(value) => update((draft) => { draft.debug.inject_429_every = value; })} />
          <NumberField label="每 N 次注入 HTTP 500" value={config.debug.inject_500_every} min={0} step={1} onChange={(value) => update((draft) => { draft.debug.inject_500_every = value; })} />
          <NumberField label="每 N 次注入超时" value={config.debug.inject_timeout_every} min={0} step={1} onChange={(value) => update((draft) => { draft.debug.inject_timeout_every = value; })} />
          <NumberField label="每 N 次注入非法 JSON" value={config.debug.inject_invalid_json_every} min={0} step={1} onChange={(value) => update((draft) => { draft.debug.inject_invalid_json_every = value; })} />
          <NumberField label="每 N 次移除 Segment" value={config.debug.inject_missing_segment_every} min={0} step={1} onChange={(value) => update((draft) => { draft.debug.inject_missing_segment_every = value; })} />
        </ConfigSection>
      </div>
    </section>
  );
}

function ConfigSection({ title, description, warning = false, children }: { title: string; description: string; warning?: boolean; children: ReactNode }) {
  return (
    <fieldset className={`config-section${warning ? " warning" : ""}`}>
      <legend>{title}</legend>
      <p>{description}</p>
      <div className="config-grid">{children}</div>
    </fieldset>
  );
}

function Field({ label, help, children }: { label: string; help?: string; children: ReactNode }) {
  return <label className="config-field"><span>{label}</span>{children}{help && <small>{help}</small>}</label>;
}

function NumberField({ label, value, onChange, help, min, max, step }: { label: string; value: number; onChange: (value: number) => void; help?: string; min?: number; max?: number; step: number }) {
  return (
    <Field label={label} help={help}>
      <input type="number" value={value} min={min} max={max} step={step} onChange={(event) => {
        if (event.target.value !== "") onChange(event.target.valueAsNumber);
      }} />
    </Field>
  );
}

function ToggleField({ label, checked, onChange, help, disabled = false }: { label: string; checked: boolean; onChange: (value: boolean) => void; help?: string; disabled?: boolean }) {
  return (
    <label className="config-toggle">
      <span><input type="checkbox" checked={checked} disabled={disabled} onChange={(event) => onChange(event.target.checked)} />{label}</span>
      {help && <small>{help}</small>}
    </label>
  );
}

function PromptSettings({ project }: { project: string }) {
  const [stage, setStage] = useState("translation");
  const [content, setContent] = useState("");
  const [message, setMessage] = useState("");
  const path = `/api/v1/projects/${project}/prompts/${stage}`;

  useEffect(() => {
    setMessage("");
    void api<{ content: string }>(path).then((value) => setContent(value.content));
  }, [path]);

  async function save() {
    await api(path, { method: "PUT", body: JSON.stringify({ content }) });
    setMessage("已验证并保存");
  }

  return (
    <section className="text-settings">
      <div className="page-heading">
        <div><h1>Prompt</h1><p>编辑项目内的阶段 Prompt 副本。</p></div>
        <button className="primary-button" onClick={save}>验证并保存</button>
      </div>
      <label className="stage-select">阶段
        <select value={stage} onChange={(event) => setStage(event.target.value)}>
          <option value="terminology">术语</option><option value="translation">翻译</option><option value="proofreading">校对</option><option value="polishing">润色</option>
        </select>
      </label>
      <span className="success-text">{message}</span>
      <textarea className="settings-editor" spellCheck={false} value={content} onChange={(event) => setContent(event.target.value)} />
    </section>
  );
}

function errorMessage(reason: unknown): string {
  return reason instanceof Error ? reason.message : "请求失败";
}
