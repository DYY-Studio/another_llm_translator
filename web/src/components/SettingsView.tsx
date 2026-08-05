import { useEffect, useId, useMemo, useRef, useState, type KeyboardEvent, type ReactNode } from "react";
import { api } from "../api";
import { translate, type Language } from "../i18n";
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
      <nav className="settings-navigation" aria-label={translate("settings.title", language)}>
        <div className="settings-scope-tabs" aria-label={translate("settings.scope", language)}>
          <button disabled={!project} className={activeScope === "project" ? "active" : ""} onClick={() => setScope("project")}>{translate("settings.project", language)}</button>
          <button className={activeScope === "global" ? "active" : ""} onClick={() => setScope("global")}>{translate("settings.global", language)}</button>
        </div>
        <div className="settings-section-tabs" aria-label={translate("settings.sectionsAria", language, { scope: translate(activeScope === "project" ? "settings.projectShort" : "settings.globalShort", language) })}>
          <button className={section === "config" ? "active" : ""} onClick={() => setSection("config")}>{translate("settings.config", language)}</button>
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
      if (active) setError(errorMessage(reason, language));
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
      setMessage(scope === "global" ? translate("settings.globalSaved", language) : translate("settings.projectSaved", language));
    } catch (reason) {
      setError(errorMessage(reason, language));
    } finally {
      setSaving(false);
    }
  }

  async function syncGlobal() {
    if (!project || !window.confirm(translate("settings.syncConfirm", language))) return;
    try {
      const result = await api<{ warnings: string[] }>(`/api/v1/projects/${project}/sync-templates`, {
        method: "POST",
        body: JSON.stringify({ choice: "update" }),
      });
      await load();
      setMessage(result.warnings.join("；"));
    } catch (reason) {
      setError(errorMessage(reason, language));
    }
  }

  if (!config) return <section className="text-settings"><p className={error ? "error-text" : "muted"}>{error || translate("settings.loadingConfig", language)}</p></section>;

  const presetOptions = presets.filter((item) => item.valid);
  const contextLabels: Array<[ContextStage, string]> = [
    ["terminology", translate("stage.terminology", language)],
    ["translation", translate("stage.translation", language)],
    ["proofreading", translate("stage.proofreading", language)],
    ["polishing", translate("stage.polishing", language)],
  ];
  const stagePresetFields: Array<[ContextStage, string]> = contextLabels.map(([stage, label]) => [stage, translate("settings.stagePreset", language, { stage: label })]);
  const crossBoundaryStages: Array<[LLMStage, string]> = contextLabels;
  return (
    <section className="config-settings">
      <div className="page-heading config-heading settings-action-heading">
        <div><h1>{scope === "global" ? translate("settings.globalConfigTitle", language) : translate("settings.projectConfigTitle", language)}</h1><p>{scope === "global" ? translate("settings.globalConfigHint", language) : translate("settings.projectConfigHint", language)}</p></div>
        <div className="button-group">
          {scope === "project" && <button className="quiet-button" onClick={syncGlobal}>{translate("settings.syncGlobal", language)}</button>}
          <button className="primary-button" disabled={saving} onClick={save}>{saving ? translate("common.saving", language) : translate("common.validateSave", language)}</button>
        </div>
      </div>
      {error && <div className="error-banner" role="alert">{error}</div>}
      {message && <p className="success-text">{message}</p>}
      <div className="config-form">
        <ConfigSection title={translate("settings.projectInput", language)} description={translate("settings.projectInputHint", language)}>
          <Field label={translate("settings.targetLanguage", language)}><input value={config.project.target_language} onChange={(event) => update((draft) => { draft.project.target_language = event.target.value; })} /></Field>
          <Field label={translate("settings.txtOutputEncoding", language)}><input value={config.project.output_encoding} onChange={(event) => update((draft) => { draft.project.output_encoding = event.target.value; })} /></Field>
          <NumberField label={translate("settings.encodingThreshold", language)} value={config.input.encoding_confidence_threshold} min={0} max={1} step={0.05} onChange={(value) => update((draft) => { draft.input.encoding_confidence_threshold = value; })} />
          <Field label={translate("settings.fallbackEncoding", language)}><input value={config.input.fallback_encoding} onChange={(event) => update((draft) => { draft.input.fallback_encoding = event.target.value; })} /></Field>
        </ConfigSection>
        <ConfigSection title={translate("settings.llmSampling", language)} description={translate("settings.llmSamplingHint", language)}>
          <Field label={translate("settings.globalPreset", language)}><select value={config.llm.preset} onChange={(event) => update((draft) => { draft.llm.preset = event.target.value; })}>{presetOptions.map((item) => <option key={item.preset_id} value={item.preset_id}>{item.preset_id} · {item.model}</option>)}</select></Field>
          {stagePresetFields.map(([stage, label]) => <Field label={label} help={translate("settings.presetEmptyHint", language)} key={stage}><select value={config.llm[`preset_${stage}`]} onChange={(event) => update((draft) => { draft.llm[`preset_${stage}`] = event.target.value; })}><option value="">{translate("settings.useGlobalPreset", language)}</option>{presetOptions.map((item) => <option key={item.preset_id} value={item.preset_id}>{item.preset_id} · {item.model}</option>)}</select></Field>)}
          <NumberField label={translate("settings.tempTerms", language)} value={config.llm.temperature_terminology} min={0} step={0.1} onChange={(value) => update((draft) => { draft.llm.temperature_terminology = value; })} />
          <NumberField label={translate("settings.tempTranslation", language)} value={config.llm.temperature_translation} min={0} step={0.1} onChange={(value) => update((draft) => { draft.llm.temperature_translation = value; })} />
          <NumberField label={translate("settings.tempProofreading", language)} value={config.llm.temperature_proofreading} min={0} step={0.1} onChange={(value) => update((draft) => { draft.llm.temperature_proofreading = value; })} />
          <NumberField label={translate("settings.tempPolishing", language)} value={config.llm.temperature_polishing} min={0} step={0.1} onChange={(value) => update((draft) => { draft.llm.temperature_polishing = value; })} />
        </ConfigSection>
        <ConfigSection title={translate("settings.execution", language)} description={translate("settings.executionHint", language)}>
          <Field label={translate("settings.schedulingMode", language)}><select value={config.execution.scheduling_mode} onChange={(event) => update((draft) => { draft.execution.scheduling_mode = event.target.value as ProjectConfig["execution"]["scheduling_mode"]; })}><option value="ordered_by_file">{translate("settings.orderedByFile", language)}</option><option value="parallel">{translate("settings.parallel", language)}</option></select></Field>
          <NumberField label={translate("settings.targetChunkTokens", language)} value={config.chunking.target_chunk_input_tokens} min={1} step={1} onChange={(value) => update((draft) => { draft.chunking.target_chunk_input_tokens = value; })} />
          <ToggleField label={translate("settings.splitOversized", language)} checked={config.chunking.allow_split_oversized_segment} onChange={(value) => update((draft) => { draft.chunking.allow_split_oversized_segment = value; })} />
          {crossBoundaryStages.map(([stage, label]) => <ToggleField key={stage} label={translate("settings.crossBoundary", language, { stage: label })} checked={config.chunking.cross_boundary_batching.includes(stage)} help={translate("settings.crossBoundaryHint", language)} onChange={(value) => update((draft) => { const selected = new Set(draft.chunking.cross_boundary_batching); if (value) selected.add(stage); else selected.delete(stage); draft.chunking.cross_boundary_batching = crossBoundaryStages.map(([candidate]) => candidate).filter((candidate) => selected.has(candidate)); })} />)}
        </ConfigSection>
        <ConfigSection title={translate("settings.referenceContext", language)} description={translate("settings.referenceContextHint", language)}>
          {contextLabels.map(([stage, label]) => <div className="context-config-row" key={stage}><ToggleField label={translate("settings.contextEnabled", language, { stage: label })} checked={config.context[stage].enabled} onChange={(value) => update((draft) => { draft.context[stage].enabled = value; })} /><NumberField label={translate("settings.previousSegments", language)} value={config.context[stage].previous_segments} min={0} step={1} onChange={(value) => update((draft) => { draft.context[stage].previous_segments = value; })} /></div>)}
        </ConfigSection>
        <ConfigSection title={translate("settings.terminology", language)} description={translate("settings.terminologyHint", language)}>
          <Field label={translate("settings.unicodeNormalization", language)} help={translate("settings.unicodeHint", language)}><input disabled value={config.terminology.unicode_normalization} /></Field>
          <ToggleField label={translate("settings.caseInsensitive", language)} checked={config.terminology.case_insensitive} disabled help={translate("settings.casefoldHint", language)} onChange={() => undefined} />
          <NumberField label={translate("settings.maxTermsPerSegment", language)} value={config.terminology.max_terms_per_segment} min={1} step={1} onChange={(value) => update((draft) => { draft.terminology.max_terms_per_segment = value; })} />
          <Field label={translate("settings.aliasCollision", language)}><select value={config.terminology.alias_primary_collision} onChange={(event) => update((draft) => { draft.terminology.alias_primary_collision = event.target.value as ProjectConfig["terminology"]["alias_primary_collision"]; })}><option value="conflict">{translate("settings.requireReview", language)}</option><option value="merge">{translate("settings.deterministicMerge", language)}</option></select></Field>
        </ConfigSection>
        <ConfigSection title={translate("settings.validation", language)} description={translate("settings.validationHint", language)}>
          <ToggleField label={translate("settings.japaneseKana", language)} checked={config.validation.translation.japanese_kana} onChange={(value) => update((draft) => { draft.validation.translation.japanese_kana = value; })} />
          <ToggleField label={translate("settings.koreanHangul", language)} checked={config.validation.translation.korean_hangul} onChange={(value) => update((draft) => { draft.validation.translation.korean_hangul = value; })} />
          <NumberField label={translate("settings.repairAttempts", language)} value={config.validation.translation.max_retry_attempts} min={0} step={1} onChange={(value) => update((draft) => { draft.validation.translation.max_retry_attempts = value; })} />
          <Field label={translate("settings.exhaustedMode", language)}><select value={config.validation.translation.exhausted_mode} onChange={(event) => update((draft) => { draft.validation.translation.exhausted_mode = event.target.value as ProjectConfig["validation"]["translation"]["exhausted_mode"]; })}><option value="fail">{translate("settings.markFailed", language)}</option><option value="warning">{translate("settings.acceptWarning", language)}</option></select></Field>
          <NumberField label={translate("settings.httpMaxAttempts", language)} value={config.retry.http_max_attempts} min={1} step={1} onChange={(value) => update((draft) => { draft.retry.http_max_attempts = value; })} />
          <NumberField label={translate("settings.formatRepairAttempts", language)} value={config.retry.format_max_attempts} min={0} step={1} onChange={(value) => update((draft) => { draft.retry.format_max_attempts = value; })} />
          <NumberField label={translate("settings.baseDelay", language)} value={config.retry.base_delay_seconds} min={0} step={0.1} onChange={(value) => update((draft) => { draft.retry.base_delay_seconds = value; })} />
          <NumberField label={translate("settings.maxDelay", language)} value={config.retry.max_delay_seconds} min={0} step={0.1} onChange={(value) => update((draft) => { draft.retry.max_delay_seconds = value; })} />
          <NumberField label={translate("settings.jitter", language)} value={config.retry.jitter_seconds} min={0} step={0.1} onChange={(value) => update((draft) => { draft.retry.jitter_seconds = value; })} />
        </ConfigSection>
        <ConfigSection title={translate("settings.debug", language)} description={translate("settings.debugHint", language)} warning>
          <ToggleField label={translate("settings.enableDebug", language)} checked={config.debug.enabled} onChange={(value) => update((draft) => { draft.debug.enabled = value; })} />
          <NumberField label={translate("settings.inject429", language)} value={config.debug.inject_429_every} min={0} step={1} onChange={(value) => update((draft) => { draft.debug.inject_429_every = value; })} />
          <NumberField label={translate("settings.inject500", language)} value={config.debug.inject_500_every} min={0} step={1} onChange={(value) => update((draft) => { draft.debug.inject_500_every = value; })} />
          <NumberField label={translate("settings.injectTimeout", language)} value={config.debug.inject_timeout_every} min={0} step={1} onChange={(value) => update((draft) => { draft.debug.inject_timeout_every = value; })} />
          <NumberField label={translate("settings.injectInvalidJson", language)} value={config.debug.inject_invalid_json_every} min={0} step={1} onChange={(value) => update((draft) => { draft.debug.inject_invalid_json_every = value; })} />
          <NumberField label={translate("settings.injectMissingSegment", language)} value={config.debug.inject_missing_segment_every} min={0} step={1} onChange={(value) => update((draft) => { draft.debug.inject_missing_segment_every = value; })} />
        </ConfigSection>
      </div>
    </section>
  );
}

function PresetSettings({ language }: { language: Language }) {
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

  useEffect(() => { void loadLists().catch((reason) => setError(errorMessage(reason, language))); }, []);
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
      if (active) setError(errorMessage(reason, language));
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
    } catch (reason) { setModelsError(errorMessage(reason, language)); }
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
      setMessage(translate("preset.saved", language));
    } catch (reason) { setError(errorMessage(reason, language)); }
  }

  async function createPreset() {
    const presetId = window.prompt(translate("preset.newId", language));
    if (!presetId || !preset) return;
    try {
      const definition = { ...preset, preset_id: presetId, extra_body: JSON.parse(extraBody) as unknown };
      await api(`/api/v1/global/presets/${presetId}`, { method: "PUT", body: JSON.stringify(definition) });
      await loadLists(presetId);
      setMessage(translate("preset.created", language, { id: presetId }));
    } catch (reason) { setError(errorMessage(reason, language)); }
  }

  async function removePreset() {
    if (!preset || !window.confirm(translate("preset.deleteConfirm", language, { id: preset.preset_id }))) return;
    try {
      await api(`/api/v1/global/presets/${preset.preset_id}`, { method: "DELETE" });
      setPreset(null); setSelected(""); await loadLists(null);
    } catch (reason) { setError(errorMessage(reason, language)); }
  }

  return (
    <div className="preset-layout">
      <div className="page-heading preset-list-heading"><div><h1>{translate("preset.title", language)}</h1><p>{translate("preset.subtitle", language)}</p></div><button className="quiet-button" disabled={!preset} onClick={createPreset}>{translate("common.new", language)}</button></div>
      <aside className="preset-list-body">
        {presets.map((item) => <button key={item.preset_id} className={selected === item.preset_id ? "preset-row active" : "preset-row"} onClick={() => setSelected(item.preset_id)}><strong>{item.preset_id}</strong><small>{item.valid ? `${item.adapter_id} · ${item.model}` : item.error}</small></button>)}
      </aside>
      <div className="page-heading settings-action-heading preset-editor-heading">
        <div><h1>{preset?.preset_id ?? (presetLoading ? translate("preset.loading", language, { id: selected }) : translate("preset.editor", language))}</h1><p>{preset ? translate("preset.changeHint", language) : presetLoading ? translate("preset.loadingHint", language) : translate("preset.selectHint", language)} </p></div>
        {preset && <div className="button-group"><button className="danger-button" onClick={removePreset}>{translate("common.delete", language)}</button><button className="primary-button" onClick={save}>{translate("common.validateSave", language)}</button></div>}
      </div>
      <section className="preset-editor-body">
        {!preset ? (
          <>{error && <div className="error-banner">{error}</div>}<p className="muted">{presetLoading ? translate("preset.loadingPreset", language) : translate("preset.selectHint", language)}</p></>
        ) : (
          <>
            {error && <div className="error-banner">{error}</div>}
            {message && <p className="success-text">{message}</p>}
            <div className="config-grid preset-fields">
              <Field label="Adapter"><select value={preset.adapter_id} onChange={(event) => updateConnection((draft) => { draft.adapter_id = event.target.value; })}>{adapters.filter((item) => item.valid !== false).map((item) => <option key={item.adapter_id}>{item.adapter_id}</option>)}</select></Field>
              <Field label="Base URL"><input value={preset.base_url} onChange={(event) => updateConnection((draft) => { draft.base_url = event.target.value; })} /></Field>
              <Field label="Endpoint"><input value={preset.endpoint} onChange={(event) => update((draft) => { draft.endpoint = event.target.value; })} /></Field>
              <Field label={translate("preset.apiKeyEnv", language)}><input value={preset.api_key_env} onChange={(event) => updateConnection((draft) => { draft.api_key_env = event.target.value; })} /></Field>
              <ModelPicker language={language} value={preset.model} models={models} loading={modelsLoading} error={modelsError} onChange={(value) => update((draft) => { draft.model = value; })} onDiscover={() => void discoverModels()} onSelect={(value) => { update((draft) => { draft.model = value; }); setMessage(translate("preset.selected", language, { model: value })); }} />
              <Field label={translate("preset.proxyUrl", language)}><input value={preset.proxy_url} onChange={(event) => updateConnection((draft) => { draft.proxy_url = event.target.value; })} /></Field>
              <NumberField label={translate("preset.contextWindow", language)} value={preset.context_window_tokens} min={1} step={1} onChange={(value) => update((draft) => { draft.context_window_tokens = value; })} />
              <NumberField label={translate("preset.maxOutputTokens", language)} value={preset.max_output_tokens} min={1} step={1} onChange={(value) => update((draft) => { draft.max_output_tokens = value; })} />
              <NumberField label={translate("preset.contextSafetyMargin", language)} value={preset.context_safety_margin_tokens} min={0} step={1} onChange={(value) => update((draft) => { draft.context_safety_margin_tokens = value; })} />
              <NumberField label={translate("preset.tokenSafetyFactor", language)} value={preset.token_safety_factor} min={0.01} step={0.05} onChange={(value) => update((draft) => { draft.token_safety_factor = value; })} />
              <NumberField label="RPM" value={preset.requests_per_minute} min={0} step={1} help={translate("preset.rpmHint", language)} onChange={(value) => update((draft) => { draft.requests_per_minute = value; })} />
              <NumberField label="ITPM" value={preset.input_tokens_per_minute} min={0} step={1} help={translate("preset.itpmHint", language)} onChange={(value) => update((draft) => { draft.input_tokens_per_minute = value; })} />
              <NumberField label={translate("preset.maxConcurrency", language)} value={preset.max_parallel} min={1} step={1} help={translate("preset.maxConcurrencyHint", language)} onChange={(value) => update((draft) => { draft.max_parallel = value; })} />
              <NumberField label={translate("preset.timeoutSeconds", language)} value={preset.request_timeout_seconds} min={0.01} step={1} onChange={(value) => updateConnection((draft) => { draft.request_timeout_seconds = value; })} />
              <label className="code-field preset-extra"><span>{translate("preset.extraBody", language)}</span><small>{translate("preset.extraBodyHint", language)}</small><textarea spellCheck={false} value={extraBody} onChange={(event) => setExtraBody(event.target.value)} /></label>
            </div>
            <h2 className="preview-heading">{translate("preset.requestPreview", language)}</h2>
            <pre className="result-box">{preview ? JSON.stringify(preview, null, 2) : translate("preset.previewHint", language)}</pre>
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
      <span>{translate("preset.modelId", language)}</span>
      <div className="model-picker-control">
        <input value={value} role="combobox" aria-autocomplete="list" aria-expanded={open} aria-controls={listId} aria-activedescendant={activeOptionId} onKeyDown={handleKeys} onChange={(event) => onChange(event.target.value)} />
        <button type="button" className="quiet-button model-discover-button" disabled={loading} onClick={() => { setQuery(""); openPicker(); onDiscover(); }}><Icon><path d="M20 6v5h-5" /><path d="M4 18v-5h5" /><path d="M18.2 9A7 7 0 0 0 6.4 6.4L4 9" /><path d="M5.8 15A7 7 0 0 0 17.6 17.6L20 15" /></Icon>{loading ? translate("preset.discoverLoading", language) : translate("preset.discoverModels", language)}</button>
      </div>
      {open && (
        <div className="model-picker-popover">
          <div className="model-search">
            <Icon><circle cx="11" cy="11" r="7" /><path d="m20 20-4-4" /></Icon>
            <input ref={searchRef} aria-label={translate("preset.searchModels", language)} placeholder={translate("preset.searchModelsPlaceholder", language)} value={query} onKeyDown={handleKeys} onChange={(event) => setQuery(event.target.value)} />
          </div>
          <div className="model-options" id={listId} role="listbox" aria-label={translate("preset.availableModels", language)}>
            {loading && <div className="model-picker-state" role="status">{translate("preset.discovering", language)}</div>}
            {!loading && error && <div className="model-picker-state error-text" role="alert">{error}</div>}
            {!loading && !error && models !== null && filteredModels.length === 0 && <div className="model-picker-state">{models.length === 0 ? translate("preset.noModels", language) : translate("preset.noMatchModels", language)}</div>}
            {!loading && !error && models === null && <div className="model-picker-state">{translate("preset.discoverHint", language)}</div>}
            {!loading && !error && filteredModels.map((item, index) => (
              <button type="button" key={item.id} id={`${listId}-option-${index}`} role="option" aria-selected={item.id === value} className={`model-option${index === activeIndex ? " active" : ""}${item.id === value ? " selected" : ""}`} onMouseEnter={() => setActiveIndex(index)} onClick={() => choose(item)}>
                <span><strong>{item.display}</strong><code>{item.id}</code></span>
                {item.id === value && <Icon><path d="m5 12 4 4L19 6" /></Icon>}
              </button>
            ))}
          </div>
          <div className="model-picker-footer">{translate("preset.footer", language, { count: models?.length ?? 0 })}</div>
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
  const [stage, setStage] = useState("translation"); const [content, setContent] = useState(""); const [message, setMessage] = useState(""); const [error, setError] = useState("");
  const path = scope === "global" ? `/api/v1/global/prompts/${stage}` : `/api/v1/projects/${project}/prompts/${stage}`;
  useEffect(() => { setMessage(""); setError(""); void api<{ content: string }>(path).then((value) => setContent(value.content)).catch((reason) => setError(errorMessage(reason, language))); }, [path]);
  async function save() { try { await api(path, { method: "PUT", body: JSON.stringify({ content }) }); setMessage(scope === "global" ? translate("settings.globalPromptSaved", language) : translate("settings.projectPromptSaved", language)); } catch (reason) { setError(errorMessage(reason, language)); } }
  return <section className="text-settings"><div className="page-heading config-heading settings-action-heading"><div><h1>{scope === "global" ? translate("settings.globalPromptTitle", language) : translate("settings.projectPromptTitle", language)}</h1><p>{scope === "global" ? translate("settings.globalConfigHint", language) : translate("settings.projectPromptHint", language)}</p></div><button className="primary-button" onClick={save}>{translate("common.validateSave", language)}</button></div><label className="stage-select">{translate("settings.stageSelect", language)}<select value={stage} onChange={(event) => setStage(event.target.value)}><option value="terminology">{translate("stage.terminology", language)}</option><option value="translation">{translate("stage.translation", language)}</option><option value="proofreading">{translate("stage.proofreading", language)}</option><option value="polishing">{translate("stage.polishing", language)}</option></select></label>{error && <div className="error-banner">{error}</div>}<span className="success-text">{message}</span><textarea className="settings-editor" spellCheck={false} value={content} onChange={(event) => setContent(event.target.value)} /></section>;
}

function errorMessage(reason: unknown, language: Language): string { return reason instanceof Error ? reason.message : translate("common.requestFailed", language); }
