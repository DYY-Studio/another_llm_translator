import { useEffect, useState, type ReactNode } from "react";
import { api } from "../api";
import { translate, errorMessage, type Language } from "../i18n";
import type { PromptLibraryEntry } from "../types";
import { PromptLibraryControls } from "./PromptLibraryControls";

export interface DecisionPromptSummary { validator_id: string; label: string; languages: string[]; choice_ids: string[] }
interface Content { instructions: string; criteria: Record<string, string> }
interface View { content: Content; language: string; languages: string[]; choice_ids: string[]; inherited: boolean; source: string }

export function DecisionPromptSettings({ project, scope, language, validator, stageControl }: {
  project: string; scope: "global" | "project"; language: Language; validator: string; stageControl: ReactNode;
}) {
  const [view, setView] = useState<View>();
  const [selection, setSelection] = useState<{ language: string; languages: string[] }>();
  const [draft, setDraft] = useState<Content>();
  const [entries, setEntries] = useState<PromptLibraryEntry[]>([]);
  const [selected, setSelected] = useState("");
  const [saveOpen, setSaveOpen] = useState(false);
  const [id, setId] = useState("");
  const [overwrite, setOverwrite] = useState(false);
  const [error, setError] = useState("");
  const [message, setMessage] = useState("");
  const path = scope === "global" ? `/api/v1/global/decision-prompts/${validator}` : `/api/v1/projects/${encodeURIComponent(project)}/decision-prompts/${validator}`;
  const libraryPath = `/api/v1/decision-prompt-library/${validator}/${selection?.language ?? "en"}`;

  function apply(value: View) { setView(value); setDraft(value.content); }
  useEffect(() => {
    let active = true;
    setView(undefined); setDraft(undefined); setSelection(undefined); setError(""); setMessage("");
    void api<{ language: string; languages: string[] }>(`${path}/language`).then(async (selected) => {
      if (!active) return;
      setSelection(selected);
      const value = await api<View>(path);
      if (active) apply(value);
    }).catch((reason) => { if (active) setError(errorMessage(reason, language)); });
    return () => { active = false; };
  }, [path]);
  useEffect(() => {
    if (!selection) return;
    let active = true;
    setEntries([]); setSelected(""); setSaveOpen(false); setId(""); setOverwrite(false);
    void api<{ entries: PromptLibraryEntry[] }>(libraryPath).then((value) => { if (active) setEntries(value.entries); }).catch((reason) => { if (active) setError(errorMessage(reason, language)); });
    return () => { active = false; };
  }, [libraryPath, !!selection]);

  async function action(work: () => Promise<void>) {
    setError(""); setMessage("");
    try { await work(); } catch (reason) { setError(errorMessage(reason, language)); }
  }
  async function refreshEntries() { setEntries((await api<{ entries: PromptLibraryEntry[] }>(libraryPath)).entries); }
  function confirmReplace() { return !view || JSON.stringify(draft) === JSON.stringify(view.content) || window.confirm(translate("decision.promptDiscard", language)); }

  return <section className="text-settings">
    <div className="page-heading config-heading settings-action-heading">
      <div><h1>{translate(scope === "global" ? "settings.globalPromptTitle" : "settings.projectPromptTitle", language)}</h1><p>{translate("decision.promptHint", language)}</p></div>
      <div className="button-group">
        <button className="quiet-button" disabled={!selection} onClick={() => {
          if (!selection || !confirmReplace()) return;
          void action(async () => {
            const value = await api<{ content: Content; choice_ids: string[] }>(`/api/v1/decision-prompts/${validator}/default?language=${selection.language}`);
            setView({ ...value, ...selection, inherited: false, source: "default" });
            setDraft(value.content); setMessage(translate("decision.promptDefaultLoaded", language));
          });
        }}>{translate("decision.promptLoadDefault", language)}</button>
        <button className="quiet-button" disabled={!draft} onClick={() => { setSaveOpen(true); setOverwrite(false); }}>{translate("settings.promptLibrarySave", language)}</button>
        <button className="primary-button" disabled={!draft || !view} onClick={() => void action(async () => {
          await api(path, { method: "PUT", body: JSON.stringify({ language: view!.language, content: draft }) });
          apply(await api<View>(`${path}?language=${view!.language}`));
          setMessage(translate(scope === "global" ? "settings.globalPromptSaved" : "settings.projectPromptSaved", language));
        })}>{translate("common.validateSave", language)}</button>
      </div>
    </div>
    {stageControl}
    {selection && <>
      <label className="stage-select">{translate("settings.promptLanguage", language)}<select value={selection.language} onChange={(event) => {
        if (!confirmReplace()) return;
        const next = event.target.value;
        void action(async () => {
          await api(`${path}/language`, { method: "PUT", body: JSON.stringify({ language: next }) });
          setSelection({ ...selection, language: next }); setView(undefined); setDraft(undefined);
          apply(await api<View>(`${path}?language=${next}`));
        });
      }}>{selection.languages.map((value) => <option key={value} value={value}>{value}</option>)}</select></label>
      {scope === "project" && <div className={`prompt-sync-card ${view?.inherited ? "synced" : "out-of-sync"}`}>
        <div><strong>{translate(view?.inherited ? "decision.promptInherited" : "decision.promptOverride", language)}</strong></div>
        <button className="quiet-button" disabled={view?.inherited} onClick={() => {
          if (!confirmReplace()) return;
          void action(async () => { await api(`${path}?language=${selection.language}`, { method: "DELETE" }); apply(await api<View>(`${path}?language=${selection.language}`)); });
        }}>{translate("decision.promptRestore", language)}</button>
      </div>}
      <PromptLibraryControls language={language} entries={entries} selected={selected} loading={false} saveOpen={saveOpen} id={id} overwrite={overwrite}
        onId={(value) => { setId(value); setOverwrite(false); }} onCancel={() => { setSaveOpen(false); setId(""); setOverwrite(false); }}
        onSelect={(value) => {
          if (!value || !confirmReplace()) return;
          void action(async () => {
            const loaded = (await api<{ content: Content }>(`${libraryPath}/${encodeURIComponent(value)}`)).content;
            if (!view && selection) setView({ content: loaded, ...selection, choice_ids: Object.keys(loaded.criteria), inherited: false, source: "library" });
            setDraft(loaded);
            setSelected(value); setMessage(translate("settings.promptLibraryLoaded", language, { id: value }));
          });
        }}
        onDelete={() => {
          if (!selected || !window.confirm(translate("settings.promptLibraryDeleteConfirm", language, { id: selected }))) return;
          void action(async () => { await api(`${libraryPath}/${encodeURIComponent(selected)}`, { method: "DELETE" }); await refreshEntries(); setSelected(""); });
        }}
        onSave={() => {
          const name = id.trim(); if (!name || !draft) return;
          if (entries.some((entry) => entry.id === name) && !overwrite) { setOverwrite(true); return; }
          void action(async () => { await api(`${libraryPath}/${encodeURIComponent(name)}`, { method: "PUT", body: JSON.stringify({ content: draft }) }); await refreshEntries(); setSelected(name); setSaveOpen(false); setOverwrite(false); });
        }} />
    </>}
    {error && <div className="error-banner">{error}</div>}
    {message && <span className="success-text">{message}</span>}
    {draft && view && <>
      <label className="config-field"><span>{translate("decision.promptInstructions", language)}</span><textarea className="settings-editor decision-prompt-instructions" spellCheck={false} value={draft.instructions} onChange={(event) => { setDraft({ ...draft, instructions: event.target.value }); setMessage(""); }} /></label>
      {view.choice_ids.map((choice) => <label className="config-field" key={choice}><span>{choice}</span><textarea className="settings-editor decision-prompt-criterion" spellCheck={false} value={draft.criteria[choice]} onChange={(event) => { setDraft({ ...draft, criteria: { ...draft.criteria, [choice]: event.target.value } }); setMessage(""); }} /></label>)}
      <div className="prompt-preview"><h3>{translate("settings.promptAssembled", language)}</h3><pre>{draft.instructions}{"\n\n"}{view.choice_ids.map((choice) => `${choice}: ${draft.criteria[choice]}`).join("\n\n")}</pre></div>
    </>}
  </section>;
}
