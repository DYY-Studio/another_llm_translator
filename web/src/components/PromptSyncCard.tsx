import { translate, type Language } from "../i18n";

export interface PromptGlobalSync { available: boolean; same: boolean; language?: string; error?: string }

export function PromptSyncCard({ language, sync, dirty, loadedGlobal, inherited, onLoadGlobal, onRestore }: {
  language: Language; sync: PromptGlobalSync; dirty: boolean; loadedGlobal: boolean; inherited?: boolean;
  onLoadGlobal: () => void; onRestore?: () => void;
}) {
  const title = !sync.available ? "settings.promptGlobalUnavailable" : dirty ? "settings.promptUnsaved" : sync.same ? "settings.promptSynced" : "settings.promptOutOfSync";
  const detail = loadedGlobal ? translate("settings.promptGlobalLoadedHint", language) : sync.language ? translate("settings.promptSyncLanguage", language, { language: sync.language }) : "";
  return <div className={`prompt-sync-card ${sync.available && sync.same && !dirty ? "synced" : "out-of-sync"}`}>
    <div><strong>{translate(title, language)}</strong><small>{inherited === undefined ? "" : `${translate(inherited ? "decision.promptInherited" : "decision.promptOverride", language)}${detail || sync.error ? " · " : ""}`}{sync.error ?? detail}</small></div>
    <div className="button-group">
      {sync.available && !sync.same && !loadedGlobal && <button className="quiet-button" onClick={onLoadGlobal}>{translate("settings.promptLoadGlobal", language)}</button>}
      {onRestore && <button className="quiet-button" disabled={inherited} onClick={onRestore}>{translate("decision.promptRestore", language)}</button>}
    </div>
  </div>;
}
