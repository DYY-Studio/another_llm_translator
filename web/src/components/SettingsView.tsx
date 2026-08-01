import { useEffect, useId, useMemo, useRef, useState, type KeyboardEvent, type ReactNode } from "react";
import { api } from "../api";
import type { LLMPreset, LLMPresetSummary, ModelRow, ProjectConfig } from "../types";
import { AdapterSettings } from "./AdapterSettings";
import { Icon } from "./Icons";

type ContextStage = keyof ProjectConfig["context"];
type ConfigScope = "project" | "global";
type SettingsSection = "config" | "prompts" | "presets" | "adapters";

interface AdapterRow {
  adapter_id: string;
  valid?: boolean;
}

export function SettingsView({ project }: { project: string }) {
  const [scope, setScope] = useState<ConfigScope>(project ? "project" : "global");
  const [section, setSection] = useState<SettingsSection>("config");
  useEffect(() => {
    if (!project) {
      setScope("global");
      setSection("config");
    }
  }, [project]);
  const activeScope: ConfigScope = project ? scope : "global";
  const globalSections: SettingsSection[] = ["presets", "adapters"];
  useEffect(() => {
    if (activeScope === "project" && globalSections.includes(section)) {
      setSection("config");
    }
  }, [activeScope, section]);
  return (
    <div className="settings-page">
      <nav className="settings-navigation" aria-label="设置">
        <div className="settings-scope-tabs" aria-label="设置范围">
          <button disabled={!project} className={activeScope === "project" ? "active" : ""} onClick={() => setScope("project")}>项目设置</button>
          <button className={activeScope === "global" ? "active" : ""} onClick={() => setScope("global")}>全局设置</button>
        </div>
        <div className="settings-section-tabs" aria-label={`${activeScope === "project" ? "项目" : "全局"}设置类别`}>
          <button className={section === "config" ? "active" : ""} onClick={() => setSection("config")}>配置</button>
          <button className={section === "prompts" ? "active" : ""} onClick={() => setSection("prompts")}>Prompt</button>
          {activeScope === "global" && <button className={section === "presets" ? "active" : ""} onClick={() => setSection("presets")}>LLM Preset</button>}
          {activeScope === "global" && <button className={section === "adapters" ? "active" : ""} onClick={() => setSection("adapters")}>LLM Adapter</button>}
        </div>
      </nav>
      <div className="settings-content">
        {section === "config" && <ConfigSettings project={project} scope={activeScope} />}
        {section === "prompts" && <PromptSettings project={project} scope={activeScope} />}
        {section === "presets" && <PresetSettings />}
        {section === "adapters" && <AdapterSettings />}
      </div>
    </div>
  );
}

function ConfigSettings({ project, scope }: { project: string; scope: ConfigScope }) {
  const [config, setConfig] = useState<ProjectConfig | null>(null);
  const [presets, setPresets] = useState<LLMPresetSummary[]>([]);
  const [message, setMessage] = useState("");
  const [error, setError] = useState("");
  const [saving, setSaving] = useState(false);
  const configPath = scope === "global" ? "/api/v1/global/config" : `/api/v1/projects/${project}/config`;

  async function load() {
    const [configResponse, presetResponse] = await Promise.all([
      api<{ config: Record<string, unknown> }>(configPath),
      api<{ presets: LLMPresetSummary[] }>("/api/v1/global/presets"),
    ]);
    setConfig(configResponse.config as unknown as ProjectConfig);
    setPresets(presetResponse.presets);
  }

  useEffect(() => {
    let active = true;
    setError("");
    void load().catch((reason: unknown) => {
      if (active) setError(errorMessage(reason));
    });
    return () => { active = false; };
  }, [configPath, project, scope]);

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
      await api(configPath, { method: "PUT", body: JSON.stringify({ config }) });
      await load();
      setMessage(scope === "global" ? "全局模板已保存；现有项目不会自动改变" : "项目配置已验证并保存");
    } catch (reason) {
      setError(errorMessage(reason));
    } finally {
      setSaving(false);
    }
  }

  async function syncGlobal() {
    if (!project || !window.confirm("用当前全局配置和 Prompt 替换项目副本？现有副本会先备份。")) return;
    try {
      const result = await api<{ warnings: string[] }>(`/api/v1/projects/${project}/sync-templates`, {
        method: "POST",
        body: JSON.stringify({ choice: "update" }),
      });
      await load();
      setMessage(result.warnings.join("；"));
    } catch (reason) {
      setError(errorMessage(reason));
    }
  }

  if (!config) return <section className="text-settings"><p className={error ? "error-text" : "muted"}>{error || "正在加载配置…"}</p></section>;

  const presetOptions = presets.filter((item) => item.valid);
  const contextLabels: Array<[ContextStage, string]> = [["terminology", "术语"], ["translation", "翻译"], ["proofreading", "校对"], ["polishing", "润色"]];
  const stagePresetFields: Array<[ContextStage, string]> = [["terminology", "术语 Preset"], ["translation", "翻译 Preset"], ["proofreading", "校对 Preset"], ["polishing", "润色 Preset"]];
  return (
    <section className="config-settings">
      <div className="page-heading config-heading settings-action-heading">
        <div><h1>{scope === "global" ? "全局配置模板" : "项目配置"}</h1><p>{scope === "global" ? "只影响新项目或明确同步的项目。" : "Prompt 保留项目副本；连接与 Adapter 实时引用全局设置。"}</p></div>
        <div className="button-group">
          {scope === "project" && <button className="quiet-button" onClick={syncGlobal}>同步全局模板</button>}
          <button className="primary-button" disabled={saving} onClick={save}>{saving ? "保存中…" : "验证并保存"}</button>
        </div>
      </div>
      {error && <div className="error-banner" role="alert">{error}</div>}
      {message && <p className="success-text">{message}</p>}
      <div className="config-form">
        <ConfigSection title="项目与输入" description="新项目默认目标、TXT 输出和源文件编码策略。">
          <Field label="目标语言"><input value={config.project.target_language} onChange={(event) => update((draft) => { draft.project.target_language = event.target.value; })} /></Field>
          <Field label="TXT 输出编码"><input value={config.project.output_encoding} onChange={(event) => update((draft) => { draft.project.output_encoding = event.target.value; })} /></Field>
          <NumberField label="编码置信度阈值" value={config.input.encoding_confidence_threshold} min={0} max={1} step={0.05} onChange={(value) => update((draft) => { draft.input.encoding_confidence_threshold = value; })} />
          <Field label="备用输入编码"><input value={config.input.fallback_encoding} onChange={(event) => update((draft) => { draft.input.fallback_encoding = event.target.value; })} /></Field>
        </ConfigSection>
        <ConfigSection title="LLM 与采样" description="Preset 提供连接、模型、Token 能力与限速；温度仍属于项目。">
          <Field label="全局 LLM Preset"><select value={config.llm.preset} onChange={(event) => update((draft) => { draft.llm.preset = event.target.value; })}>{presetOptions.map((item) => <option key={item.preset_id} value={item.preset_id}>{item.preset_id} · {item.model}</option>)}</select></Field>
          {stagePresetFields.map(([stage, label]) => <Field label={label} help="不选择时使用全局 Preset。" key={stage}><select value={config.llm[`preset_${stage}`]} onChange={(event) => update((draft) => { draft.llm[`preset_${stage}`] = event.target.value; })}><option value="">使用全局 Preset</option>{presetOptions.map((item) => <option key={item.preset_id} value={item.preset_id}>{item.preset_id} · {item.model}</option>)}</select></Field>)}
          <NumberField label="术语温度" value={config.llm.temperature_terminology} min={0} step={0.1} onChange={(value) => update((draft) => { draft.llm.temperature_terminology = value; })} />
          <NumberField label="翻译温度" value={config.llm.temperature_translation} min={0} step={0.1} onChange={(value) => update((draft) => { draft.llm.temperature_translation = value; })} />
          <NumberField label="校对温度" value={config.llm.temperature_proofreading} min={0} step={0.1} onChange={(value) => update((draft) => { draft.llm.temperature_proofreading = value; })} />
          <NumberField label="润色温度" value={config.llm.temperature_polishing} min={0} step={0.1} onChange={(value) => update((draft) => { draft.llm.temperature_polishing = value; })} />
        </ConfigSection>
        <ConfigSection title="执行与分块" description="调度、Chunk 软目标和超长 Segment 行为。">
          <Field label="调度模式"><select value={config.execution.scheduling_mode} onChange={(event) => update((draft) => { draft.execution.scheduling_mode = event.target.value as ProjectConfig["execution"]["scheduling_mode"]; })}><option value="ordered_by_file">文件内有序</option><option value="parallel">全部并发</option></select></Field>
          <NumberField label="目标 Chunk 输入 Token" value={config.chunking.target_chunk_input_tokens} min={1} step={1} onChange={(value) => update((draft) => { draft.chunking.target_chunk_input_tokens = value; })} />
          <ToggleField label="允许拆分超长 Segment" checked={config.chunking.allow_split_oversized_segment} onChange={(value) => update((draft) => { draft.chunking.allow_split_oversized_segment = value; })} />
        </ConfigSection>
        <ConfigSection title="参考上下文" description="各阶段携带同文件前文的数量。">
          {contextLabels.map(([stage, label]) => <div className="context-config-row" key={stage}><ToggleField label={`${label}携带前文`} checked={config.context[stage].enabled} onChange={(value) => update((draft) => { draft.context[stage].enabled = value; })} /><NumberField label="前文 Segment 数" value={config.context[stage].previous_segments} min={0} step={1} onChange={(value) => update((draft) => { draft.context[stage].previous_segments = value; })} /></div>)}
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

function PresetSettings() {
  const [presets, setPresets] = useState<LLMPresetSummary[]>([]);
  const [adapters, setAdapters] = useState<AdapterRow[]>([]);
  const [selected, setSelected] = useState("");
  const [preset, setPreset] = useState<LLMPreset | null>(null);
  const [extraBody, setExtraBody] = useState("{}");
  const [preview, setPreview] = useState<Record<string, unknown> | null>(null);
  const [models, setModels] = useState<ModelRow[] | null>(null);
  const [modelsLoading, setModelsLoading] = useState(false);
  const [modelsError, setModelsError] = useState("");
  const [message, setMessage] = useState("");
  const [error, setError] = useState("");

  async function loadLists(preferred?: string | null) {
    const [presetResponse, adapterResponse] = await Promise.all([
      api<{ presets: LLMPresetSummary[] }>("/api/v1/global/presets"),
      api<{ adapters: AdapterRow[] }>("/api/v1/global/adapters"),
    ]);
    setPresets(presetResponse.presets);
    setAdapters(adapterResponse.adapters);
    const firstValid = presetResponse.presets.find((item) => item.valid)?.preset_id ?? "";
    const next = preferred === null ? firstValid : preferred || selected || firstValid;
    if (next) setSelected(next);
  }

  useEffect(() => { void loadLists().catch((reason) => setError(errorMessage(reason))); }, []);
  useEffect(() => {
    if (!selected) return;
    setError("");
    setPreset(null);
    setPreview(null);
    setModels(null);
    setModelsError("");
    void Promise.all([
      api<LLMPreset>(`/api/v1/global/presets/${selected}`),
      api<Record<string, unknown>>(`/api/v1/global/presets/${selected}/preview`),
    ]).then(([definition, requestPreview]) => {
      setPreset(definition);
      setExtraBody(JSON.stringify(definition.extra_body, null, 2));
      setPreview(requestPreview);
    }).catch((reason) => setError(errorMessage(reason)));
  }, [selected]);

  async function discoverModels() {
    if (!preset) return;
    setModels(null);
    setModelsLoading(true);
    setModelsError(""); setMessage(""); setError("");
    try {
      const definition = { ...preset, extra_body: JSON.parse(extraBody) as unknown };
      const result = await api<{ models: ModelRow[] }>(`/api/v1/global/presets/${preset.preset_id}/models`, { method: "POST", body: JSON.stringify(definition) });
      setModels(result.models);
    } catch (reason) { setModelsError(errorMessage(reason)); }
    finally { setModelsLoading(false); }
  }

  function update(change: (draft: LLMPreset) => void) {
    setMessage(""); setError("");
    setPreset((current) => { if (!current) return current; const next = structuredClone(current); change(next); return next; });
  }

  function updateConnection(change: (draft: LLMPreset) => void) {
    setModels(null);
    setModelsError("");
    update(change);
  }

  async function save() {
    if (!preset) return;
    try {
      const definition = { ...preset, extra_body: JSON.parse(extraBody) as unknown };
      await api(`/api/v1/global/presets/${preset.preset_id}`, { method: "PUT", body: JSON.stringify(definition) });
      setPreset(definition as LLMPreset);
      setPreview(await api<Record<string, unknown>>(`/api/v1/global/presets/${preset.preset_id}/preview`));
      await loadLists(preset.preset_id);
      setMessage("Preset 已验证并保存；引用项目将立即使用新内容");
    } catch (reason) { setError(errorMessage(reason)); }
  }

  async function createPreset() {
    const presetId = window.prompt("新 Preset ID（小写字母、数字和连字符）");
    if (!presetId || !preset) return;
    try {
      const definition = { ...preset, preset_id: presetId, extra_body: JSON.parse(extraBody) as unknown };
      await api(`/api/v1/global/presets/${presetId}`, { method: "PUT", body: JSON.stringify(definition) });
      await loadLists(presetId);
      setMessage(`已创建 ${presetId}`);
    } catch (reason) { setError(errorMessage(reason)); }
  }

  async function removePreset() {
    if (!preset || !window.confirm(`删除 Preset ${preset.preset_id}？`)) return;
    try {
      await api(`/api/v1/global/presets/${preset.preset_id}`, { method: "DELETE" });
      setPreset(null); setSelected(""); await loadLists(null);
    } catch (reason) { setError(errorMessage(reason)); }
  }

  return (
    <div className="preset-layout">
      <div className="page-heading preset-list-heading"><div><h1>LLM Preset</h1><p>全局实时连接设置</p></div><button className="quiet-button" disabled={!preset} onClick={createPreset}>新建</button></div>
      <aside className="preset-list-body">
        {presets.map((item) => <button key={item.preset_id} className={selected === item.preset_id ? "preset-row active" : "preset-row"} onClick={() => setSelected(item.preset_id)}><strong>{item.preset_id}</strong><small>{item.valid ? `${item.adapter_id} · ${item.model}` : item.error}</small></button>)}
      </aside>
      <div className="page-heading settings-action-heading preset-editor-heading">
        <div><h1>{preset?.preset_id ?? "Preset 编辑"}</h1><p>{preset ? "修改后会立即影响所有引用项目，并产生新的阶段指纹。" : "选择一个有效 Preset。"}</p></div>
        {preset && <div className="button-group"><button className="danger-button" onClick={removePreset}>删除</button><button className="primary-button" onClick={save}>验证并保存</button></div>}
      </div>
      <section className="preset-editor-body">
        {!preset ? (
          <>{error && <div className="error-banner">{error}</div>}<p className="muted">选择一个有效 Preset。</p></>
        ) : (
          <>
            {error && <div className="error-banner">{error}</div>}
            {message && <p className="success-text">{message}</p>}
            <div className="config-grid preset-fields"><Field label="Adapter"><select value={preset.adapter_id} onChange={(event) => updateConnection((draft) => { draft.adapter_id = event.target.value; })}>{adapters.filter((item) => item.valid !== false).map((item) => <option key={item.adapter_id}>{item.adapter_id}</option>)}</select></Field><Field label="Base URL"><input value={preset.base_url} onChange={(event) => updateConnection((draft) => { draft.base_url = event.target.value; })} /></Field><Field label="Endpoint"><input value={preset.endpoint} onChange={(event) => update((draft) => { draft.endpoint = event.target.value; })} /></Field><Field label="API Key 环境变量"><input value={preset.api_key_env} onChange={(event) => updateConnection((draft) => { draft.api_key_env = event.target.value; })} /></Field><ModelPicker value={preset.model} models={models} loading={modelsLoading} error={modelsError} onChange={(value) => update((draft) => { draft.model = value; })} onDiscover={() => void discoverModels()} onSelect={(value) => { update((draft) => { draft.model = value; }); setMessage(`已选择 ${value}；保存后生效`); }} /><Field label="代理 URL"><input value={preset.proxy_url} onChange={(event) => updateConnection((draft) => { draft.proxy_url = event.target.value; })} /></Field><NumberField label="上下文窗口 Token" value={preset.context_window_tokens} min={1} step={1} onChange={(value) => update((draft) => { draft.context_window_tokens = value; })} /><NumberField label="最大输出 Token" value={preset.max_output_tokens} min={1} step={1} onChange={(value) => update((draft) => { draft.max_output_tokens = value; })} /><NumberField label="上下文安全余量" value={preset.context_safety_margin_tokens} min={0} step={1} onChange={(value) => update((draft) => { draft.context_safety_margin_tokens = value; })} /><NumberField label="Token 安全系数" value={preset.token_safety_factor} min={0.01} step={0.05} onChange={(value) => update((draft) => { draft.token_safety_factor = value; })} /><NumberField label="RPM" value={preset.requests_per_minute} min={0} step={1} onChange={(value) => update((draft) => { draft.requests_per_minute = value; })} /><NumberField label="ITPM" value={preset.input_tokens_per_minute} min={0} step={1} onChange={(value) => update((draft) => { draft.input_tokens_per_minute = value; })} /><NumberField label="最大并发" value={preset.max_parallel} min={1} step={1} onChange={(value) => update((draft) => { draft.max_parallel = value; })} /><NumberField label="请求超时（秒）" value={preset.request_timeout_seconds} min={0.01} step={1} onChange={(value) => updateConnection((draft) => { draft.request_timeout_seconds = value; })} /><label className="code-field preset-extra"><span>附加 JSON Body</span><small>只允许 JSON 对象；不得包含模板占位符或覆盖 Adapter 顶层字段。</small><textarea spellCheck={false} value={extraBody} onChange={(event) => setExtraBody(event.target.value)} /></label></div>
            <h2 className="preview-heading">最终请求预览（Header 已脱敏）</h2>
            <pre className="result-box">{preview ? JSON.stringify(preview, null, 2) : "保存后加载预览"}</pre>
          </>
        )}
      </section>
    </div>
  );
}

function ModelPicker({ value, models, loading, error, onChange, onDiscover, onSelect }: { value: string; models: ModelRow[] | null; loading: boolean; error: string; onChange: (value: string) => void; onDiscover: () => void; onSelect: (value: string) => void }) {
  const rootRef = useRef<HTMLDivElement>(null);
  const searchRef = useRef<HTMLInputElement>(null);
  const listId = useId();
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState("");
  const filteredModels = useMemo(() => {
    const normalized = query.trim().toLocaleLowerCase();
    if (!models || !normalized) return models ?? [];
    return models.filter((item) => item.id.toLocaleLowerCase().includes(normalized) || item.display.toLocaleLowerCase().includes(normalized));
  }, [models, query]);
  const selectedIndex = filteredModels.findIndex((item) => item.id === value);
  const [activeIndex, setActiveIndex] = useState(-1);

  useEffect(() => {
    function closeOnOutsideClick(event: PointerEvent) {
      if (!rootRef.current?.contains(event.target as Node)) setOpen(false);
    }
    document.addEventListener("pointerdown", closeOnOutsideClick);
    return () => document.removeEventListener("pointerdown", closeOnOutsideClick);
  }, []);

  useEffect(() => {
    setActiveIndex(selectedIndex >= 0 ? selectedIndex : filteredModels.length > 0 ? 0 : -1);
  }, [query, models, value]);

  function openPicker() {
    setOpen(true);
    window.setTimeout(() => {
      rootRef.current?.scrollIntoView({ block: "start" });
      searchRef.current?.focus();
    }, 0);
  }

  function choose(item: ModelRow) {
    onSelect(item.id);
    setQuery("");
    setOpen(false);
  }

  function handleKeys(event: KeyboardEvent<HTMLInputElement>) {
    if (event.key === "Escape") {
      setOpen(false);
      event.preventDefault();
      return;
    }
    if (event.key === "ArrowDown" || event.key === "ArrowUp") {
      if (!open) openPicker();
      if (filteredModels.length > 0) {
        const direction = event.key === "ArrowDown" ? 1 : -1;
        setActiveIndex((current) => (current + direction + filteredModels.length) % filteredModels.length);
      }
      event.preventDefault();
      return;
    }
    if (event.key === "Enter" && open && activeIndex >= 0) {
      choose(filteredModels[activeIndex]);
      event.preventDefault();
    }
  }

  const activeOptionId = open && activeIndex >= 0 ? `${listId}-option-${activeIndex}` : undefined;
  return (
    <div className="config-field model-picker" ref={rootRef}>
      <span>模型标识</span>
      <div className="model-picker-control">
        <input value={value} role="combobox" aria-autocomplete="list" aria-expanded={open} aria-controls={listId} aria-activedescendant={activeOptionId} onKeyDown={handleKeys} onChange={(event) => onChange(event.target.value)} />
        <button type="button" className="quiet-button model-discover-button" disabled={loading} onClick={() => { setQuery(""); openPicker(); onDiscover(); }}><Icon><path d="M20 6v5h-5" /><path d="M4 18v-5h5" /><path d="M18.2 9A7 7 0 0 0 6.4 6.4L4 9" /><path d="M5.8 15A7 7 0 0 0 17.6 17.6L20 15" /></Icon>{loading ? "正在获取" : "获取模型"}</button>
      </div>
      {open && (
        <div className="model-picker-popover">
          <div className="model-search">
            <Icon><circle cx="11" cy="11" r="7" /><path d="m20 20-4-4" /></Icon>
            <input ref={searchRef} aria-label="搜索模型" placeholder="搜索模型名称或 ID" value={query} onKeyDown={handleKeys} onChange={(event) => setQuery(event.target.value)} />
          </div>
          <div className="model-options" id={listId} role="listbox" aria-label="可用模型">
            {loading && <div className="model-picker-state" role="status">正在从当前 Preset 草稿获取模型…</div>}
            {!loading && error && <div className="model-picker-state error-text" role="alert">{error}</div>}
            {!loading && !error && models !== null && filteredModels.length === 0 && <div className="model-picker-state">{models.length === 0 ? "端点返回空模型列表" : "没有匹配的模型"}</div>}
            {!loading && !error && models === null && <div className="model-picker-state">点击“获取模型”检查当前草稿连接。</div>}
            {!loading && !error && filteredModels.map((item, index) => (
              <button type="button" key={item.id} id={`${listId}-option-${index}`} role="option" aria-selected={item.id === value} className={`model-option${index === activeIndex ? " active" : ""}${item.id === value ? " selected" : ""}`} onMouseEnter={() => setActiveIndex(index)} onClick={() => choose(item)}>
                <span><strong>{item.display}</strong><code>{item.id}</code></span>
                {item.id === value && <Icon><path d="m5 12 4 4L19 6" /></Icon>}
              </button>
            ))}
          </div>
          <div className="model-picker-footer">共 {models?.length ?? 0} 个模型 · 选择后仍需保存</div>
        </div>
      )}
    </div>
  );
}

function ConfigSection({ title, description, warning = false, children }: { title: string; description: string; warning?: boolean; children: ReactNode }) { return <fieldset className={`config-section${warning ? " warning" : ""}`}><legend>{title}</legend><p>{description}</p><div className="config-grid">{children}</div></fieldset>; }
function Field({ label, help, children }: { label: string; help?: string; children: ReactNode }) { return <label className="config-field"><span>{label}</span>{children}{help && <small>{help}</small>}</label>; }
function NumberField({ label, value, onChange, help, min, max, step }: { label: string; value: number; onChange: (value: number) => void; help?: string; min?: number; max?: number; step: number }) { return <Field label={label} help={help}><input type="number" value={value} min={min} max={max} step={step} onChange={(event) => { if (event.target.value !== "") onChange(event.target.valueAsNumber); }} /></Field>; }
function ToggleField({ label, checked, onChange, help, disabled = false }: { label: string; checked: boolean; onChange: (value: boolean) => void; help?: string; disabled?: boolean }) { return <label className="config-toggle"><span><input type="checkbox" checked={checked} disabled={disabled} onChange={(event) => onChange(event.target.checked)} />{label}</span>{help && <small>{help}</small>}</label>; }

function PromptSettings({ project, scope }: { project: string; scope: ConfigScope }) {
  const [stage, setStage] = useState("translation"); const [content, setContent] = useState(""); const [message, setMessage] = useState(""); const [error, setError] = useState("");
  const path = scope === "global" ? `/api/v1/global/prompts/${stage}` : `/api/v1/projects/${project}/prompts/${stage}`;
  useEffect(() => { setMessage(""); setError(""); void api<{ content: string }>(path).then((value) => setContent(value.content)).catch((reason) => setError(errorMessage(reason))); }, [path]);
  async function save() { try { await api(path, { method: "PUT", body: JSON.stringify({ content }) }); setMessage(scope === "global" ? "全局 Prompt 已保存；现有项目不会自动改变" : "项目 Prompt 已保存"); } catch (reason) { setError(errorMessage(reason)); } }
  return <section className="text-settings"><div className="page-heading config-heading settings-action-heading"><div><h1>{scope === "global" ? "全局 Prompt 模板" : "项目 Prompt"}</h1><p>{scope === "global" ? "只影响新项目或明确同步的项目。" : "编辑项目内的阶段 Prompt 副本。"}</p></div><button className="primary-button" onClick={save}>验证并保存</button></div><label className="stage-select">阶段<select value={stage} onChange={(event) => setStage(event.target.value)}><option value="terminology">术语</option><option value="translation">翻译</option><option value="proofreading">校对</option><option value="polishing">润色</option></select></label>{error && <div className="error-banner">{error}</div>}<span className="success-text">{message}</span><textarea className="settings-editor" spellCheck={false} value={content} onChange={(event) => setContent(event.target.value)} /></section>;
}

function errorMessage(reason: unknown): string { return reason instanceof Error ? reason.message : "请求失败"; }
