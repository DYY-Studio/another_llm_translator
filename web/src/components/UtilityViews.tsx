import { useEffect, useRef, useState, type RefObject } from "react";
import { api } from "../api";
import {
  nativeBridgeAvailable,
  pickNativeFile,
  pickNativeFolder,
  saveExport,
} from "../native";
import { useClassicSelection } from "../useClassicSelection";
import type { ProjectOverview, ProjectSummary } from "../types";
import { translate, type Language } from "../i18n";

type InputKind = "file" | "folder";

interface ExportFile {
  path: string;
  size: number;
  mtime: number;
}

interface AdapterSummary {
  adapter_id: string;
  capabilities: string[];
  extensions: string[];
  import_options: Array<{
    option_id: string;
    label: string;
    default: string;
    choices: Array<{ value: string; label: string }>;
  }>;
  run_options: Array<{
    option_id: string;
    label: string;
    default: string;
    choices: Array<{ value: string; label: string }>;
  }>;
}

type AdapterOptions = Record<string, Record<string, string>>;

interface PendingInput {
  file?: File;
  serverPath?: string;
  path: string;
  kind: InputKind;
  adapterId: string;
}

interface DirectoryEntry {
  name: string;
  path: string;
  is_project: boolean;
}

interface DriveEntry {
  name: string;
  path: string;
  type: string;
  available: boolean;
}

interface DirectoryListing {
  path: string;
  parent: string | null;
  is_project: boolean;
  directories: DirectoryEntry[];
  drives: DriveEntry[];
}

type DirectoryPickerMode = "parent" | "project";

function extensionOf(path: string) {
  const dot = path.lastIndexOf(".");
  return dot < 0 ? "" : path.slice(dot).toLocaleLowerCase();
}

function driveTypeLabel(type: string, language: Language) {
  const labels: Record<string, string> = {
    unknown: "drive.unknown",
    unavailable: "drive.unavailable",
    removable: "drive.removable",
    fixed: "drive.fixed",
    network: "drive.network",
    cdrom: "drive.cdrom",
    ramdisk: "drive.ramdisk",
  };
  const key = labels[type] ?? type;
  return translate(key, language);
}

function InputQueue({
  value,
  onChange,
  existingPaths = [],
  disabled = false,
  options,
  onOptionsChange,
  language,
}: {
  value: PendingInput[];
  onChange: (value: PendingInput[]) => void;
  existingPaths?: string[];
  disabled?: boolean;
  options: AdapterOptions;
  onOptionsChange: (value: AdapterOptions) => void;
  language: Language;
}) {
  const fileRef = useRef<HTMLInputElement>(null);
  const folderRef = useRef<HTMLInputElement>(null);
  const [adapters, setAdapters] = useState<AdapterSummary[]>([]);
  const [message, setMessage] = useState("");
  const folderSelectionSupported = "webkitdirectory" in document.createElement("input");

  useEffect(() => {
    folderRef.current?.setAttribute("webkitdirectory", "");
    void api<{ adapters: AdapterSummary[] }>("/api/v1/document-adapters")
      .then((result) => setAdapters(result.adapters))
      .catch((reason) => setMessage(String(reason)));
  }, []);

  const extensionOwners = new Map<string, string>();
  for (const adapter of adapters) {
    if (!adapter.capabilities.includes("import")) continue;
    for (const extension of adapter.extensions) {
      extensionOwners.set(extension.toLocaleLowerCase(), adapter.adapter_id);
    }
  }
  const accepted = [...extensionOwners.keys()].join(",");
  const queuedAdapters = new Set(value.map((item) => item.adapterId));

  function addBatch(files: FileList | null, kind: InputKind) {
    if (!files?.length) return;
    setMessage("");
    const incoming: PendingInput[] = [];
    const ignored: string[] = [];
    for (const file of Array.from(files)) {
      const relative = kind === "folder"
        ? (file.webkitRelativePath || file.name).split("/").slice(1).join("/") || file.name
        : file.name;
      const adapterId = extensionOwners.get(extensionOf(relative));
      if (!adapterId) {
        if (kind === "folder") {
          ignored.push(relative);
          continue;
        }
        setMessage(translate("inputQueue.unsupported", language, { path: relative }));
        return;
      }
      incoming.push({ file, path: relative, kind, adapterId });
    }
    if (!incoming.length) {
      setMessage(translate("inputQueue.noSupported", language));
      return;
    }
    const known = new Set(
      [...existingPaths, ...value.map((item) => item.path)]
        .map((path) => path.toLocaleLowerCase()),
    );
    for (const item of incoming) {
      const key = item.path.toLocaleLowerCase();
      if (known.has(key)) {
        setMessage(translate("inputQueue.duplicate", language, { path: item.path }));
        return;
      }
      known.add(key);
    }
    onChange([...value, ...incoming]);
    if (ignored.length) {
      setMessage(translate("inputQueue.ignored", language, { count: ignored.length }));
    }
  }

  function clearInput(ref: RefObject<HTMLInputElement | null>) {
    if (ref.current) ref.current.value = "";
  }

  async function addNativeBatch(kind: InputKind) {
    const picked = kind === "file"
      ? await pickNativeFile()
      : await pickNativeFolder();
    if (!picked) return;
    setMessage("");
    const adapterId = extensionOwners.get(extensionOf(picked));
    if (!adapterId) {
      setMessage(translate("inputQueue.unsupported", language, { path: picked }));
      return;
    }
    const relative = kind === "file"
      ? (picked.split(/[\\/]/).pop() ?? picked)
      : picked;
    const known = new Set(
      [...existingPaths, ...value.map((item) => item.path)]
        .map((path) => path.toLocaleLowerCase()),
    );
    if (known.has(relative.toLocaleLowerCase())) {
      setMessage(translate("inputQueue.duplicate", language, { path: relative }));
      return;
    }
    onChange([...value, { serverPath: picked, path: relative, kind, adapterId }]);
  }

  return (
    <div className="input-queue">
      <div className="input-queue-heading">
        <div><strong>{translate("inputQueue.title", language)}</strong><small>{translate("inputQueue.hint", language)}</small></div>
        <div className="button-group">
          <input
            ref={fileRef}
            hidden
            type="file"
            accept={accepted}
            multiple
            onChange={(event) => {
              addBatch(event.target.files, "file");
              clearInput(fileRef);
            }}
          />
          <input
            ref={folderRef}
            hidden
            type="file"
            accept={accepted}
            multiple
            onChange={(event) => {
              addBatch(event.target.files, "folder");
              clearInput(folderRef);
            }}
          />
          <button type="button" className="quiet-button" disabled={disabled || !adapters.length} onClick={() => { if (nativeBridgeAvailable()) void addNativeBatch("file"); else fileRef.current?.click(); }}>{translate("inputQueue.chooseFiles", language)}</button>
          <button type="button" className="quiet-button" disabled={disabled || !adapters.length || (!nativeBridgeAvailable() && !folderSelectionSupported)} onClick={() => { if (nativeBridgeAvailable()) void addNativeBatch("folder"); else folderRef.current?.click(); }}>{translate("inputQueue.chooseFolder", language)}</button>
        </div>
      </div>
      {!folderSelectionSupported && <small className="muted">{translate("inputQueue.noFolderSupport", language)}</small>}
      {message && <button type="button" className="input-queue-message" onClick={() => setMessage("")}>{message}</button>}
      {adapters.flatMap((adapter) => queuedAdapters.has(adapter.adapter_id)
        ? [...adapter.import_options, ...adapter.run_options].map((option) => (
          <label className="input-queue-option" key={`${adapter.adapter_id}.${option.option_id}`}>
            {option.label}
            <select
              disabled={disabled}
              value={options[adapter.adapter_id]?.[option.option_id] ?? option.default}
              onChange={(event) => onOptionsChange({
                ...options,
                [adapter.adapter_id]: {
                  ...options[adapter.adapter_id],
                  [option.option_id]: event.target.value,
                },
              })}
            >
              {option.choices.map((choice) => <option value={choice.value} key={choice.value}>{choice.label}</option>)}
            </select>
            <small>{translate("inputQueue.optionHint", language)}</small>
          </label>
        ))
        : [])}
      <div className="input-queue-list">
        {!value.length && <div className="input-queue-empty">{translate("inputQueue.empty", language)}</div>}
        {value.map((item, index) => (
          <div className="input-queue-row" key={`${item.path}-${index}`}>
            <span><strong>{item.path}</strong><small>{item.adapterId.toUpperCase()} · {item.kind === "folder" ? translate("inputQueue.folder", language) : translate("inputQueue.file", language)}</small></span>
            <button type="button" className="danger-link" disabled={disabled} onClick={() => onChange(value.filter((_, itemIndex) => itemIndex !== index))}>{translate("common.remove", language)}</button>
          </div>
        ))}
      </div>
    </div>
  );
}

function ProjectBar({
  projects,
  project,
  onProject,
  onCreate,
  language,
}: {
  projects: ProjectSummary[];
  project: string;
  onProject: (value: string) => void;
  onCreate: () => void;
  language: Language;
}) {
  return (
    <div className="overview-project-bar">
      <select value={project} onChange={(event) => onProject(event.target.value)} aria-label={translate("project.select", language)}>
        <option value="">{translate("project.select", language)}</option>
        {projects.map((item) => <option key={item.selector} value={item.selector}>{item.external ? `${item.name} · ${item.path}` : item.name}</option>)}
      </select>
      <button className="quiet-button" onClick={onCreate}>{translate("project.create", language)}</button>
    </div>
  );
}

export function Overview({
  projects,
  project,
  value,
  onProject,
  onCreate,
  onFilesChanged,
  onDeleted,
  language,
}: {
  projects: ProjectSummary[];
  project: string;
  value: ProjectOverview | null;
  onProject: (value: string) => void;
  onCreate: () => void;
  onFilesChanged: () => Promise<void>;
  onDeleted: (path: string) => Promise<void>;
  language: Language;
}) {
  if (!value) {
    return (
      <div className="page">
        <ProjectBar projects={projects} project={project} onProject={onProject} onCreate={onCreate} language={language} />
        <p className="overview-empty-hint">{translate("app.selectOrCreate", language)}</p>
      </div>
    );
  }
  const completed = value.completed_segments;
  const projectPath = value.path;
  const selection = useClassicSelection();
  const [pendingInputs, setPendingInputs] = useState<PendingInput[]>([]);
  const [adapterOptions, setAdapterOptions] = useState<AdapterOptions>({});
  const [removing, setRemoving] = useState(false);
  const [deleting, setDeleting] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const fileIds = value.files.map((item) => item.file_id);

  async function upload() {
    if (!pendingInputs.length) return;
    setBusy(true);
    setError("");
    try {
      const body = new FormData();
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
      const result = await api<{ warnings: string[] }>(`/api/v1/projects/${project}/files`, {
        method: "POST",
        body,
      });
      setPendingInputs([]);
      setAdapterOptions({});
      if (result.warnings.length) setError(result.warnings.join("；"));
      selection.reset();
      await onFilesChanged();
    } catch (value) {
      setError(String(value));
    } finally {
      setBusy(false);
    }
  }

  async function removeSelected() {
    setBusy(true);
    setError("");
    try {
      await api(`/api/v1/projects/${project}/files/remove`, {
        method: "POST",
        body: JSON.stringify({ file_ids: [...selection.selectedKeys] }),
      });
      selection.reset();
      setRemoving(false);
      await onFilesChanged();
    } catch (value) {
      setError(String(value));
    } finally {
      setBusy(false);
    }
  }

  async function deleteProject() {
    setBusy(true);
    setError("");
    try {
      await api(`/api/v1/projects/${project}`, {
        method: "DELETE",
        body: JSON.stringify({ confirm: true }),
      });
      setDeleting(false);
      await onDeleted(projectPath);
    } catch (reason) {
      setError(String(reason));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="page">
      <ProjectBar projects={projects} project={project} onProject={onProject} onCreate={onCreate} language={language} />
      <div className="page-heading overview-heading">
        <div><h1>{value.name}</h1><p>{value.path}</p></div>
        <button className="danger-button" disabled={busy} onClick={() => setDeleting(true)}>{translate("overview.delete", language)}</button>
      </div>
      <div className="summary-strip">
        <div><strong>{value.files.length}</strong><span>{translate("overview.files", language)}</span></div>
        <div><strong>{value.nonempty_segment_count}</strong><span>{translate("overview.nonempty", language)}</span></div>
        <div><strong>{completed}</strong><span>{translate("overview.translated", language)}</span></div>
      </div>
      <div className="section-heading">
        <div><h2>{translate("overview.fileHeading", language)}</h2><p>{translate("overview.fileHint", language)}</p></div>
        <div className="section-actions">
          <button className="primary-button" disabled={busy || !pendingInputs.length} onClick={() => void upload()}>
            {translate("overview.add", language)}
          </button>
          <button className="danger-button" disabled={busy || selection.selectedKeys.size === 0} onClick={() => setRemoving(true)}>
            {translate("overview.remove", language)}
          </button>
        </div>
      </div>
      <InputQueue value={pendingInputs} onChange={setPendingInputs} existingPaths={value.files.map((item) => item.name)} disabled={busy} options={adapterOptions} onOptionsChange={setAdapterOptions} language={language} />
      {error && <button className="error-banner" onClick={() => setError("")}>{error}</button>}
      <div className="file-list">
        {value.files.length === 0 && (
          <div className="empty-file-state">
            <strong>{translate("overview.noFiles", language)}</strong>
            <span>{translate("overview.addHint", language)}</span>
          </div>
        )}
        {value.files.map((item) => (
          <button
            type="button"
            key={item.file_id}
            className={`file-row${selection.selectedKeys.has(item.file_id) ? " selected" : ""}`}
            onClick={(event) => selection.select(item.file_id, fileIds, event)}
          >
            <span>{item.file_id}</span><strong>{item.name}</strong><small>{item.document_adapter_id.toUpperCase()}</small>
          </button>
        ))}
      </div>
      {removing && (
        <div className="modal-backdrop" onMouseDown={() => setRemoving(false)}>
          <div className="modal" onMouseDown={(event) => event.stopPropagation()}>
            <h2>{translate("overview.removeFiles", language, { count: selection.selectedKeys.size })}</h2>
            <p>{translate("overview.removeFilesHint", language)}</p>
            <div className="modal-actions">
              <button className="quiet-button" onClick={() => setRemoving(false)}>{translate("common.cancel", language)}</button>
              <button className="danger-button" disabled={busy} onClick={() => void removeSelected()}>{translate("overview.confirmRemove", language)}</button>
            </div>
          </div>
        </div>
      )}
      {deleting && (
        <div className="modal-backdrop" onMouseDown={() => setDeleting(false)}>
          <div className="modal" role="dialog" aria-modal="true" aria-label={translate("overview.deleteTitle", language)} onMouseDown={(event) => event.stopPropagation()}>
            <h2>{translate("overview.deleteTitle", language)}?</h2>
            <p>{translate("overview.deleteHint", language)}</p>
            {error && <p className="error-text">{error}</p>}
            <div className="modal-actions">
              <button className="quiet-button" disabled={busy} onClick={() => setDeleting(false)}>{translate("dialog.cancel", language)}</button>
              <button className="danger-button" disabled={busy} onClick={() => void deleteProject()}>{translate("overview.confirmDelete", language)}</button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}

function formatSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

function formatTime(seconds: number): string {
  return new Date(seconds * 1000).toLocaleString();
}

export function ExportView({
  project,
  overview,
  language,
}: {
  project: string;
  overview: ProjectOverview;
  language: Language;
}) {
  const [stage, setStage] = useState("translated");
  const [format, setFormat] = useState("original");
  const [bilingual, setBilingual] = useState(false);
  const [error, setError] = useState("");
  const [message, setMessage] = useState("");
  const [files, setFiles] = useState<ExportFile[]>([]);
  const [highlighted, setHighlighted] = useState<string[]>([]);
  const selection = useClassicSelection();
  const fileIds = overview.files.map((item) => item.file_id);
  const native = nativeBridgeAvailable();

  async function refresh() {
    const value = await api<{ files: ExportFile[] }>(
      `/api/v1/projects/${project}/exports`,
    );
    setFiles(value.files);
  }

  useEffect(() => {
    void refresh().catch((reason) => setError(String(reason)));
  }, [project]);

  async function run() {
    setError(""); setMessage(""); setHighlighted([]);
    try {
      const value = await api<Record<string, unknown>>(`/api/v1/projects/${project}/export`, {
        method: "POST",
        body: JSON.stringify({
          stage,
          format,
          bilingual,
          allow_missing: false,
          file_ids: selection.selectedKeys.size
            ? [...selection.selectedKeys]
            : null,
        }),
      });
      await refresh();
      const written = Array.isArray(value.written)
        ? value.written.map(String).map((item) => item.replace(/^output\//, ""))
        : [];
      setHighlighted(written);
      if (written.length) {
        setMessage(translate("export.generated", language, { count: written.length }));
      }
    } catch (reason) {
      setError(String(reason));
    }
  }

  function downloadHref(path: string): string {
    return `/api/v1/projects/${project}/exports/download?file=${encodeURIComponent(path)}`;
  }

  async function saveViaNative(url: string, filename: string) {
    setError("");
    try {
      const saved = await saveExport(url, filename);
      if (saved) setMessage(translate("export.savedTo", language, { path: saved }));
    } catch (reason) {
      setError(String(reason));
    }
  }

  function download(path: string) {
    const filename = path.split("/").pop() || "export";
    void saveViaNative(downloadHref(path), filename);
  }

  function downloadAll() {
    if (!files.length) return;
    const url = `/api/v1/projects/${project}/exports/download-all?${files
      .map((item) => `file=${encodeURIComponent(item.path)}`)
      .join("&")}`;
    if (!native) {
      const anchor = document.createElement("a");
      anchor.href = url;
      document.body.appendChild(anchor);
      anchor.click();
      anchor.remove();
      return;
    }
    void saveViaNative(url, `${project}-exports.zip`);
  }

  async function removeFile(path: string) {
    if (!window.confirm(translate("export.deleteConfirm", language, { path }))) {
      return;
    }
    setError(""); setMessage("");
    try {
      await api(`/api/v1/projects/${project}/exports/remove`, {
        method: "POST",
        body: JSON.stringify({ files: [path] }),
      });
      await refresh();
    } catch (reason) {
      setError(String(reason));
    }
  }

  return (
    <div className="page narrow-page">
      <div className="page-heading"><div><h1>{translate("export.title", language)}</h1><p>{translate("export.description", language)}</p></div></div>
      <label>{translate("export.resultStage", language)}<select value={stage} onChange={(event) => setStage(event.target.value)}><option value="translated">{translate("stage.translation", language)}</option><option value="proofread">{translate("export.proofread", language)}</option><option value="polished">{translate("export.polished", language)}</option></select></label>
      <label>{translate("export.format", language)}<select value={format} onChange={(event) => setFormat(event.target.value)}><option value="original">{translate("export.keepFormat", language)}</option><option value="txt">{translate("export.txt", language)}</option></select></label>
      <div className="export-file-heading">
        <div>
          <strong>{translate("export.fileScope", language)}</strong>
          <small>{selection.selectedKeys.size ? translate("export.selected", language, { count: selection.selectedKeys.size }) : translate("export.allFiles", language)}</small>
        </div>
        <button className="quiet-button" disabled={!selection.selectedKeys.size} onClick={() => selection.reset()}>{translate("export.clearSelection", language)}</button>
      </div>
      <div className="file-list export-file-list">
        {overview.files.map((item) => (
          <button
            type="button"
            key={item.file_id}
            className={`file-row${selection.selectedKeys.has(item.file_id) ? " selected" : ""}`}
            onClick={(event) => selection.select(item.file_id, fileIds, event)}
          >
            <span>{item.file_id}</span><strong>{item.name}</strong><small>{item.document_adapter_id.toUpperCase()}</small>
          </button>
        ))}
      </div>
      <label className="check-row"><input type="checkbox" checked={bilingual} onChange={(event) => setBilingual(event.target.checked)} /> {translate("export.bilingual", language)}</label>
      <button className="primary-button" onClick={() => void run()}>{translate("export.generate", language)}</button>
      <div className="export-file-heading">
        <div>
          <strong>{translate("export.outputFiles", language)}</strong>
          <small>{translate("export.outputCount", language, { count: files.length })}</small>
        </div>
        <button className="quiet-button" disabled={!files.length} onClick={downloadAll}>{translate("export.downloadAll", language)}</button>
      </div>
      <div className="file-list export-file-list">
        {!files.length && <p className="export-empty">{translate("export.noFiles", language)}</p>}
        {files.map((item) => (
          <div key={item.path} className={`file-row export-row${highlighted.includes(item.path) ? " selected" : ""}`}>
            <strong>{item.path}</strong>
            <small>{formatSize(item.size)} · {formatTime(item.mtime)}</small>
            <span className="export-actions">
              {native
                ? <button className="quiet-button" onClick={() => download(item.path)}>{translate("export.download", language)}</button>
                : <a className="quiet-button" href={downloadHref(item.path)}>{translate("export.download", language)}</a>}
              <button className="quiet-button" onClick={() => void removeFile(item.path)}>{translate("export.delete", language)}</button>
            </span>
          </div>
        ))}
      </div>
      {message && <p className="notice-box">{message}</p>}
      {error && <button className="error-banner" onClick={() => setError("")}>{error}</button>}
    </div>
  );
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
      .catch((reason) => setError(String(reason)));
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
      setError(String(reason));
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
      setError(String(reason));
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
      const query = normalized ? `?path=${encodeURIComponent(normalized)}` : "";
      const result = await api<DirectoryListing>(`/api/v1/directories${query}`);
      if (revision !== requestRevision.current) return;
      setListing(result);
    } catch (reason) {
      if (revision === requestRevision.current) {
        setError(reason instanceof Error ? reason.message : String(reason));
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
