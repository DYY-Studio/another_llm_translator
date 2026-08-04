import { useEffect, useRef, useState, type RefObject } from "react";
import { api } from "../api";
import { useClassicSelection } from "../useClassicSelection";
import type { ProjectOverview } from "../types";
import { translate, type Language } from "../i18n";

type InputKind = "file" | "folder";

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
  file: File;
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
  const labels: Record<string, [string, string]> = {
    unknown: ["未知", "Unknown"],
    unavailable: ["不可用", "Unavailable"],
    removable: ["可移动磁盘", "Removable"],
    fixed: ["本地磁盘", "Fixed"],
    network: ["网络驱动器", "Network"],
    cdrom: ["光盘驱动器", "CD/DVD"],
    ramdisk: ["内存磁盘", "RAM disk"],
  };
  const label = labels[type] ?? [type, type];
  return label[language === "en" ? 1 : 0];
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
        setMessage(language === "en" ? `Unsupported input file: ${relative}` : `不支持的输入文件：${relative}`);
        return;
      }
      incoming.push({ file, path: relative, kind, adapterId });
    }
    if (!incoming.length) {
      setMessage(language === "en" ? "The selected folder has no supported input files" : "所选文件夹中没有受支持的输入文件");
      return;
    }
    const known = new Set(
      [...existingPaths, ...value.map((item) => item.path)]
        .map((path) => path.toLocaleLowerCase()),
    );
    for (const item of incoming) {
      const key = item.path.toLocaleLowerCase();
      if (known.has(key)) {
        setMessage(language === "en" ? `Duplicate input path; this selection was not added: ${item.path}` : `输入路径重名，本次选择未加入：${item.path}`);
        return;
      }
      known.add(key);
    }
    onChange([...value, ...incoming]);
    if (ignored.length) {
      setMessage(language === "en" ? `Ignored ${ignored.length} unsupported files` : `已忽略 ${ignored.length} 个不支持的文件`);
    }
  }

  function clearInput(ref: RefObject<HTMLInputElement | null>) {
    if (ref.current) ref.current.value = "";
  }

  return (
    <div className="input-queue">
      <div className="input-queue-heading">
        <div><strong>{language === "en" ? "Input queue" : "待输入列表"}</strong><small>{language === "en" ? "Add files or folders in multiple batches; folder paths stay relative." : "可分多次选择；文件夹导入保留内部相对路径。"}</small></div>
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
          <button type="button" className="quiet-button" disabled={disabled || !adapters.length} onClick={() => fileRef.current?.click()}>{language === "en" ? "Choose files" : "选择文件"}</button>
          <button type="button" className="quiet-button" disabled={disabled || !adapters.length || !folderSelectionSupported} onClick={() => folderRef.current?.click()}>{language === "en" ? "Choose folder" : "选择文件夹"}</button>
        </div>
      </div>
      {!folderSelectionSupported && <small className="muted">{language === "en" ? "This browser cannot select folders; individual files are still available." : "当前浏览器不支持文件夹选择，可继续选择单独文件。"}</small>}
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
            <small>{language === "en" ? "Applies to this import only; re-import existing files after changing it." : "仅用于本次导入；修改既有文件需重新导入。"}</small>
          </label>
        ))
        : [])}
      <div className="input-queue-list">
        {!value.length && <div className="input-queue-empty">{language === "en" ? "No files selected." : "尚未选择文件。"}</div>}
        {value.map((item, index) => (
          <div className="input-queue-row" key={`${item.path}-${index}`}>
            <span><strong>{item.path}</strong><small>{item.adapterId.toUpperCase()} · {item.kind === "folder" ? (language === "en" ? "Folder" : "文件夹") : (language === "en" ? "File" : "单独文件")}</small></span>
            <button type="button" className="danger-link" disabled={disabled} onClick={() => onChange(value.filter((_, itemIndex) => itemIndex !== index))}>{language === "en" ? "Remove" : "移除"}</button>
          </div>
        ))}
      </div>
    </div>
  );
}

export function Overview({
  project,
  value,
  onFilesChanged,
  onDeleted,
  language,
}: {
  project: string;
  value: ProjectOverview;
  onFilesChanged: () => Promise<void>;
  onDeleted: (path: string) => Promise<void>;
  language: Language;
}) {
  const completed = value.completed_segments;
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
        body.append("files", item.file, item.file.name);
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
      await onDeleted(value.path);
    } catch (value) {
      setError(String(value));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="page">
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
        <div><h2>{translate("overview.fileHeading", language)}</h2><p>{language === "en" ? "Each file keeps its source format." : "每个文件保留其来源格式"}</p></div>
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
            <h2>{language === "en" ? `Remove ${selection.selectedKeys.size} files?` : `移除 ${selection.selectedKeys.size} 个文件？`}</h2>
            <p>{language === "en" ? "The project copies and active Segments will be removed; historical stage results and existing outputs remain. Re-adding files assigns new File and Segment IDs." : "项目内源文件副本和活动 Segment 将被删除；历史阶段结果与既有输出文件会保留。以后重新添加会分配新的 File 与 Segment ID。"}</p>
            <div className="modal-actions">
              <button className="quiet-button" onClick={() => setRemoving(false)}>{language === "en" ? "Cancel" : "取消"}</button>
              <button className="danger-button" disabled={busy} onClick={() => void removeSelected()}>{language === "en" ? "Remove" : "确认移除"}</button>
            </div>
          </div>
        </div>
      )}
      {deleting && (
        <div className="modal-backdrop" onMouseDown={() => setDeleting(false)}>
          <div className="modal" role="dialog" aria-modal="true" aria-label={language === "en" ? "Delete project permanently" : "永久删除项目"} onMouseDown={(event) => event.stopPropagation()}>
            <h2>{language === "en" ? "Delete project permanently?" : "永久删除项目？"}</h2>
            <p>{language === "en" ? "This deletes the project directory, source files, Runs, term library, and stage results. It cannot be undone; confirm that nothing needs to be kept." : "将删除整个项目目录、源文件、Run、术语库和阶段结果，无法撤销。请确认项目中没有需要保留的数据。"}</p>
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
  const [result, setResult] = useState("");
  const selection = useClassicSelection();
  const fileIds = overview.files.map((item) => item.file_id);
  async function run() {
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
    setResult(JSON.stringify(value, null, 2));
  }
  return (
    <div className="page narrow-page">
      <div className="page-heading"><div><h1>{translate("export.title", language)}</h1><p>{translate("export.description", language)}</p></div></div>
      <label>{language === "en" ? "Result stage" : "结果阶段"}<select value={stage} onChange={(event) => setStage(event.target.value)}><option value="translated">{language === "en" ? "Translation" : "翻译"}</option><option value="proofread">{language === "en" ? "Applied proofreading" : "已应用校对"}</option><option value="polished">{language === "en" ? "Applied polishing" : "已应用润色"}</option></select></label>
      <label>{language === "en" ? "Output format" : "输出格式"}<select value={format} onChange={(event) => setFormat(event.target.value)}><option value="original">{language === "en" ? "Keep each file's format" : "保留各文件原格式"}</option><option value="txt">{language === "en" ? "Unified TXT" : "统一输出 TXT"}</option></select></label>
      <div className="export-file-heading">
        <div>
          <strong>{language === "en" ? "File scope" : "文件范围"}</strong>
          <small>{selection.selectedKeys.size ? (language === "en" ? `${selection.selectedKeys.size} files selected` : `已选择 ${selection.selectedKeys.size} 个文件`) : (language === "en" ? "All files when none are selected" : "未选择时导出全部文件")}</small>
        </div>
        <button className="quiet-button" disabled={!selection.selectedKeys.size} onClick={() => selection.reset()}>{language === "en" ? "Clear selection" : "清除选择"}</button>
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
      <label className="check-row"><input type="checkbox" checked={bilingual} onChange={(event) => setBilingual(event.target.checked)} /> {language === "en" ? "Generate bilingual output" : "生成双语对照"}</label>
      <button className="primary-button" onClick={run}>{language === "en" ? "Generate output" : "生成输出"}</button>
      {result && <pre className="result-box">{result}</pre>}
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
      body.append("files", item.file, item.file.name);
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
          <div className="dialog-tabs" role="tablist" aria-label={language === "en" ? "Project actions" : "项目操作"}>
            <button className={mode === "create" ? "active" : ""} onClick={() => setMode("create")}>{translate("dialog.new", language)}</button>
            <button className={mode === "open" ? "active" : ""} onClick={() => setMode("open")}>{translate("dialog.open", language)}</button>
          </div>
          {error && <div className="error-banner" role="alert">{error}</div>}
          {mode === "open" ? <>
            <label>{translate("dialog.projectPath", language)}<div className="path-picker-control"><input value={projectPath} onChange={(event) => setProjectPath(event.target.value)} placeholder="/path/to/project" /><button type="button" className="quiet-button" disabled={!projectPath.trim()} onClick={() => setDirectoryPickerMode("project")}>{translate("dialog.browse", language)}</button></div></label>
            <p className="muted">{language === "en" ? "Only this directory is opened; parent directories are not scanned." : "只打开此目录，不扫描父目录，也不会移动项目。"}</p>
            <div className="modal-actions"><button className="quiet-button" onClick={onClose}>{translate("dialog.cancel", language)}</button><button className="primary-button" disabled={!projectPath.trim()} onClick={open}>{translate("dialog.openProject", language)}</button></div>
          </> : <>
            <label>{translate("dialog.projectName", language)}<input value={name} onChange={(event) => setName(event.target.value)} /></label>
            <label>{translate("dialog.parentDir", language)}<div className="path-picker-control"><input value={parentDir} onChange={(event) => setParentDir(event.target.value)} /><button type="button" className="quiet-button" disabled={!parentDir.trim()} onClick={() => setDirectoryPickerMode("parent")}>{translate("dialog.browse", language)}</button></div></label>
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
