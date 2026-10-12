import { translate, type Language } from "../i18n";
import type { PromptLibraryEntry } from "../types";

export function PromptLibraryControls({ language, hint, entries, selected, loading, saveOpen, id, overwrite, onSelect, onDelete, onId, onCancel, onSave }: {
  language: Language; hint?: string; entries: PromptLibraryEntry[]; selected: string; loading: boolean; saveOpen: boolean; id: string; overwrite: boolean;
  onSelect: (id: string) => void; onDelete: () => void; onId: (id: string) => void; onCancel: () => void; onSave: () => void;
}) {
  return <div className="prompt-library-card">
    <div><strong>{translate("settings.promptLibraryTitle", language)}</strong><small>{hint ?? translate("settings.promptLibraryHint", language)}</small></div>
    {saveOpen && <div className="prompt-library-save-form">
      <input aria-label={translate("settings.promptLibraryNewId", language)} value={id} onChange={(event) => onId(event.target.value)} placeholder="custom-rules" />
      {overwrite && <small>{translate("settings.promptLibraryOverwriteConfirm", language, { id })}</small>}
      <div className="button-group">
        <button className="quiet-button" onClick={onCancel}>{translate("common.cancel", language)}</button>
        <button className="primary-button" onClick={onSave}>{translate(overwrite ? "settings.promptLibraryConfirmOverwrite" : "common.save", language)}</button>
      </div>
    </div>}
    <div className="prompt-library-controls">
      <select value={selected} disabled={loading || !entries.length} onChange={(event) => onSelect(event.target.value)}>
        <option value="">{translate(loading ? "settings.promptLibraryLoading" : "settings.promptLibrarySelect", language)}</option>
        {entries.map((item) => <option key={item.id} value={item.id}>{item.id}</option>)}
      </select>
      <button className="quiet-button" disabled={!selected} onClick={onDelete}>{translate("common.delete", language)}</button>
    </div>
  </div>;
}
