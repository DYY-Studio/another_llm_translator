import { useEffect, useRef, useState, useId, type DragEvent as ReactDragEvent, type KeyboardEvent as ReactKeyboardEvent, type RefObject } from "react";
import { api, apiErrorFromResponse, errorPayloadFrom } from "../api";
import { DirectoryPicker } from "./DirectoryPicker";
import { nativeBridgeAvailable, pickNativeFile, pickNativeFolder, saveExport } from "../native";
import { moveFileBlock, moveFilesByCommand, type DropPosition, type FileMoveCommand } from "../fileOrder";
import { useClassicSelection } from "../useClassicSelection";
import { errorMessage, formatErrorPayload, translate, type Language } from "../i18n";
import type { AdapterOptions, PendingInput } from "./ProjectInputs";
import { InputQueue } from "./ProjectInputs";

export function CreateProjectDialog({ onClose, onCreated, language }: { onClose: () => void; onCreated: (selector: string, externalPath?: string) => void; language: Language }) {
  const [mode, setMode] = useState<"create" | "open">("create");
  const [name, setName] = useState("");
  const [parentDir, setParentDir] = useState("");
  const [projectPath, setProjectPath] = useState("");
  const [pendingInputs, setPendingInputs] = useState<PendingInput[]>([]);
  const [adapterOptions, setAdapterOptions] = useState<AdapterOptions>({});
  const [error, setError] = useState("");
  const [directoryPickerMode, setDirectoryPickerMode] = useState<"parent" | "project" | null>(null);
  useEffect(() => {
    void api<{ default_projects_path: string }>("/api/v1/projects")
      .then((value) => {
        setParentDir(value.default_projects_path);
        setProjectPath(value.default_projects_path);
      })
      .catch((reason) => setError(errorMessage(reason, language)));
  }, []);
  async function submit() {
    const body = new FormData();
    body.append("name", name);
    body.append("empty", String(pendingInputs.length === 0));
    body.append("parent_dir", parentDir.trim());
    for (const item of pendingInputs) {
      if (item.serverPath) {
        body.append("server_paths", item.serverPath);
        body.append("server_input_kinds", item.kind);
        continue;
      }
      if (item.file) {
        body.append("files", item.file, item.file.name);
      }
      body.append("relative_paths", item.path);
      body.append("input_kinds", item.kind);
    }
    body.append("adapter_options", JSON.stringify(adapterOptions));
    try {
      const result = await api<{ project_selector: string; project_path: string; external: boolean }>("/api/v1/projects", { method: "POST", body });
      onCreated(result.project_selector, result.external ? result.project_path : undefined);
    } catch (reason) {
      setError(errorMessage(reason, language));
    }
  }
  async function open() {
    try {
      const result = await api<{ selector: string; path: string; external: boolean }>("/api/v1/projects/register", {
        method: "POST",
        body: JSON.stringify({ path: projectPath.trim() }),
      });
      onCreated(result.selector, result.external ? result.path : undefined);
    } catch (reason) {
      setError(errorMessage(reason, language));
    }
  }
  return (
    <>
      <div className="modal-backdrop" onMouseDown={onClose}>
        <div className="modal" onMouseDown={(event) => event.stopPropagation()}>
          <div className="dialog-tabs" role="tablist" aria-label={translate("dialog.projectActions", language)}>
            <button className={mode === "create" ? "active" : ""} onClick={() => setMode("create")}>{translate("dialog.new", language)}</button>
            <button className={mode === "open" ? "active" : ""} onClick={() => setMode("open")}>{translate("dialog.open", language)}</button>
          </div>
          {error && <div className="error-banner" role="alert">{error}</div>}
          {mode === "open" ? <>
            <label>{translate("dialog.projectPath", language)}<div className="path-picker-control"><input value={projectPath} onChange={(event) => setProjectPath(event.target.value)} placeholder="/path/to/project" /><button type="button" className="quiet-button" disabled={!projectPath.trim()} onClick={() => { if (nativeBridgeAvailable()) { void pickNativeFolder().then((path) => { if (path) setProjectPath(path); }); } else { setDirectoryPickerMode("project"); } }}>{translate("dialog.browse", language)}</button></div></label>
            <p className="muted">{translate("dialog.openHint", language)}</p>
            <div className="modal-actions"><button className="quiet-button" onClick={onClose}>{translate("dialog.cancel", language)}</button><button className="primary-button" disabled={!projectPath.trim()} onClick={open}>{translate("dialog.openProject", language)}</button></div>
          </> : <>
            <label>{translate("dialog.projectName", language)}<input value={name} onChange={(event) => setName(event.target.value)} /></label>
            <label>{translate("dialog.parentDir", language)}<div className="path-picker-control"><input value={parentDir} onChange={(event) => setParentDir(event.target.value)} /><button type="button" className="quiet-button" disabled={!parentDir.trim()} onClick={() => { if (nativeBridgeAvailable()) { void pickNativeFolder().then((path) => { if (path) setParentDir(path); }); } else { setDirectoryPickerMode("parent"); } }}>{translate("dialog.browse", language)}</button></div></label>
            <InputQueue value={pendingInputs} onChange={setPendingInputs} options={adapterOptions} onOptionsChange={setAdapterOptions} language={language} />
            <p className="muted">{translate("dialog.emptyHint", language)}</p>
            <div className="modal-actions"><button className="quiet-button" onClick={onClose}>{translate("dialog.cancel", language)}</button><button className="primary-button" disabled={!name.trim() || !parentDir.trim()} onClick={submit}>{translate("dialog.createProject", language)}</button></div>
          </>}
        </div>
      </div>
      {directoryPickerMode && <DirectoryPicker
        initialPath={directoryPickerMode === "parent" ? parentDir : projectPath}
        mode={directoryPickerMode}
        language={language}
        onClose={() => setDirectoryPickerMode(null)}
        onSelect={(path) => {
          if (directoryPickerMode === "parent") setParentDir(path);
          else setProjectPath(path);
          setDirectoryPickerMode(null);
        }}
      />}
    </>
  );
}
