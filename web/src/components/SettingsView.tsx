import { useEffect, useId, useMemo, useRef, useState, type KeyboardEvent, type ReactNode } from "react";
import { api } from "../api";
import type { Language } from "../i18n";
import type { LLMStage, LLMPreset, LLMPresetSummary, ModelRow, ProjectConfig } from "../types";
import { AdapterSettings } from "./AdapterSettings";
import { Icon } from "./Icons";

type ContextStage = keyof ProjectConfig["context"];
type ConfigScope = "project" | "global";
type SettingsSection = "config" | "prompts" | "presets" | "adapters";

interface AdapterRow {
  adapter_id: string;
  valid?: boolean;
}

export function SettingsView({ project, language }: { project: string; language: Language }) {
  const en = language === "en";
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
      <nav className="settings-navigation" aria-label={en ? "Settings" : "设置"}>
        <div className="settings-scope-tabs" aria-label={en ? "Settings scope" : "设置范围"}>
          <button disabled={!project} className={activeScope === "project" ? "active" : ""} onClick={() => setScope("project")}>{en ? "Project" : "项目设置"}</button>
          <button className={activeScope === "global" ? "active" : ""} onClick={() => setScope("global")}>{en ? "Global" : "全局设置"}</button>
        </div>
        <div className="settings-section-tabs" aria-label={`${activeScope === "project" ? (en ? "Project" : "项目") : (en ? "Global" : "全局")} ${en ? "settings sections" : "设置类别"}`}>
          <button className={section === "config" ? "active" : ""} onClick={() => setSection("config")}>{en ? "Config" : "配置"}</button>
          <button className={section === "prompts" ? "active" : ""} onClick={() => setSection("prompts")}>Prompt</button>
          {activeScope === "global" && <button className={section === "presets" ? "active" : ""} onClick={() => setSection("presets")}>LLM Preset</button>}
          {activeScope === "global" && <button className={section === "adapters" ? "active" : ""} onClick={() => setSection("adapters")}>LLM Adapter</button>}
        </div>
      </nav>
      <div className="settings-content">
        {section === "config" && <ConfigSettings project={project} scope={activeScope} language={language} />}
        {section === "prompts" && <PromptSettings project={project} scope={activeScope} language={language} />}
        {section === "presets" && <PresetSettings language={language} />}
        {section === "adapters" && <AdapterSettings language={language} />}
      </div>
    </div>
  );
}

function ConfigSettings({ project, scope, language }: { project: string; scope: ConfigScope; language: Language }) {
  const en = language === "en";
  const [config, setConfig] = useState<ProjectConfig | null>(null);
  const [presets, setPresets] = useState<LLMPresetSummary[]>([]);
  const [message, setMessage] = useState("");
  const [error, setError] = useState("");
  const [saving, setSaving] = useState(false);
  const loadRevision = useRef(0);
  const configPath = scope === "global" ? "/api/v1/global/config" : `/api/v1/projects/${project}/config`;

  async function load() {
    const revision = ++loadRevision.current;
    const [configResponse, presetResponse] = await Promise.all([
      api<{ config: Record<string, unknown> }>(configPath),
      api<{ presets: LLMPresetSummary[] }>("/api/v1/global/presets"),
    ]);
    if (revision !== loadRevision.current) return;
    setConfig(configResponse.config as unknown as ProjectConfig);
    setPresets(presetResponse.presets);
  }

  useEffect(() => {
    let active = true;
    setConfig(null);
    setMessage("");
    setError("");
    void load().catch((reason: unknown) => {
      if (active) setError(errorMessage(reason));
    });
    return () => { active = false; loadRevision.current += 1; };
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
      setMessage(scope === "global" ? localized(language, "全局模板已保存；现有项目不会自动改变", "Global template saved; existing projects are unchanged") : localized(language, "项目配置已验证并保存", "Project configuration validated and saved"));
    } catch (reason) {
      setError(errorMessage(reason));
    } finally {
      setSaving(false);
    }
  }

  async function syncGlobal() {
    if (!project || !window.confirm(localized(language, "用当前全局配置和 Prompt 替换项目副本？现有副本会先备份。", "Replace the project copies with the current global config and Prompts? Existing copies will be backed up first."))) return;
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

  if (!config) return <section className="text-settings"><p className={error ? "error-text" : "muted"}>{error || (en ? "Loading configuration…" : "正在加载配置…")}</p></section>;

  const presetOptions = presets.filter((item) => item.valid);
  const contextLabels: Array<[ContextStage, string]> = [
    ["terminology", localized(language, "术语", "Terms")],
    ["translation", localized(language, "翻译", "Translation")],
    ["proofreading", localized(language, "校对", "Proofreading")],
    ["polishing", localized(language, "润色", "Polishing")],
  ];
  const stagePresetFields: Array<[ContextStage, string]> = [
    ["terminology", localized(language, "术语 Preset", "Terms Preset")],
    ["translation", localized(language, "翻译 Preset", "Translation Preset")],
    ["proofreading", localized(language, "校对 Preset", "Proofreading Preset")],
    ["polishing", localized(language, "润色 Preset", "Polishing Preset")],
  ];
  const crossBoundaryStages: Array<[LLMStage, string]> = [
    ["terminology", localized(language, "术语", "Terms")],
    ["translation", localized(language, "翻译", "Translation")],
    ["proofreading", localized(language, "校对", "Proofreading")],
    ["polishing", localized(language, "润色", "Polishing")],
  ];
  return (
    <section className="config-settings">
      <div className="page-heading config-heading settings-action-heading">
        <div><h1>{scope === "global" ? (en ? "Global configuration template" : "全局配置模板") : (en ? "Project configuration" : "项目配置")}</h1><p>{scope === "global" ? (en ? "Affects new projects or projects explicitly synchronized." : "只影响新项目或明确同步的项目。") : (en ? "Prompts stay in the project; connections and adapters use global settings." : "Prompt 保留项目副本；连接与 Adapter 实时引用全局设置。")}</p></div>
        <div className="button-group">
          {scope === "project" && <button className="quiet-button" onClick={syncGlobal}>{en ? "Sync global template" : "同步全局模板"}</button>}
          <button className="primary-button" disabled={saving} onClick={save}>{saving ? (en ? "Saving…" : "保存中…") : (en ? "Validate and save" : "验证并保存")}</button>
        </div>
      </div>
      {error && <div className="error-banner" role="alert">{error}</div>}
      {message && <p className="success-text">{message}</p>}
      <div className="config-form">
        <ConfigSection title={localized(language, "项目与输入", "Project and input")} description={localized(language, "新项目默认目标、TXT 输出和源文件编码策略。", "Defaults for new projects, TXT output, and source encoding.")}>
          <Field label={localized(language, "目标语言", "Target language")}><input value={config.project.target_language} onChange={(event) => update((draft) => { draft.project.target_language = event.target.value; })} /></Field>
          <Field label={localized(language, "TXT 输出编码", "TXT output encoding")}><input value={config.project.output_encoding} onChange={(event) => update((draft) => { draft.project.output_encoding = event.target.value; })} /></Field>
          <NumberField label={localized(language, "编码置信度阈值", "Encoding confidence threshold")} value={config.input.encoding_confidence_threshold} min={0} max={1} step={0.05} onChange={(value) => update((draft) => { draft.input.encoding_confidence_threshold = value; })} />
          <Field label={localized(language, "备用输入编码", "Fallback input encoding")}><input value={config.input.fallback_encoding} onChange={(event) => update((draft) => { draft.input.fallback_encoding = event.target.value; })} /></Field>
        </ConfigSection>
        <ConfigSection title={localized(language, "LLM 与采样", "LLM and sampling")} description={localized(language, "Preset 提供连接、模型、Token 能力与限速；温度仍属于项目。", "Presets provide connections, models, token limits, and rate limits; temperature remains project-specific.")}>
          <Field label={localized(language, "全局 LLM Preset", "Global LLM Preset")}><select value={config.llm.preset} onChange={(event) => update((draft) => { draft.llm.preset = event.target.value; })}>{presetOptions.map((item) => <option key={item.preset_id} value={item.preset_id}>{item.preset_id} · {item.model}</option>)}</select></Field>
          {stagePresetFields.map(([stage, label]) => <Field label={label} help={localized(language, "不选择时使用全局 Preset。", "Uses the global Preset when empty.")} key={stage}><select value={config.llm[`preset_${stage}`]} onChange={(event) => update((draft) => { draft.llm[`preset_${stage}`] = event.target.value; })}><option value="">{localized(language, "使用全局 Preset", "Use global Preset")}</option>{presetOptions.map((item) => <option key={item.preset_id} value={item.preset_id}>{item.preset_id} · {item.model}</option>)}</select></Field>)}
          <NumberField label={localized(language, "术语温度", "Terms temperature")} value={config.llm.temperature_terminology} min={0} step={0.1} onChange={(value) => update((draft) => { draft.llm.temperature_terminology = value; })} />
          <NumberField label={localized(language, "翻译温度", "Translation temperature")} value={config.llm.temperature_translation} min={0} step={0.1} onChange={(value) => update((draft) => { draft.llm.temperature_translation = value; })} />
          <NumberField label={localized(language, "校对温度", "Proofreading temperature")} value={config.llm.temperature_proofreading} min={0} step={0.1} onChange={(value) => update((draft) => { draft.llm.temperature_proofreading = value; })} />
          <NumberField label={localized(language, "润色温度", "Polishing temperature")} value={config.llm.temperature_polishing} min={0} step={0.1} onChange={(value) => update((draft) => { draft.llm.temperature_polishing = value; })} />
        </ConfigSection>
        <ConfigSection title={localized(language, "执行与分块", "Execution and chunks")} description={localized(language, "调度、Chunk 软目标、超长 Segment 行为，以及按阶段启用的跨 File / EPUB spine part 合并。", "Scheduling, soft Chunk targets, oversized Segment handling, and optional cross-file / EPUB spine-part batching by stage.")}>
          <Field label={localized(language, "调度模式", "Scheduling mode")}><select value={config.execution.scheduling_mode} onChange={(event) => update((draft) => { draft.execution.scheduling_mode = event.target.value as ProjectConfig["execution"]["scheduling_mode"]; })}><option value="ordered_by_file">{localized(language, "文件内有序", "Ordered within file")}</option><option value="parallel">{localized(language, "全部并发", "Parallel")}</option></select></Field>
          <NumberField label={localized(language, "目标 Chunk 输入 Token", "Target Chunk input tokens")} value={config.chunking.target_chunk_input_tokens} min={1} step={1} onChange={(value) => update((draft) => { draft.chunking.target_chunk_input_tokens = value; })} />
          <ToggleField label={localized(language, "允许拆分超长 Segment", "Allow oversized Segment splitting")} checked={config.chunking.allow_split_oversized_segment} onChange={(value) => update((draft) => { draft.chunking.allow_split_oversized_segment = value; })} />
          {crossBoundaryStages.map(([stage, label]) => <ToggleField key={stage} label={localized(language, `${label}阶段允许跨边界合并`, `Allow ${label} cross-boundary batching`)} checked={config.chunking.cross_boundary_batching.includes(stage)} help={localized(language, "仅按源文顺序合并允许的边界；参考上文仍以 Chunk 首段的 File / part 为准。", "Only permitted source-order boundaries are merged; reference context still uses the first Segment's File / part.")} onChange={(value) => update((draft) => { const selected = new Set(draft.chunking.cross_boundary_batching); if (value) selected.add(stage); else selected.delete(stage); draft.chunking.cross_boundary_batching = crossBoundaryStages.map(([candidate]) => candidate).filter((candidate) => selected.has(candidate)); })} />)}
        </ConfigSection>
        <ConfigSection title={localized(language, "参考上下文", "Reference context")} description={localized(language, "各阶段携带同文件前文的数量。", "How many preceding Segments each stage can reference within a file.")}>
          {contextLabels.map(([stage, label]) => <div className="context-config-row" key={stage}><ToggleField label={localized(language, `${label}携带前文`, `${label} context`)} checked={config.context[stage].enabled} onChange={(value) => update((draft) => { draft.context[stage].enabled = value; })} /><NumberField label={localized(language, "前文 Segment 数", "Previous Segment count")} value={config.context[stage].previous_segments} min={0} step={1} onChange={(value) => update((draft) => { draft.context[stage].previous_segments = value; })} /></div>)}
        </ConfigSection>
        <ConfigSection title={localized(language, "术语", "Terminology")} description={localized(language, "术语匹配与注入规则。", "Term matching and injection rules.")}>
          <Field label={localized(language, "Unicode 归一化", "Unicode normalization")} help={localized(language, "当前行为固定为 NFKC。", "Fixed to NFKC.")}><input disabled value={config.terminology.unicode_normalization} /></Field>
          <ToggleField label={localized(language, "忽略大小写", "Case-insensitive matching")} checked={config.terminology.case_insensitive} disabled help={localized(language, "当前行为固定启用 casefold。", "casefold is always enabled.")} onChange={() => undefined} />
          <NumberField label={localized(language, "每个 Segment 最大术语数", "Maximum terms per Segment")} value={config.terminology.max_terms_per_segment} min={1} step={1} onChange={(value) => update((draft) => { draft.terminology.max_terms_per_segment = value; })} />
          <Field label={localized(language, "别名与主条目冲突", "Alias / primary collision")}><select value={config.terminology.alias_primary_collision} onChange={(event) => update((draft) => { draft.terminology.alias_primary_collision = event.target.value as ProjectConfig["terminology"]["alias_primary_collision"]; })}><option value="conflict">{localized(language, "要求人工裁决", "Require review")}</option><option value="merge">{localized(language, "确定性合并", "Deterministic merge")}</option></select></Field>
        </ConfigSection>
        <ConfigSection title={localized(language, "翻译校验与重试", "Translation validation and retries")} description={localized(language, "文字检查、HTTP/格式重试及退避。", "Text checks, HTTP / format retries, and backoff.")}>
          <ToggleField label={localized(language, "检查日语 Kana 残留", "Check residual Japanese Kana")} checked={config.validation.translation.japanese_kana} onChange={(value) => update((draft) => { draft.validation.translation.japanese_kana = value; })} />
          <ToggleField label={localized(language, "检查韩语 Hangul 残留", "Check residual Korean Hangul")} checked={config.validation.translation.korean_hangul} onChange={(value) => update((draft) => { draft.validation.translation.korean_hangul = value; })} />
          <NumberField label={localized(language, "文字校验修复次数", "Text validation repair attempts")} value={config.validation.translation.max_retry_attempts} min={0} step={1} onChange={(value) => update((draft) => { draft.validation.translation.max_retry_attempts = value; })} />
          <Field label={localized(language, "校验耗尽行为", "When validation is exhausted")}><select value={config.validation.translation.exhausted_mode} onChange={(event) => update((draft) => { draft.validation.translation.exhausted_mode = event.target.value as ProjectConfig["validation"]["translation"]["exhausted_mode"]; })}><option value="fail">{localized(language, "标记失败", "Mark failed")}</option><option value="warning">{localized(language, "接受并警告", "Accept with warning")}</option></select></Field>
          <NumberField label={localized(language, "HTTP 总尝试上限", "Maximum HTTP attempts")} value={config.retry.http_max_attempts} min={1} step={1} onChange={(value) => update((draft) => { draft.retry.http_max_attempts = value; })} />
          <NumberField label={localized(language, "格式修正次数", "Format repair attempts")} value={config.retry.format_max_attempts} min={0} step={1} onChange={(value) => update((draft) => { draft.retry.format_max_attempts = value; })} />
          <NumberField label={localized(language, "退避初始秒数", "Initial backoff seconds")} value={config.retry.base_delay_seconds} min={0} step={0.1} onChange={(value) => update((draft) => { draft.retry.base_delay_seconds = value; })} />
          <NumberField label={localized(language, "退避最大秒数", "Maximum backoff seconds")} value={config.retry.max_delay_seconds} min={0} step={0.1} onChange={(value) => update((draft) => { draft.retry.max_delay_seconds = value; })} />
          <NumberField label={localized(language, "随机抖动秒数", "Jitter seconds")} value={config.retry.jitter_seconds} min={0} step={0.1} onChange={(value) => update((draft) => { draft.retry.jitter_seconds = value; })} />
        </ConfigSection>
        <ConfigSection title={localized(language, "调试与故障注入", "Debug and fault injection")} description={localized(language, "仅用于本地确定性调试；普通运行应保持关闭。", "For deterministic local debugging only; keep disabled for normal runs.")} warning>
          <ToggleField label={localized(language, "启用调试与故障注入", "Enable debug and fault injection")} checked={config.debug.enabled} onChange={(value) => update((draft) => { draft.debug.enabled = value; })} />
          <NumberField label={localized(language, "每 N 次注入 HTTP 429", "Inject HTTP 429 every N requests")} value={config.debug.inject_429_every} min={0} step={1} onChange={(value) => update((draft) => { draft.debug.inject_429_every = value; })} />
          <NumberField label={localized(language, "每 N 次注入 HTTP 500", "Inject HTTP 500 every N requests")} value={config.debug.inject_500_every} min={0} step={1} onChange={(value) => update((draft) => { draft.debug.inject_500_every = value; })} />
          <NumberField label={localized(language, "每 N 次注入超时", "Inject timeout every N requests")} value={config.debug.inject_timeout_every} min={0} step={1} onChange={(value) => update((draft) => { draft.debug.inject_timeout_every = value; })} />
          <NumberField label={localized(language, "每 N 次注入非法 JSON", "Inject invalid JSON every N requests")} value={config.debug.inject_invalid_json_every} min={0} step={1} onChange={(value) => update((draft) => { draft.debug.inject_invalid_json_every = value; })} />
          <NumberField label={localized(language, "每 N 次移除 Segment", "Remove a Segment every N requests")} value={config.debug.inject_missing_segment_every} min={0} step={1} onChange={(value) => update((draft) => { draft.debug.inject_missing_segment_every = value; })} />
        </ConfigSection>
      </div>
    </section>
  );
}

function PresetSettings({ language }: { language: Language }) {
  const en = language === "en";
  const [presets, setPresets] = useState<LLMPresetSummary[]>([]);
  const [adapters, setAdapters] = useState<AdapterRow[]>([]);
  const [selected, setSelected] = useState("");
  const [preset, setPreset] = useState<LLMPreset | null>(null);
  const [presetLoading, setPresetLoading] = useState(false);
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
    let active = true;
    setError("");
    setPreset(null);
    setPresetLoading(true);
    setPreview(null);
    setModels(null);
    setModelsError("");
    void Promise.all([
      api<LLMPreset>(`/api/v1/global/presets/${selected}`),
      api<Record<string, unknown>>(`/api/v1/global/presets/${selected}/preview`),
    ]).then(([definition, requestPreview]) => {
      if (!active) return;
      setPreset(definition);
      setExtraBody(JSON.stringify(definition.extra_body, null, 2));
      setPreview(requestPreview);
    }).catch((reason) => {
      if (active) setError(errorMessage(reason));
    }).finally(() => {
      if (active) setPresetLoading(false);
    });
    return () => { active = false; };
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
      setMessage(en ? "Preset validated and saved; referenced projects use it immediately" : "Preset 已验证并保存；引用项目将立即使用新内容");
    } catch (reason) { setError(errorMessage(reason)); }
  }

  async function createPreset() {
    const presetId = window.prompt(en ? "New Preset ID (lowercase letters, numbers, and hyphens)" : "新 Preset ID（小写字母、数字和连字符）");
    if (!presetId || !preset) return;
    try {
      const definition = { ...preset, preset_id: presetId, extra_body: JSON.parse(extraBody) as unknown };
      await api(`/api/v1/global/presets/${presetId}`, { method: "PUT", body: JSON.stringify(definition) });
      await loadLists(presetId);
      setMessage(en ? `Created ${presetId}` : `已创建 ${presetId}`);
    } catch (reason) { setError(errorMessage(reason)); }
  }

  async function removePreset() {
    if (!preset || !window.confirm(en ? `Delete Preset ${preset.preset_id}?` : `删除 Preset ${preset.preset_id}？`)) return;
    try {
      await api(`/api/v1/global/presets/${preset.preset_id}`, { method: "DELETE" });
      setPreset(null); setSelected(""); await loadLists(null);
    } catch (reason) { setError(errorMessage(reason)); }
  }

  return (
    <div className="preset-layout">
      <div className="page-heading preset-list-heading"><div><h1>LLM Preset</h1><p>{en ? "Global live connection settings" : "全局实时连接设置"}</p></div><button className="quiet-button" disabled={!preset} onClick={createPreset}>{en ? "New" : "新建"}</button></div>
      <aside className="preset-list-body">
        {presets.map((item) => <button key={item.preset_id} className={selected === item.preset_id ? "preset-row active" : "preset-row"} onClick={() => setSelected(item.preset_id)}><strong>{item.preset_id}</strong><small>{item.valid ? `${item.adapter_id} · ${item.model}` : item.error}</small></button>)}
      </aside>
      <div className="page-heading settings-action-heading preset-editor-heading">
        <div><h1>{preset?.preset_id ?? (presetLoading ? (en ? `Loading ${selected}` : `正在加载 ${selected}`) : (en ? "Preset editor" : "Preset 编辑器"))}</h1><p>{preset ? (en ? "Changes affect all referenced projects and create a new stage fingerprint." : "修改后会立即影响所有引用项目，并产生新的阶段指纹。") : presetLoading ? (en ? "Reading the Preset definition and request preview." : "正在读取 Preset 定义和请求预览。") : (en ? "Select a valid Preset." : "选择一个有效 Preset。")} </p></div>
        {preset && <div className="button-group"><button className="danger-button" onClick={removePreset}>{en ? "Delete" : "删除"}</button><button className="primary-button" onClick={save}>{en ? "Validate and save" : "验证并保存"}</button></div>}
      </div>
      <section className="preset-editor-body">
        {!preset ? (
          <>{error && <div className="error-banner">{error}</div>}<p className="muted">{presetLoading ? (en ? "Loading Preset…" : "正在加载 Preset…") : (en ? "Select a valid Preset." : "选择一个有效 Preset。")}</p></>
        ) : (
          <>
            {error && <div className="error-banner">{error}</div>}
            {message && <p className="success-text">{message}</p>}
            <div className="config-grid preset-fields">
              <Field label="Adapter"><select value={preset.adapter_id} onChange={(event) => updateConnection((draft) => { draft.adapter_id = event.target.value; })}>{adapters.filter((item) => item.valid !== false).map((item) => <option key={item.adapter_id}>{item.adapter_id}</option>)}</select></Field>
              <Field label="Base URL"><input value={preset.base_url} onChange={(event) => updateConnection((draft) => { draft.base_url = event.target.value; })} /></Field>
              <Field label="Endpoint"><input value={preset.endpoint} onChange={(event) => update((draft) => { draft.endpoint = event.target.value; })} /></Field>
              <Field label={localized(language, "API Key 环境变量", "API key environment variable")}><input value={preset.api_key_env} onChange={(event) => updateConnection((draft) => { draft.api_key_env = event.target.value; })} /></Field>
              <ModelPicker language={language} value={preset.model} models={models} loading={modelsLoading} error={modelsError} onChange={(value) => update((draft) => { draft.model = value; })} onDiscover={() => void discoverModels()} onSelect={(value) => { update((draft) => { draft.model = value; }); setMessage(en ? `Selected ${value}; save to apply` : `已选择 ${value}；保存后生效`); }} />
              <Field label={localized(language, "代理 URL", "Proxy URL")}><input value={preset.proxy_url} onChange={(event) => updateConnection((draft) => { draft.proxy_url = event.target.value; })} /></Field>
              <NumberField label={localized(language, "上下文窗口 Token", "Context window tokens")} value={preset.context_window_tokens} min={1} step={1} onChange={(value) => update((draft) => { draft.context_window_tokens = value; })} />
              <NumberField label={localized(language, "最大输出 Token", "Maximum output tokens")} value={preset.max_output_tokens} min={1} step={1} onChange={(value) => update((draft) => { draft.max_output_tokens = value; })} />
              <NumberField label={localized(language, "上下文安全余量", "Context safety margin")} value={preset.context_safety_margin_tokens} min={0} step={1} onChange={(value) => update((draft) => { draft.context_safety_margin_tokens = value; })} />
              <NumberField label={localized(language, "Token 安全系数", "Token safety factor")} value={preset.token_safety_factor} min={0.01} step={0.05} onChange={(value) => update((draft) => { draft.token_safety_factor = value; })} />
              <NumberField label="RPM" value={preset.requests_per_minute} min={0} step={1} help={localized(language, "0 表示禁用；启用后按 60 / RPM 平滑安排所有实际尝试。", "0 disables it; enabled RPM paces every actual attempt at 60 / RPM seconds.")} onChange={(value) => update((draft) => { draft.requests_per_minute = value; })} />
              <NumberField label="ITPM" value={preset.input_tokens_per_minute} min={0} step={1} help={localized(language, "0 表示禁用；启用后使用 60 秒滑动窗口。", "0 disables it; enabled ITPM uses a 60-second sliding window.")} onChange={(value) => update((draft) => { draft.input_tokens_per_minute = value; })} />
              <NumberField label={localized(language, "最大并发", "Maximum concurrency")} value={preset.max_parallel} min={1} step={1} help={localized(language, "必须至少为 1；即使 RPM 和 ITPM 都关闭仍生效。", "Must be at least 1 and remains active when RPM and ITPM are disabled.")} onChange={(value) => update((draft) => { draft.max_parallel = value; })} />
              <NumberField label={localized(language, "请求超时（秒）", "Request timeout (seconds)")} value={preset.request_timeout_seconds} min={0.01} step={1} onChange={(value) => updateConnection((draft) => { draft.request_timeout_seconds = value; })} />
              <label className="code-field preset-extra"><span>{localized(language, "附加 JSON Body", "Extra JSON body")}</span><small>{localized(language, "只允许 JSON 对象；不得包含模板占位符或覆盖 Adapter 顶层字段。", "JSON object only; no template placeholders or Adapter top-level overrides.")}</small><textarea spellCheck={false} value={extraBody} onChange={(event) => setExtraBody(event.target.value)} /></label>
            </div>
            <h2 className="preview-heading">{localized(language, "最终请求预览（Header 已脱敏）", "Final request preview (headers redacted)")}</h2>
            <pre className="result-box">{preview ? JSON.stringify(preview, null, 2) : localized(language, "保存后加载预览", "Save to load preview")}</pre>
          </>
        )}
      </section>
    </div>
  );
}

function ModelPicker({ language, value, models, loading, error, onChange, onDiscover, onSelect }: { language: Language; value: string; models: ModelRow[] | null; loading: boolean; error: string; onChange: (value: string) => void; onDiscover: () => void; onSelect: (value: string) => void }) {
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
      <span>{localized(language, "模型标识", "Model ID")}</span>
      <div className="model-picker-control">
        <input value={value} role="combobox" aria-autocomplete="list" aria-expanded={open} aria-controls={listId} aria-activedescendant={activeOptionId} onKeyDown={handleKeys} onChange={(event) => onChange(event.target.value)} />
        <button type="button" className="quiet-button model-discover-button" disabled={loading} onClick={() => { setQuery(""); openPicker(); onDiscover(); }}><Icon><path d="M20 6v5h-5" /><path d="M4 18v-5h5" /><path d="M18.2 9A7 7 0 0 0 6.4 6.4L4 9" /><path d="M5.8 15A7 7 0 0 0 17.6 17.6L20 15" /></Icon>{loading ? localized(language, "正在获取", "Loading") : localized(language, "获取模型", "Discover models")}</button>
      </div>
      {open && (
        <div className="model-picker-popover">
          <div className="model-search">
            <Icon><circle cx="11" cy="11" r="7" /><path d="m20 20-4-4" /></Icon>
            <input ref={searchRef} aria-label={localized(language, "搜索模型", "Search models")} placeholder={localized(language, "搜索模型名称或 ID", "Search model name or ID")} value={query} onKeyDown={handleKeys} onChange={(event) => setQuery(event.target.value)} />
          </div>
          <div className="model-options" id={listId} role="listbox" aria-label={localized(language, "可用模型", "Available models")}>
            {loading && <div className="model-picker-state" role="status">{localized(language, "正在从当前 Preset 草稿获取模型…", "Discovering models from the current Preset draft…")}</div>}
            {!loading && error && <div className="model-picker-state error-text" role="alert">{error}</div>}
            {!loading && !error && models !== null && filteredModels.length === 0 && <div className="model-picker-state">{models.length === 0 ? localized(language, "端点返回空模型列表", "The endpoint returned no models") : localized(language, "没有匹配的模型", "No matching models")}</div>}
            {!loading && !error && models === null && <div className="model-picker-state">{localized(language, "点击“获取模型”检查当前草稿连接。", "Click Discover models to check the current draft connection.")}</div>}
            {!loading && !error && filteredModels.map((item, index) => (
              <button type="button" key={item.id} id={`${listId}-option-${index}`} role="option" aria-selected={item.id === value} className={`model-option${index === activeIndex ? " active" : ""}${item.id === value ? " selected" : ""}`} onMouseEnter={() => setActiveIndex(index)} onClick={() => choose(item)}>
                <span><strong>{item.display}</strong><code>{item.id}</code></span>
                {item.id === value && <Icon><path d="m5 12 4 4L19 6" /></Icon>}
              </button>
            ))}
          </div>
          <div className="model-picker-footer">{localized(language, `共 ${models?.length ?? 0} 个模型 · 选择后仍需保存`, `${models?.length ?? 0} models · save to apply`)}</div>
        </div>
      )}
    </div>
  );
}

function ConfigSection({ title, description, warning = false, children }: { title: string; description: string; warning?: boolean; children: ReactNode }) { return <fieldset className={`config-section${warning ? " warning" : ""}`}><legend>{title}</legend><p>{description}</p><div className="config-grid">{children}</div></fieldset>; }
function Field({ label, help, children }: { label: string; help?: string; children: ReactNode }) { return <label className="config-field"><span>{label}</span>{children}{help && <small>{help}</small>}</label>; }
function NumberField({ label, value, onChange, help, min, max, step }: { label: string; value: number; onChange: (value: number) => void; help?: string; min?: number; max?: number; step: number }) { return <Field label={label} help={help}><input type="number" value={value} min={min} max={max} step={step} onChange={(event) => { if (event.target.value !== "") onChange(event.target.valueAsNumber); }} /></Field>; }
function ToggleField({ label, checked, onChange, help, disabled = false }: { label: string; checked: boolean; onChange: (value: boolean) => void; help?: string; disabled?: boolean }) { return <label className="config-toggle"><span><input type="checkbox" checked={checked} disabled={disabled} onChange={(event) => onChange(event.target.checked)} />{label}</span>{help && <small>{help}</small>}</label>; }

function PromptSettings({ project, scope, language }: { project: string; scope: ConfigScope; language: Language }) {
  const en = language === "en";
  const [stage, setStage] = useState("translation"); const [content, setContent] = useState(""); const [message, setMessage] = useState(""); const [error, setError] = useState("");
  const path = scope === "global" ? `/api/v1/global/prompts/${stage}` : `/api/v1/projects/${project}/prompts/${stage}`;
  useEffect(() => { setMessage(""); setError(""); void api<{ content: string }>(path).then((value) => setContent(value.content)).catch((reason) => setError(errorMessage(reason))); }, [path]);
  async function save() { try { await api(path, { method: "PUT", body: JSON.stringify({ content }) }); setMessage(scope === "global" ? localized(language, "全局 Prompt 已保存；现有项目不会自动改变", "Global Prompt saved; existing projects are unchanged") : localized(language, "项目 Prompt 已保存", "Project Prompt saved")); } catch (reason) { setError(errorMessage(reason)); } }
  return <section className="text-settings"><div className="page-heading config-heading settings-action-heading"><div><h1>{scope === "global" ? (en ? "Global Prompt template" : "全局 Prompt 模板") : (en ? "Project Prompt" : "项目 Prompt")}</h1><p>{scope === "global" ? (en ? "Affects new projects or projects explicitly synchronized." : "只影响新项目或明确同步的项目。") : (en ? "Edit the project's Prompt copy." : "编辑项目内的阶段 Prompt 副本。")}</p></div><button className="primary-button" onClick={save}>{en ? "Validate and save" : "验证并保存"}</button></div><label className="stage-select">{en ? "Stage" : "阶段"}<select value={stage} onChange={(event) => setStage(event.target.value)}><option value="terminology">{en ? "Terms" : "术语"}</option><option value="translation">{en ? "Translation" : "翻译"}</option><option value="proofreading">{en ? "Proofreading" : "校对"}</option><option value="polishing">{en ? "Polishing" : "润色"}</option></select></label>{error && <div className="error-banner">{error}</div>}<span className="success-text">{message}</span><textarea className="settings-editor" spellCheck={false} value={content} onChange={(event) => setContent(event.target.value)} /></section>;
}

function localized(language: Language, chinese: string, english: string): string {
  return language === "en" ? english : chinese;
}

function errorMessage(reason: unknown): string { return reason instanceof Error ? reason.message : "请求失败"; }
