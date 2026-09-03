import { useEffect, useRef, useState, useId, type DragEvent as ReactDragEvent, type KeyboardEvent as ReactKeyboardEvent, type RefObject } from "react";
import { api, apiErrorFromResponse, errorPayloadFrom } from "../api";
import { nativeBridgeAvailable, pickNativeFile, pickNativeFolder, saveExport } from "../native";
import { moveFileBlock, moveFilesByCommand, type DropPosition, type FileMoveCommand } from "../fileOrder";
import { useClassicSelection } from "../useClassicSelection";
import { errorMessage, formatErrorPayload, translate, type Language } from "../i18n";
import type { AdapterOptions, PendingInput } from "./ProjectInputs";
import { InputQueue } from "./ProjectInputs";

type DirectoryPickerMode = "parent" | "project";
interface DirectoryEntry { name: string; path: string; is_project: boolean; }
interface DriveEntry { name: string; path: string; type: string; available: boolean; }
interface DirectoryListing { path: string; parent: string | null; is_project: boolean; directories: DirectoryEntry[]; drives: DriveEntry[]; }
function driveTypeLabel(type: string, language: Language) {
  const labels: Record<string, string> = { unknown: "drive.unknown", unavailable: "drive.unavailable", removable: "drive.removable", fixed: "drive.fixed", network: "drive.network", cdrom: "drive.cdrom", ramdisk: "drive.ramdisk" };
  return translate(labels[type] ?? type, language);
}

export function CreateProjectDialog({ onClose, onCreated, language }: { onClose: () => void; onCreated: (selector: string, externalPath?: string) => void; language: Language }) {
  const [mode, setMode] = useState<"create" | "open">("create");
  const [name, setName] = useState("");
  const [parentDir, setParentDir] = useState("");
  const [projectPath, setProjectPath] = useState("");
  const [pendingInputs, setPendingInputs] = useState<PendingInput[]>([]);
  const [adapterOptions, setAdapterOptions] = useState<AdapterOptions>({});
  const [error, setError] = useState("");
  const [directoryPickerMode, setDirectoryPickerMode] = useState<DirectoryPickerMode | null>(null);
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
      const result = await api<{ selector: string; path: string; external: boolean }>("/api/v1/projects/open", {
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

function DirectoryPicker({
  initialPath,
  mode,
  language,
  onClose,
  onSelect,
}: {
  initialPath: string;
  mode: DirectoryPickerMode;
  language: Language;
  onClose: () => void;
  onSelect: (path: string) => void;
}) {
  const [listing, setListing] = useState<DirectoryListing | null>(null);
  const [requestedPath, setRequestedPath] = useState(initialPath);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const requestRevision = useRef(0);

  async function load(path: string) {
    const revision = ++requestRevision.current;
    const normalized = path.trim();
    setRequestedPath(normalized);
    setListing(null);
    setLoading(true);
    setError("");
    try {
      const result = await api<DirectoryListing>("/api/v1/directories", {
        method: "POST",
        body: JSON.stringify({ path: normalized }),
      });
      if (revision !== requestRevision.current) return;
      setListing(result);
    } catch (reason) {
      if (revision === requestRevision.current) {
        setError(errorMessage(reason, language));
      }
    } finally {
      if (revision === requestRevision.current) setLoading(false);
    }
  }

  useEffect(() => {
    void load(initialPath);
    return () => { requestRevision.current += 1; };
  }, [initialPath]);

  const canSelect = Boolean(listing) && (mode === "parent" || listing?.is_project === true);
  return (
    <div className="modal-backdrop directory-picker-backdrop" onMouseDown={onClose}>
      <div className="modal directory-picker-modal" role="dialog" aria-modal="true" aria-labelledby="directory-picker-title" onMouseDown={(event) => event.stopPropagation()}>
        <div className="directory-picker-heading">
          <div>
            <h2 id="directory-picker-title">{translate("directory.title", language)}</h2>
            <p><span>{translate("directory.current", language)}：</span><code>{listing?.path ?? requestedPath}</code></p>
          </div>
          <button type="button" className="quiet-button" onClick={onClose}>{translate("dialog.cancel", language)}</button>
        </div>
        {error && <div className="error-banner" role="alert">{error}</div>}
        <div className="directory-picker-toolbar">
          {listing?.drives.length ? <span>{translate("directory.drives", language)}</span> : <span />}
          <button type="button" className="quiet-button" disabled={loading} onClick={() => void load(requestedPath)}>{translate("directory.refresh", language)}</button>
        </div>
        <div className="directory-list" aria-live="polite">
          {listing?.drives.map((entry) => (
            <button
              type="button"
              className={`directory-entry directory-drive${entry.available ? "" : " unavailable"}`}
              disabled={loading || !entry.available}
              key={entry.path}
              onClick={() => void load(entry.path)}
            >
              <strong>{entry.name}</strong>
              <small>{driveTypeLabel(entry.type, language)} · {entry.available ? translate("directory.available", language) : translate("directory.unavailable", language)}</small>
            </button>
          ))}
          {listing?.parent && <button type="button" className="directory-entry directory-parent" disabled={loading} onClick={() => void load(listing.parent as string)}><strong>{translate("directory.up", language)}</strong><small>{listing.parent}</small></button>}
          {listing?.directories.map((entry) => (
            <button type="button" className="directory-entry" disabled={loading} key={entry.path} onClick={() => void load(entry.path)}>
              <strong>{entry.name}</strong>
              <small>{entry.is_project ? translate("directory.project", language) : ""}</small>
            </button>
          ))}
          {loading && <div className="directory-list-state">{translate("directory.loading", language)}</div>}
          {!loading && !error && listing && !listing.directories.length && !listing.drives.length && <div className="directory-list-state">{translate("directory.empty", language)}</div>}
        </div>
        {mode === "project" && !listing?.is_project && !loading && !error && <p className="error-text">{translate("directory.notProject", language)}</p>}
        <div className="modal-actions">
          <button type="button" className="primary-button" disabled={!canSelect || loading} onClick={() => { if (listing) onSelect(listing.path); }}>{translate("directory.select", language)}</button>
        </div>
      </div>
    </div>
  );
}
