import { useDialogs, Feedback } from "./InteractionProvider";
import { useEffect, useState, type ReactNode } from "react";
import { api } from "../api";
import { translate, errorMessage, type Language } from "../i18n";
import type { PromptLibraryEntry } from "../types";
import { PromptSyncCard, type PromptGlobalSync } from "./PromptSyncCard";
import { PromptLibraryControls } from "./PromptLibraryControls";

export interface DecisionPromptSummary { validator_id: string; label: string; choice_ids: string[] }
interface Content { instructions: string; criteria: Record<string, string> }
interface View { content: Content; choice_ids: string[]; inherited: boolean; global_sync?: PromptGlobalSync }

export function DecisionPromptSettings({ project, scope, language, validator, stageControl }: {
  project: string; scope: "global" | "project"; language: Language; validator: string; stageControl: ReactNode;
}) {
  const dialogs = useDialogs();
  const [view, setView] = useState<View>();
  const [defaults, setDefaults] = useState<{ content: Content; choice_ids: string[] }>();
  const [draft, setDraft] = useState<Content>();
  const [entries, setEntries] = useState<PromptLibraryEntry[]>([]);
  const [selected, setSelected] = useState("");
  const [saveOpen, setSaveOpen] = useState(false);
  const [id, setId] = useState("");
  const [overwrite, setOverwrite] = useState(false);
  const [error, setError] = useState("");
  const [message, setMessage] = useState("");
  const [loadedGlobal, setLoadedGlobal] = useState(false);
  const [libraryLoading, setLibraryLoading] = useState(false);
  const path = scope === "global" ? `/api/v1/global/decision-prompts/${validator}` : `/api/v1/projects/${encodeURIComponent(project)}/decision-prompts/${validator}`;
  const libraryPath = `/api/v1/decision-prompt-library/${validator}`;

  function apply(value: View) { setView(value); setDraft(value.content); setLoadedGlobal(false); }
  useEffect(() => {
    let active = true;
    setView(undefined); setDraft(undefined); setDefaults(undefined); setError(""); setMessage("");
    void api<{ content: Content; choice_ids: string[] }>(`/api/v1/decision-prompts/${validator}/default`).then(async (value) => {
      if (!active) return;
      setDefaults(value);
      const current = await api<View>(path);
      if (active) apply(current);
    }).catch((reason) => { if (active) setError(errorMessage(reason, language)); });
    return () => { active = false; };
  }, [path]);
  useEffect(() => {
    if (!defaults) return;
    let active = true;
    setLibraryLoading(true);
    setEntries([]); setSelected(""); setSaveOpen(false); setId(""); setOverwrite(false);
    void api<{ entries: PromptLibraryEntry[] }>(libraryPath).then((value) => { if (active) setEntries(value.entries); }).catch((reason) => { if (active) setError(errorMessage(reason, language)); }).finally(() => { if (active) setLibraryLoading(false); });
    return () => { active = false; };
  }, [libraryPath, !!defaults]);

  async function action(work: () => Promise<void>) {
    setError(""); setMessage("");
    try { await work(); } catch (reason) { setError(errorMessage(reason, language)); }
  }
  async function refreshEntries() { setEntries((await api<{ entries: PromptLibraryEntry[] }>(libraryPath)).entries); }
  async function confirmReplace() { return !view || JSON.stringify(draft) === JSON.stringify(view.content) || await dialogs.confirm(translate("decision.promptDiscard", language)); }

  return <section className="text-settings">
    <div className="page-heading config-heading settings-action-heading">
      <div><h1>{translate(scope === "global" ? "settings.globalPromptTitle" : "settings.projectPromptTitle", language)}</h1><p>{translate("decision.promptHint", language)}</p></div>
      <div className="button-group">
        <button className="quiet-button" disabled={!defaults} onClick={async () => {
          if (!defaults || !await confirmReplace()) return;
          setError("");
          if (!view) setView({ ...defaults, inherited: false });
          setDraft(defaults.content); setLoadedGlobal(false); setMessage(translate("settings.promptDefaultLoaded", language));
        }}>{translate("settings.promptLoadDefault", language)}</button>
        <button className="quiet-button" disabled={!draft} onClick={() => { setSaveOpen(true); setOverwrite(false); }}>{translate("settings.promptLibrarySave", language)}</button>
        <button className="primary-button" disabled={!draft || !view} onClick={() => void action(async () => {
          await api(path, { method: "PUT", body: JSON.stringify({ content: draft }) });
          apply(await api<View>(path));
          setMessage(translate(scope === "global" ? "settings.globalPromptSaved" : "settings.projectPromptSaved", language));
        })}>{translate("common.validateSave", language)}</button>
      </div>
    </div>
    {stageControl}
    {defaults && <>
      {scope === "project" && <PromptSyncCard language={language} sync={view?.global_sync ?? { available: false, same: false }}
        dirty={!!view && JSON.stringify(draft) !== JSON.stringify(view.content)} loadedGlobal={loadedGlobal} inherited={view?.inherited}
        onLoadGlobal={async () => {
          if (!await confirmReplace()) return;
          void action(async () => {
            const global = await api<View>(`/api/v1/global/decision-prompts/${validator}`);
            setDraft(global.content); setLoadedGlobal(true);
          });
        }}
        onRestore={async () => {
          if (!await dialogs.confirm(translate("decision.promptRestoreConfirm", language))) return;
          void action(async () => { await api(path, { method: "DELETE", body: JSON.stringify({ confirm: true }) }); apply(await api<View>(path)); setMessage(translate("decision.promptRestored", language)); });
        }} />}
      <PromptLibraryControls language={language} hint={translate("decision.promptLibraryHint", language)} entries={entries} selected={selected} loading={libraryLoading} saveOpen={saveOpen} id={id} overwrite={overwrite}
        onId={(value) => { setId(value); setOverwrite(false); }} onCancel={() => { setSaveOpen(false); setId(""); setOverwrite(false); }}
        onSelect={async (value) => {
          if (!value || !await confirmReplace()) return;
          void action(async () => {
            const loaded = (await api<{ content: Content }>(`${libraryPath}/${encodeURIComponent(value)}`)).content;
            if (!view) setView({ content: loaded, choice_ids: Object.keys(loaded.criteria), inherited: false });
            setDraft(loaded); setLoadedGlobal(false);
            setSelected(value); setMessage(translate("settings.promptLibraryLoaded", language, { id: value }));
          });
        }}
        onDelete={async () => {
          if (!selected || !await dialogs.confirm(translate("settings.promptLibraryDeleteConfirm", language, { id: selected }))) return;
          void action(async () => { await api(`${libraryPath}/${encodeURIComponent(selected)}`, { method: "DELETE" }); await refreshEntries(); setSelected(""); setMessage(translate("settings.promptLibraryDeleted", language)); });
        }}
        onSave={() => {
          const name = id.trim(); if (!name || !draft) return;
          if (entries.some((entry) => entry.id === name) && !overwrite) { setOverwrite(true); return; }
          void action(async () => { await api(`${libraryPath}/${encodeURIComponent(name)}`, { method: "PUT", body: JSON.stringify({ content: draft }) }); await refreshEntries(); setSelected(name); setSaveOpen(false); setId(""); setOverwrite(false); setMessage(translate("settings.promptLibrarySaved", language, { id: name })); });
        }} />
    </>}
    {error && <Feedback message={error} onDismiss={() => setError((current) => current === error ? "" : current)} />}
    {message && <Feedback message={message} type="success" onDismiss={() => setMessage((current) => current === message ? "" : current)} />}
    {draft && view && <>
      <label className="config-field"><span>{translate("decision.promptInstructions", language)}</span><textarea className="settings-editor decision-prompt-instructions" spellCheck={false} value={draft.instructions} onChange={(event) => { setDraft({ ...draft, instructions: event.target.value }); setLoadedGlobal(false); setMessage(""); }} /></label>
      {view.choice_ids.map((choice) => <label className="config-field" key={choice}><span>{choice}</span><textarea className="settings-editor decision-prompt-criterion" spellCheck={false} value={draft.criteria[choice]} onChange={(event) => { setDraft({ ...draft, criteria: { ...draft.criteria, [choice]: event.target.value } }); setLoadedGlobal(false); setMessage(""); }} /></label>)}
      <div className="prompt-preview"><h3>{translate("settings.promptAssembled", language)}</h3><pre>{draft.instructions}{"\n\n"}{view.choice_ids.map((choice) => `${choice}: ${draft.criteria[choice]}`).join("\n\n")}</pre></div>
    </>}
  </section>;
}
