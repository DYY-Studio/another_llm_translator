import {
  useId,
  useEffect,
  useRef,
  useState,
  type DragEvent as ReactDragEvent,
  type KeyboardEvent as ReactKeyboardEvent,
  type RefObject,
} from "react";
import { api, apiErrorFromResponse, errorPayloadFrom } from "../api";
import {
  nativeBridgeAvailable,
  pickNativeFile,
  pickNativeFolder,
  saveExport,
} from "../native";
import {
  moveFileBlock,
  moveFilesByCommand,
  type DropPosition,
  type FileMoveCommand,
} from "../fileOrder";
import { useClassicSelection } from "../useClassicSelection";
import type {
  ErrorPayload,
  ProjectOverview,
  ProjectSummary,
  SettingsField,
  Stage,
} from "../types";
import {
  errorMessage,
  formatErrorPayload,
  translate,
  type Language,
} from "../i18n";

type InputKind = "file" | "folder";

interface ExportFile {
  path: string;
  size: number;
  mtime: number;
}

const EXPORT_SETTINGS_FIELDS: Partial<Record<string, SettingsField>> = {
  missing_target_language_tag: "target_language_tag",
  missing_target_language: "target_language",
  unrepresentable_output_encoding: "output_encoding",
};

const EXPORT_RESULT_STAGES: Partial<Record<string, Stage>> = {
  translated: "translation",
  proofread: "proofreading",
  polished: "polishing",
};

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
type ProjectFile = ProjectOverview["files"][number];

interface OptimisticFileOrder {
  project: string;
  before: string[];
  after: string[];
}

interface ButtonReorderState {
  project: string;
}

interface ReplacementImpact {
  file_id: string;
  old_segment_count: number;
  new_segment_count: number;
  preserved_segment_count: number;
  added_segment_count: number;
  removed_segment_count: number;
  ambiguous_old_segment_count: number;
  ambiguous_new_segment_count: number;
  preserved_completed_by_stage: Record<string, number>;
  removed_completed_by_stage: Record<string, number>;
  warnings: string[];
  previous_adapter_options: Record<string, string>;
  replacement_adapter_options: Record<string, string>;
  changed_adapter_options: string[];
}

interface ReplacementOptionsResponse {
  adapter: AdapterSummary;
  values: Record<string, string>;
}

interface ReplacementSource {
  file?: File;
  serverPath?: string;
  label: string;
}

const NATURAL_NUMBER = /^[0-9]+$/;
const NATURAL_PARTS = /([0-9]+)/;

function extensionOf(path: string) {
  const dot = path.lastIndexOf(".");
  return dot < 0 ? "" : path.slice(dot).toLocaleLowerCase();
}

function compareText(left: string, right: string) {
  const leftPoints = Array.from(left, (value) => value.codePointAt(0) ?? 0);
  const rightPoints = Array.from(right, (value) => value.codePointAt(0) ?? 0);
  const length = Math.min(leftPoints.length, rightPoints.length);
  for (let index = 0; index < length; index += 1) {
    if (leftPoints[index] !== rightPoints[index]) {
      return leftPoints[index] - rightPoints[index];
    }
  }
  return leftPoints.length - rightPoints.length;
}

function compareNaturalPaths(left: string, right: string) {
  const leftFolded = left.toLowerCase();
  const rightFolded = right.toLowerCase();
  const leftParts = leftFolded.split(NATURAL_PARTS);
  const rightParts = rightFolded.split(NATURAL_PARTS);
  const length = Math.min(leftParts.length, rightParts.length);
  for (let index = 0; index < length; index += 1) {
    const leftPart = leftParts[index];
    const rightPart = rightParts[index];
    const leftIsNumber = NATURAL_NUMBER.test(leftPart);
    const rightIsNumber = NATURAL_NUMBER.test(rightPart);
    if (leftIsNumber && rightIsNumber) {
      const difference = BigInt(leftPart) - BigInt(rightPart);
      if (difference !== 0n) return difference < 0n ? -1 : 1;
      continue;
    }
    if (leftIsNumber !== rightIsNumber) return leftIsNumber ? -1 : 1;
    const comparison = compareText(leftPart, rightPart);
    if (comparison) return comparison;
  }
  if (leftParts.length !== rightParts.length) {
    return leftParts.length - rightParts.length;
  }
  return compareText(left, right);
}

function sameOrder(left: string[], right: string[]) {
  return left.length === right.length
    && left.every((fileId, index) => fileId === right[index]);
}

function filesInOrder(files: ProjectFile[], fileIds: string[]) {
  const byId = new Map(files.map((item) => [item.file_id, item]));
  return fileIds.map((fileId) => byId.get(fileId)!);
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
      .catch((reason) => setMessage(errorMessage(reason, language)));
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
    if (kind === "folder") {
      incoming.sort((left, right) => compareNaturalPaths(left.path, right.path));
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
  runningProjectIds,
  onProject,
  onCreate,
  language,
}: {
  projects: ProjectSummary[];
  project: string;
  runningProjectIds: ReadonlySet<string>;
  onProject: (value: string) => void;
  onCreate: () => void;
  language: Language;
}) {
  return (
    <div className="overview-project-bar">
      <ProjectPicker projects={projects} project={project} runningProjectIds={runningProjectIds} onProject={onProject} language={language} />
      <button className="quiet-button" onClick={onCreate}>{translate("project.create", language)}</button>
    </div>
  );
}

function ProjectPicker({
  projects,
  project,
  runningProjectIds,
  onProject,
  language,
}: {
  projects: ProjectSummary[];
  project: string;
  runningProjectIds: ReadonlySet<string>;
  onProject: (value: string) => void;
  language: Language;
}) {
  const rootRef = useRef<HTMLDivElement>(null);
  const searchRef = useRef<HTMLInputElement>(null);
  const listId = useId();
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState("");
  const [activeIndex, setActiveIndex] = useState(-1);
  const selected = projects.find((item) => item.selector === project) ?? null;
  const selectedRunning = Boolean(selected && runningProjectIds.has(selected.project_id));
  const otherRunning = projects.filter((item) => item.selector !== project && runningProjectIds.has(item.project_id)).length;
  const normalizedQuery = query.trim().toLocaleLowerCase();
  const filteredProjects = normalizedQuery
    ? projects.filter((item) => (
      item.name.toLocaleLowerCase().includes(normalizedQuery)
      || item.path.toLocaleLowerCase().includes(normalizedQuery)
    ))
    : projects;

  useEffect(() => {
    function closeOnOutsideClick(event: PointerEvent) {
      if (!rootRef.current?.contains(event.target as Node)) closePicker();
    }
    document.addEventListener("pointerdown", closeOnOutsideClick);
    return () => document.removeEventListener("pointerdown", closeOnOutsideClick);
  }, []);

  useEffect(() => {
    if (!open) return;
    const selectedIndex = filteredProjects.findIndex((item) => item.selector === project);
    setActiveIndex(selectedIndex >= 0 ? selectedIndex : filteredProjects.length > 0 ? 0 : -1);
  }, [open, query, project, projects]);

  function closePicker() {
    setOpen(false);
    setQuery("");
  }

  function openPicker() {
    setOpen(true);
    window.setTimeout(() => searchRef.current?.focus(), 0);
  }

  function choose(item: ProjectSummary) {
    onProject(item.selector);
    closePicker();
  }

  function moveActive(direction: 1 | -1) {
    if (!open) openPicker();
    if (!filteredProjects.length) return;
    setActiveIndex((current) => {
      const start = current < 0 ? (direction > 0 ? -1 : 0) : current;
      return (start + direction + filteredProjects.length) % filteredProjects.length;
    });
  }

  function handleKeys(event: ReactKeyboardEvent<HTMLInputElement | HTMLButtonElement>) {
    if (event.key === "Escape") {
      closePicker();
      event.preventDefault();
      return;
    }
    if (event.key === "ArrowDown" || event.key === "ArrowUp") {
      moveActive(event.key === "ArrowDown" ? 1 : -1);
      event.preventDefault();
      return;
    }
    if (event.key === "Enter" && open && activeIndex >= 0) {
      const item = filteredProjects[activeIndex];
      if (item) choose(item);
      event.preventDefault();
    }
  }

  const activeOptionId = open && activeIndex >= 0
    ? `${listId}-option-${activeIndex}`
    : undefined;

  return (
    <div className="project-picker" ref={rootRef}>
      <button
        type="button"
        className="project-picker-trigger"
        role="combobox"
        aria-label={translate("project.select", language)}
        aria-expanded={open}
        aria-controls={listId}
        aria-activedescendant={activeOptionId}
        onClick={() => (open ? closePicker() : openPicker())}
        onKeyDown={handleKeys}
      >
        <span className="project-picker-value">
          <strong>{selected?.name ?? translate("project.select", language)}</strong>
          {selected && <small>{selected.path}</small>}
        </span>
        {(selectedRunning || otherRunning > 0) && <span className="project-picker-status" aria-label={selectedRunning ? translate("project.running", language) : translate("project.otherRunning", language, { count: otherRunning })}>
          {selectedRunning ? translate("project.running", language) : translate("project.otherRunning", language, { count: otherRunning })}
        </span>}
        <span className="project-picker-chevron" aria-hidden="true">⌄</span>
      </button>
      {open && (
        <div className="project-picker-popover">
          <div className="project-search">
            <input
              ref={searchRef}
              aria-label={translate("project.search", language)}
              placeholder={translate("project.searchPlaceholder", language)}
              value={query}
              onKeyDown={handleKeys}
              onChange={(event) => setQuery(event.target.value)}
            />
          </div>
          <div className="project-options" id={listId} role="listbox" aria-label={translate("project.select", language)}>
            {filteredProjects.length === 0 && (
              <div className="project-picker-state" role="status">{translate("project.noMatch", language)}</div>
            )}
            {filteredProjects.map((item, index) => (
              <button
                type="button"
                key={item.selector}
                id={`${listId}-option-${index}`}
                role="option"
                aria-selected={item.selector === project}
                className={`project-option${index === activeIndex ? " active" : ""}${item.selector === project ? " selected" : ""}`}
                onMouseEnter={() => setActiveIndex(index)}
                onClick={() => choose(item)}
              >
                <span>
                  <strong>{item.name}</strong>
                  <small title={item.path}>{item.path}</small>
                </span>
                <span className="project-option-meta">
                  {runningProjectIds.has(item.project_id) && <small className="project-running-badge">{translate("project.running", language)}</small>}
                  {item.selector === project && <span className="project-option-check" aria-hidden="true">✓</span>}
                </span>
              </button>
            ))}
          </div>
        </div>
      )}
    </div>
  );
}

function AddFilesDialog({
  pendingInputs,
  onPendingInputsChange,
  adapterOptions,
  onAdapterOptionsChange,
  existingPaths,
  busy,
  error,
  onClose,
  onSubmit,
  language,
}: {
  pendingInputs: PendingInput[];
  onPendingInputsChange: (value: PendingInput[]) => void;
  adapterOptions: AdapterOptions;
  onAdapterOptionsChange: (value: AdapterOptions) => void;
  existingPaths: string[];
  busy: boolean;
  error: string;
  onClose: () => void;
  onSubmit: () => void;
  language: Language;
}) {
  return (
    <div className="modal-backdrop add-files-backdrop" onMouseDown={onClose}>
      <div
        className="modal add-files-modal"
        role="dialog"
        aria-modal="true"
        aria-labelledby="overview-add-files-title"
        onMouseDown={(event) => event.stopPropagation()}
      >
        <div className="add-files-heading">
          <h2 id="overview-add-files-title">{translate("overview.addFilesTitle", language)}</h2>
          <p>{translate("overview.addFilesHint", language)}</p>
        </div>
        <div className="add-files-content">
          <InputQueue
            value={pendingInputs}
            onChange={onPendingInputsChange}
            existingPaths={existingPaths}
            disabled={busy}
            options={adapterOptions}
            onOptionsChange={onAdapterOptionsChange}
            language={language}
          />
          {error && <div className="error-banner" role="alert">{error}</div>}
        </div>
        <div className="modal-actions">
          <button type="button" className="quiet-button" disabled={busy} onClick={onClose}>{translate("dialog.cancel", language)}</button>
          <button type="button" className="primary-button" disabled={busy || !pendingInputs.length} onClick={onSubmit}>{translate("overview.add", language)}</button>
        </div>
      </div>
    </div>
  );
}

function ReplacementDialog({
  project,
  file,
  busy: parentBusy,
  language,
  onClose,
  onCompleted,
}: {
  project: string;
  file: ProjectFile;
  busy: boolean;
  language: Language;
  onClose: () => void;
  onCompleted: (result: ReplacementImpact) => Promise<void> | void;
}) {
  const fileRef = useRef<HTMLInputElement>(null);
  const [adapter, setAdapter] = useState<AdapterSummary | null>(null);
  const [source, setSource] = useState<ReplacementSource | null>(null);
  const [options, setOptions] = useState<Record<string, string>>({});
  const [preview, setPreview] = useState<(ReplacementImpact & { preview_id: string }) | null>(null);
  const [loadingAdapter, setLoadingAdapter] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  useEffect(() => {
    let active = true;
    setLoadingAdapter(true);
    void api<ReplacementOptionsResponse>(
      `/api/v1/projects/${project}/files/${file.file_id}/replacement-options`,
    )
      .then((value) => {
        if (!active) return;
        setAdapter(value.adapter);
        setOptions(value.values);
      })
      .catch((reason) => {
        if (active) setError(errorMessage(reason, language));
      })
      .finally(() => {
        if (active) setLoadingAdapter(false);
      });
    return () => { active = false; };
  }, [file.file_id, language, project]);

  function chooseFile(value: File | undefined) {
    if (!value) return;
    setSource({ file: value, label: value.name });
    setPreview(null);
    setError("");
  }

  async function chooseNativeFile() {
    const picked = await pickNativeFile();
    if (!picked) return;
    setSource({
      serverPath: picked,
      label: picked.split(/[\\/]/).pop() ?? picked,
    });
    setPreview(null);
    setError("");
  }

  async function createPreview() {
    if (!source || !adapter) return;
    setBusy(true);
    setError("");
    try {
      const body = new FormData();
      if (source.file) body.append("file", source.file, source.file.name);
      if (source.serverPath) body.append("server_path", source.serverPath);
      body.append(
        "adapter_options",
        JSON.stringify({ [adapter.adapter_id]: options }),
      );
      setPreview(await api<ReplacementImpact & { preview_id: string }>(
        `/api/v1/projects/${project}/files/${file.file_id}/replacement-preview`,
        { method: "POST", body },
      ));
    } catch (reason) {
      setError(errorMessage(reason, language));
    } finally {
      setBusy(false);
    }
  }

  async function cancelPreview() {
    if (busy) return;
    setBusy(true);
    setError("");
    try {
      if (preview) {
        await api(
          `/api/v1/projects/${project}/files/${file.file_id}/replacement-preview/${preview.preview_id}`,
          { method: "DELETE" },
        );
      }
      onClose();
    } catch (reason) {
      setError(errorMessage(reason, language));
    } finally {
      setBusy(false);
    }
  }

  async function confirmPreview() {
    if (!preview) return;
    setBusy(true);
    setError("");
    let result: ReplacementImpact;
    try {
      result = await api<ReplacementImpact>(
        `/api/v1/projects/${project}/files/${file.file_id}/replacement-confirm`,
        {
          method: "POST",
          body: JSON.stringify({ preview_id: preview.preview_id }),
        },
      );
    } catch (reason) {
      setError(errorMessage(reason, language));
      setBusy(false);
      return;
    }
    setPreview(null);
    setBusy(false);
    onCompleted(result);
  }

  const stageCounts = (values: Record<string, number>) => (
    Object.entries(values).filter(([, count]) => count > 0).map(([stage, count]) => (
      <span key={stage}>{stage}: {count}</span>
    ))
  );
  const optionDefinitions = adapter
    ? [...adapter.import_options, ...adapter.run_options]
    : [];

  return (
    <div className="modal-backdrop" onMouseDown={() => void cancelPreview()}>
      <div
        className="modal replacement-modal"
        role="dialog"
        aria-modal="true"
        aria-labelledby="overview-replacement-title"
        onMouseDown={(event) => event.stopPropagation()}
      >
        <div className="replacement-heading">
          <h2 id="overview-replacement-title">{translate("overview.replaceTitle", language)}</h2>
          <p>{translate("overview.replaceHint", language, { name: file.name })}</p>
        </div>
        {!preview && (
          <>
            <div className="replacement-source">
              <strong>{translate("overview.replaceSource", language)}</strong>
              <div className="button-group">
                <input
                  ref={fileRef}
                  hidden
                  type="file"
                  accept={adapter?.extensions.join(",")}
                  onChange={(event) => {
                    chooseFile(event.target.files?.[0]);
                    event.target.value = "";
                  }}
                />
                <button
                  type="button"
                  className="quiet-button"
                  disabled={busy || parentBusy || loadingAdapter}
                  onClick={() => {
                    if (nativeBridgeAvailable()) void chooseNativeFile();
                    else fileRef.current?.click();
                  }}
                >
                  {translate("overview.replaceChoose", language)}
                </button>
              </div>
              <span className={source ? "" : "muted"}>
                {source?.label ?? translate("overview.replaceNoSource", language)}
              </span>
            </div>
            {optionDefinitions.length > 0 && (
              <div className="replacement-options">
                {optionDefinitions.map((option) => (
                  <label key={option.option_id}>
                    {option.label}
                    <select
                      disabled={busy || parentBusy}
                      value={options[option.option_id] ?? option.default}
                      onChange={(event) => setOptions({ ...options, [option.option_id]: event.target.value })}
                    >
                      {option.choices.map((choice) => <option value={choice.value} key={choice.value}>{choice.label}</option>)}
                    </select>
                  </label>
                ))}
              </div>
            )}
          </>
        )}
        {preview && (
          <div className="replacement-impact" aria-live="polite">
            <div className="replacement-counts">
              <span>{translate("overview.replaceOldCount", language, { count: preview.old_segment_count })}</span>
              <span>{translate("overview.replaceNewCount", language, { count: preview.new_segment_count })}</span>
              <span>{translate("overview.replacePreservedCount", language, { count: preview.preserved_segment_count })}</span>
              <span>{translate("overview.replaceAddedCount", language, { count: preview.added_segment_count })}</span>
              <span>{translate("overview.replaceRemovedCount", language, { count: preview.removed_segment_count })}</span>
            </div>
            {(preview.ambiguous_old_segment_count > 0 || preview.ambiguous_new_segment_count > 0) && (
              <p>{translate("overview.replaceAmbiguousCount", language, {
                old: preview.ambiguous_old_segment_count,
                new: preview.ambiguous_new_segment_count,
              })}</p>
            )}
            {stageCounts(preview.preserved_completed_by_stage).length > 0 && (
              <div><strong>{translate("overview.replacePreservedProgress", language)}</strong>{stageCounts(preview.preserved_completed_by_stage)}</div>
            )}
            {stageCounts(preview.removed_completed_by_stage).length > 0 && (
              <div><strong>{translate("overview.replaceRemovedProgress", language)}</strong>{stageCounts(preview.removed_completed_by_stage)}</div>
            )}
            {preview.changed_adapter_options.length > 0 && (
              <div className="replacement-option-changes">
                <strong>{translate("overview.replaceOptionsChanged", language)}</strong>
                {preview.changed_adapter_options.map((optionId) => {
                  const option = optionDefinitions.find((item) => item.option_id === optionId);
                  return (
                    <div key={optionId}>
                      {option?.label ?? optionId}: {preview.previous_adapter_options[optionId]} → {preview.replacement_adapter_options[optionId]}
                    </div>
                  );
                })}
              </div>
            )}
            {preview.warnings.map((warning) => <p className="warning-text" key={warning}>{warning}</p>)}
          </div>
        )}
        {error && <div className="error-banner" role="alert">{error}</div>}
        <div className="modal-actions">
          <button type="button" className="quiet-button" disabled={busy} onClick={() => void cancelPreview()}>
            {preview ? translate("overview.replaceCancelPreview", language) : translate("dialog.cancel", language)}
          </button>
          {preview ? (
            <button type="button" className="primary-button" disabled={busy} onClick={() => void confirmPreview()}>
              {busy ? translate("overview.replaceApplying", language) : translate("overview.replaceConfirm", language)}
            </button>
          ) : (
            <button type="button" className="primary-button" disabled={busy || parentBusy || !source || !adapter} onClick={() => void createPreview()}>
              {busy ? translate("overview.replacePreviewing", language) : translate("overview.replacePreview", language)}
            </button>
          )}
        </div>
      </div>
    </div>
  );
}

export function Overview({
  projects,
  project,
  runningProjectIds,
  value,
  onProject,
  onCreate,
  onFilesChanged,
  onDeleted,
  language,
}: {
  projects: ProjectSummary[];
  project: string;
  runningProjectIds: ReadonlySet<string>;
  value: ProjectOverview | null;
  onProject: (value: string) => void;
  onCreate: () => void;
  onFilesChanged: () => Promise<void>;
  onDeleted: (path: string) => Promise<void>;
  language: Language;
}) {
  const selection = useClassicSelection();
  const [pendingInputs, setPendingInputs] = useState<PendingInput[]>([]);
  const [adapterOptions, setAdapterOptions] = useState<AdapterOptions>({});
  const [addFilesOpen, setAddFilesOpen] = useState(false);
  const [replacementTarget, setReplacementTarget] = useState<ProjectFile | null>(null);
  const [removing, setRemoving] = useState(false);
  const [deleting, setDeleting] = useState(false);
  const [compacting, setCompacting] = useState(false);
  const [storageMessage, setStorageMessage] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [optimisticOrder, setOptimisticOrder] = useState<OptimisticFileOrder | null>(null);
  const [draggedFileIds, setDraggedFileIds] = useState<string[]>([]);
  const [buttonReorder, setButtonReorder] = useState<ButtonReorderState | null>(null);
  const [dropTarget, setDropTarget] = useState<{
    fileId: string;
    position: DropPosition;
  } | null>(null);

  useEffect(() => {
    if (buttonReorder && buttonReorder.project !== project) setButtonReorder(null);
  }, [buttonReorder, project]);

  function closeAddFiles() {
    if (busy) return;
    setAddFilesOpen(false);
    setPendingInputs([]);
    setAdapterOptions({});
    setError("");
  }

  function openAddFiles() {
    setError("");
    setAddFilesOpen(true);
  }

  function openReplacement(item: ProjectFile) {
    setError("");
    setReplacementTarget(item);
  }

  function changeProject(nextProject: string) {
    closeAddFiles();
    setButtonReorder(null);
    setDraggedFileIds([]);
    setDropTarget(null);
    selection.reset();
    setStorageMessage("");
    onProject(nextProject);
  }

  if (!value) {
    return (
      <div className="page">
        <ProjectBar projects={projects} project={project} runningProjectIds={runningProjectIds} onProject={changeProject} onCreate={onCreate} language={language} />
        <p className="overview-empty-hint">{translate("app.selectOrCreate", language)}</p>
      </div>
    );
  }
  const completed = value.completed_segments;
  const projectPath = value.path;
  const serverFileIds = value.files.map((item) => item.file_id);
  const orderedFiles = optimisticOrder?.project === project
    && sameOrder(serverFileIds, optimisticOrder.before)
    ? filesInOrder(value.files, optimisticOrder.after)
    : value.files;
  const fileIds = orderedFiles.map((item) => item.file_id);
  const draggedFileIdSet = new Set(draggedFileIds);
  const buttonReorderMode = buttonReorder?.project === project;
  const selectedReorderFileIds = buttonReorderMode
    ? fileIds.filter((fileId) => selection.selectedKeys.has(fileId))
    : [];
  const selectedReorderFiles = buttonReorderMode
    ? orderedFiles.filter((item) => selection.selectedKeys.has(item.file_id))
    : [];

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
      setAddFilesOpen(false);
      if (result.warnings.length) setError(result.warnings.join("；"));
      selection.reset();
      await onFilesChanged();
    } catch (value) {
      setError(errorMessage(value, language));
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
      setError(errorMessage(value, language));
    } finally {
      setBusy(false);
    }
  }

  async function saveFileOrder(before: string[], after: string[]) {
    setOptimisticOrder({ project, before, after });
    setBusy(true);
    setError("");
    let saved = false;
    try {
      await api(`/api/v1/projects/${project}/files/reorder`, {
        method: "POST",
        body: JSON.stringify({ file_ids: after }),
      });
      saved = true;
      await onFilesChanged();
    } catch (reason) {
      if (!saved) setOptimisticOrder(null);
      setError(errorMessage(reason, language));
    } finally {
      setBusy(false);
    }
  }

  function toggleButtonReorder() {
    if (buttonReorderMode) {
      setButtonReorder(null);
      selection.reset();
      return;
    }
    selection.reset();
    setDraggedFileIds([]);
    setDropTarget(null);
    setError("");
    setButtonReorder({ project });
  }

  function moveSelectedFiles(command: FileMoveCommand) {
    if (busy || compacting || selectedReorderFileIds.length === 0) return;
    const nextFileIds = moveFilesByCommand(fileIds, selectedReorderFileIds, command);
    if (sameOrder(fileIds, nextFileIds)) return;
    void saveFileOrder(fileIds, nextFileIds);
  }

  function moveCommandDisabled(command: FileMoveCommand) {
    return busy
      || compacting
      || selectedReorderFileIds.length === 0
      || sameOrder(fileIds, moveFilesByCommand(fileIds, selectedReorderFileIds, command));
  }

  function reorderHandleLabel(item: ProjectFile) {
    return selection.selectedKeys.has(item.file_id) && selection.selectedKeys.size > 1
      ? translate("overview.reorderGroupHandle", language, {
          count: selection.selectedKeys.size,
          name: item.name,
        })
      : translate("overview.reorderHandle", language, { name: item.name });
  }

  function startFileDrag(event: ReactDragEvent<HTMLButtonElement>, fileId: string) {
    if (busy || compacting || buttonReorderMode || orderedFiles.length < 2) {
      event.preventDefault();
      return;
    }
    event.stopPropagation();
    const movingFileIds = selection.selectedKeys.has(fileId)
      ? fileIds.filter((candidate) => selection.selectedKeys.has(candidate))
      : [fileId];
    if (!selection.selectedKeys.has(fileId)) selection.reset(fileId);
    event.dataTransfer.effectAllowed = "move";
    event.dataTransfer.setData(
      "application/x-another-llm-file-ids",
      JSON.stringify(movingFileIds),
    );
    event.dataTransfer.setData("text/plain", fileId);
    setDraggedFileIds(movingFileIds);
    setDropTarget(null);
  }

  function updateDropTarget(event: ReactDragEvent<HTMLDivElement>, fileId: string) {
    if (busy || compacting || buttonReorderMode || orderedFiles.length < 2) return;
    event.preventDefault();
    event.dataTransfer.dropEffect = "move";
    if (draggedFileIdSet.has(fileId)) {
      setDropTarget(null);
      return;
    }
    const bounds = event.currentTarget.getBoundingClientRect();
    const position = event.clientY < bounds.top + bounds.height / 2
      ? "before"
      : "after";
    setDropTarget((current) => (
      current?.fileId === fileId && current.position === position
        ? current
        : { fileId, position }
    ));
  }

  function dropFile(event: ReactDragEvent<HTMLDivElement>, fileId: string) {
    event.preventDefault();
    if (busy || compacting || buttonReorderMode) return;
    let movingFileIds = draggedFileIds;
    if (movingFileIds.length === 0) {
      try {
        const payload = JSON.parse(
          event.dataTransfer.getData("application/x-another-llm-file-ids"),
        );
        if (Array.isArray(payload) && payload.every((item) => typeof item === "string")) {
          movingFileIds = payload;
        }
      } catch {
        const fallbackFileId = event.dataTransfer.getData("text/plain");
        movingFileIds = fallbackFileId ? [fallbackFileId] : [];
      }
    }
    const bounds = event.currentTarget.getBoundingClientRect();
    const position = event.clientY < bounds.top + bounds.height / 2
      ? "before"
      : "after";
    setDraggedFileIds([]);
    setDropTarget(null);
    const nextFileIds = moveFileBlock(fileIds, movingFileIds, fileId, position);
    if (sameOrder(fileIds, nextFileIds)) return;
    void saveFileOrder(fileIds, nextFileIds);
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
      setButtonReorder(null);
      await onDeleted(projectPath);
    } catch (reason) {
      setError(errorMessage(reason, language));
    } finally {
      setBusy(false);
    }
  }

  async function compactStorage() {
    if (!window.confirm(translate("overview.compactConfirm", language))) return;
    setCompacting(true);
    setError("");
    setStorageMessage("");
    try {
      const result = await api<{ reclaimed_bytes: number }>(
        `/api/v1/projects/${project}/storage/compact`,
        { method: "POST" },
      );
      setStorageMessage(
        translate("overview.compactDone", language, {
          size: formatSize(result.reclaimed_bytes),
        }),
      );
    } catch (reason) {
      setError(errorMessage(reason, language));
    } finally {
      setCompacting(false);
    }
  }

  return (
    <div className="page overview-page">
      <ProjectBar projects={projects} project={project} runningProjectIds={runningProjectIds} onProject={changeProject} onCreate={onCreate} language={language} />
      <div className="page-heading overview-heading">
        <div className="overview-identity"><h1>{value.name}</h1><p>{value.path}</p></div>
        <div className="summary-strip">
          <div><strong>{value.files.length}</strong><span>{translate("overview.files", language)}</span></div>
          <div><strong>{value.nonempty_segment_count}</strong><span>{translate("overview.nonempty", language)}</span></div>
          <div><strong>{completed}</strong><span>{translate("overview.translated", language)}</span></div>
        </div>
        <div className="overview-project-actions">
          <button className="quiet-button" disabled={busy || compacting || buttonReorderMode} onClick={() => void compactStorage()}>
            {compacting ? translate("overview.compacting", language) : translate("overview.compact", language)}
          </button>
          <button className="danger-button" disabled={busy || compacting} onClick={() => setDeleting(true)}>{translate("overview.delete", language)}</button>
        </div>
      </div>
      <div className="overview-file-section">
        <div className="section-heading">
        <div><h2>{translate("overview.fileHeading", language)}</h2><p>{translate("overview.fileHint", language)}</p></div>
        <div className="section-actions overview-file-actions">
          <button className="primary-button" disabled={busy || compacting || buttonReorderMode} onClick={openAddFiles}>
            {translate("overview.addFiles", language)}
          </button>
          <button className="danger-button" disabled={busy || compacting || buttonReorderMode || selection.selectedKeys.size === 0} onClick={() => setRemoving(true)}>
            {translate("overview.remove", language)}
          </button>
          <button
            type="button"
            className="quiet-button mobile-reorder-toggle"
            aria-pressed={buttonReorderMode}
            disabled={busy || compacting || (!buttonReorderMode && orderedFiles.length < 2)}
            onClick={toggleButtonReorder}
          >
            {translate(buttonReorderMode ? "overview.reorderDone" : "overview.reorderStart", language)}
          </button>
        </div>
        </div>
        {error && <button className="error-banner" onClick={() => setError("")}>{error}</button>}
        {storageMessage && <button className="success-banner" onClick={() => setStorageMessage("")}>{storageMessage}</button>}
        {buttonReorderMode && (
          <div className="mobile-reorder-toolbar" role="toolbar" aria-label={translate("overview.reorderToolbar", language)}>
            <span aria-live="polite">
              {selectedReorderFiles.length === 0
                ? translate("overview.reorderChoose", language)
                : selectedReorderFiles.length === 1
                  ? translate("overview.reorderFocused", language, { name: selectedReorderFiles[0].name })
                  : translate("overview.reorderSelected", language, { count: selectedReorderFiles.length })}
            </span>
            <div className="mobile-reorder-actions">
              <button type="button" className="quiet-button" disabled={moveCommandDisabled("top")} onClick={() => moveSelectedFiles("top")}>{translate("overview.moveTop", language)}</button>
              <button type="button" className="quiet-button" disabled={moveCommandDisabled("up")} onClick={() => moveSelectedFiles("up")}>{translate("overview.moveUp", language)}</button>
              <button type="button" className="quiet-button" disabled={moveCommandDisabled("down")} onClick={() => moveSelectedFiles("down")}>{translate("overview.moveDown", language)}</button>
              <button type="button" className="quiet-button" disabled={moveCommandDisabled("bottom")} onClick={() => moveSelectedFiles("bottom")}>{translate("overview.moveBottom", language)}</button>
            </div>
          </div>
        )}
        <div className="file-list overview-file-list">
          {value.files.length === 0 && (
            <div className="empty-file-state">
              <strong>{translate("overview.noFiles", language)}</strong>
              <span>{translate("overview.addHint", language)}</span>
            </div>
          )}
          {orderedFiles.map((item) => (
            <div
              key={item.file_id}
              className={`file-row${selection.selectedKeys.has(item.file_id) ? " selected" : ""}${draggedFileIdSet.has(item.file_id) ? " dragging" : ""}${dropTarget?.fileId === item.file_id ? ` drop-${dropTarget.position}` : ""}`}
              onDragOver={(event) => updateDropTarget(event, item.file_id)}
              onDrop={(event) => dropFile(event, item.file_id)}
            >
              <button
                type="button"
                className="file-row-drag"
                draggable={!busy && !compacting && !buttonReorderMode && orderedFiles.length > 1}
                disabled={busy || compacting || buttonReorderMode || orderedFiles.length < 2}
                aria-label={reorderHandleLabel(item)}
                title={reorderHandleLabel(item)}
                onClick={(event) => event.stopPropagation()}
                onDragStart={(event) => startFileDrag(event, item.file_id)}
                onDragEnd={() => {
                  setDraggedFileIds([]);
                  setDropTarget(null);
                }}
              >
                <span aria-hidden="true">⠿</span>
              </button>
              <button
                type="button"
                className={`file-row-select${buttonReorderMode ? " reorder-select" : ""}`}
                disabled={busy || compacting}
                aria-pressed={selection.selectedKeys.has(item.file_id)}
                onClick={(event) => {
                  if (buttonReorderMode) {
                    selection.select(item.file_id, fileIds, {
                      ctrlKey: true,
                      metaKey: false,
                      shiftKey: false,
                    });
                    return;
                  }
                  selection.select(item.file_id, fileIds, event);
                }}
              >
                {buttonReorderMode && (
                  <span className="file-row-reorder-check" aria-hidden="true">
                    {selection.selectedKeys.has(item.file_id) ? "✓" : ""}
                  </span>
                )}
                <span>{item.file_id}</span><strong>{item.name}</strong><small>{item.document_adapter_id.toUpperCase()}</small>
              </button>
              <button
                type="button"
                className="quiet-button file-row-replace"
                disabled={busy || compacting || buttonReorderMode}
                onClick={(event) => {
                  event.stopPropagation();
                  openReplacement(item);
                }}
              >
                {translate("overview.replace", language)}
              </button>
            </div>
          ))}
        </div>
      </div>
      {addFilesOpen && (
        <AddFilesDialog
          pendingInputs={pendingInputs}
          onPendingInputsChange={setPendingInputs}
          adapterOptions={adapterOptions}
          onAdapterOptionsChange={setAdapterOptions}
          existingPaths={value.files.map((item) => item.name)}
          busy={busy}
          error={error}
          onClose={closeAddFiles}
          onSubmit={() => void upload()}
          language={language}
        />
      )}
      {replacementTarget && (
        <ReplacementDialog
          project={project}
          file={replacementTarget}
          busy={busy}
          language={language}
          onClose={() => setReplacementTarget(null)}
          onCompleted={(result) => {
            setStorageMessage(translate("overview.replaceDone", language, {
              file: replacementTarget.name,
              preserved: result.preserved_segment_count,
              added: result.added_segment_count,
              removed: result.removed_segment_count,
            }));
            selection.reset();
            setReplacementTarget(null);
            void onFilesChanged().catch((reason) => {
              setError(errorMessage(reason, language));
            });
          }}
        />
      )}
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
  onNavigateStage,
  onOpenSettings,
}: {
  project: string;
  overview: ProjectOverview;
  language: Language;
  onNavigateStage: (stage: Stage) => void;
  onOpenSettings: (field: SettingsField) => void;
}) {
  const [stage, setStage] = useState("translated");
  const [format, setFormat] = useState("original");
  const [bilingual, setBilingual] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const [exportError, setExportError] = useState<ErrorPayload | null>(null);
  const [message, setMessage] = useState("");
  const [files, setFiles] = useState<ExportFile[]>([]);
  const [highlighted, setHighlighted] = useState<string[]>([]);
  const [tab, setTab] = useState<"export" | "browse">("export");
  const [selectionFilter, setSelectionFilter] = useState("");
  const [browseFilter, setBrowseFilter] = useState("");
  const selection = useClassicSelection();
  const native = nativeBridgeAvailable();

  const filteredSourceFiles = overview.files.filter((item) => (
    item.name.toLocaleLowerCase().includes(selectionFilter.toLocaleLowerCase().trim())
  ));
  const fileIds = filteredSourceFiles.map((item) => item.file_id);
  const filteredExports = files.filter((item) => (
    item.path.toLocaleLowerCase().includes(browseFilter.toLocaleLowerCase().trim())
  ));

  async function refresh() {
    const value = await api<{ files: ExportFile[] }>(
      `/api/v1/projects/${project}/exports`,
    );
    setFiles(value.files);
  }

  useEffect(() => {
    void refresh().catch((reason) => setError(reason));
  }, [project]);

  function openBrowse() {
    setTab("browse");
    void refresh().catch((reason) => setError(reason));
  }

  async function run() {
    setError(null); setExportError(null); setMessage(""); setHighlighted([]);
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
      const payload = errorPayloadFrom(reason);
      if (payload?.code === "export_error") setExportError(payload);
      else setError(reason);
    }
  }

  function downloadHref(): string {
    return `/api/v1/projects/${project}/exports/download`;
  }

  async function saveViaNative(
    url: string,
    filename: string,
    body?: Record<string, unknown>,
  ) {
    setError(null);
    try {
      const saved = await saveExport(
        url,
        filename,
        body ? JSON.stringify(body) : undefined,
      );
      if (saved) setMessage(translate("export.savedTo", language, { path: saved }));
    } catch (reason) {
      setError(reason);
    }
  }

  function download(path: string) {
    const filename = path.split("/").pop() || "export";
    if (native) {
      void saveViaNative(downloadHref(), filename, { file: path });
      return;
    }
    void fetch(downloadHref(), {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ file: path }),
    }).then(async (response) => {
      if (!response.ok) throw await apiErrorFromResponse(response);
      const url = URL.createObjectURL(await response.blob());
      const anchor = document.createElement("a");
      anchor.href = url;
      anchor.download = filename;
      document.body.appendChild(anchor);
      anchor.click();
      anchor.remove();
      URL.revokeObjectURL(url);
    }).catch((reason) => setError(reason));
  }

  function downloadAll() {
    if (!files.length) return;
    const url = `/api/v1/projects/${project}/exports/download-all`;
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
    setError(null); setMessage("");
    try {
      await api(`/api/v1/projects/${project}/exports/remove`, {
        method: "POST",
        body: JSON.stringify({ files: [path] }),
      });
      await refresh();
    } catch (reason) {
      setError(reason);
    }
  }

  const action = (() => {
    if (!exportError) return null;
    const reason = String(exportError.params.reason ?? "");
    const settingsField = EXPORT_SETTINGS_FIELDS[reason];
    if (settingsField) return { kind: "settings" as const, field: settingsField };
    if (reason !== "missing_stage_results") return null;
    const target = EXPORT_RESULT_STAGES[String(exportError.params.stage ?? "")];
    return target ? { kind: "stage" as const, stage: target } : null;
  })();

  return (
    <div className="page export-page">
      <div className="page-heading"><div><h1>{translate("export.title", language)}</h1><p>{translate("export.description", language)}</p></div></div>
      <div className="dialog-tabs" role="tablist" aria-label={translate("export.title", language)}>
        <button className={tab === "export" ? "active" : ""} onClick={() => setTab("export")}>{translate("export.tabExport", language)}</button>
        <button className={tab === "browse" ? "active" : ""} onClick={openBrowse}>{translate("export.tabBrowse", language)}</button>
      </div>
      {error != null ? <button className="error-banner" onClick={() => setError(null)}>{errorMessage(error, language)}</button> : null}
      {tab === "export" ? <>
        <div className="export-workspace">
          <div className="export-form-col">
            <label>{translate("export.resultStage", language)}<select value={stage} onChange={(event) => setStage(event.target.value)}><option value="translated">{translate("stage.translation", language)}</option><option value="proofread">{translate("export.proofread", language)}</option><option value="polished">{translate("export.polished", language)}</option></select></label>
            <label>{translate("export.format", language)}<select value={format} onChange={(event) => setFormat(event.target.value)}><option value="original">{translate("export.keepFormat", language)}</option><option value="txt">{translate("export.txt", language)}</option></select></label>
            <label className="check-row"><input type="checkbox" checked={bilingual} onChange={(event) => setBilingual(event.target.checked)} /> {translate("export.bilingual", language)}</label>
            <button className="primary-button" onClick={() => void run()}>{translate("export.generate", language)}</button>
            {exportError && (
              <div className="export-error-card" role="alert">
                <div className="export-error-heading">
                  <strong>{translate("export.errorTitle", language)}</strong>
                  <button
                    className="export-error-dismiss"
                    aria-label={translate("common.dismiss", language)}
                    title={translate("common.dismiss", language)}
                    onClick={() => setExportError(null)}
                  >
                    ×
                  </button>
                </div>
                <p>{formatErrorPayload(exportError, language)}</p>
                {action ? <div className="export-error-actions">
                  {action?.kind === "settings" && (
                    <button className="quiet-button" onClick={() => onOpenSettings(action.field)}>
                      {translate("export.openProjectSettings", language)}
                    </button>
                  )}
                  {action?.kind === "stage" && (
                    <button className="quiet-button" onClick={() => onNavigateStage(action.stage)}>
                      {translate("export.openStage", language, { stage: translate(`stage.${action.stage}`, language) })}
                    </button>
                  )}
                </div> : null}
              </div>
            )}
            {message && (
              <div className="notice-box"><span>{message}</span><button className="quiet-button" onClick={openBrowse}>{translate("export.viewOutputs", language)}</button></div>
            )}
          </div>
          <div className="export-select-col">
            <div className="export-file-heading">
              <div>
                <strong>{translate("export.fileScope", language)}</strong>
                <small>{selection.selectedKeys.size ? translate("export.selected", language, { count: selection.selectedKeys.size }) : translate("export.allFiles", language)}</small>
              </div>
              <button className="quiet-button" disabled={!selection.selectedKeys.size} onClick={() => selection.reset()}>{translate("export.clearSelection", language)}</button>
            </div>
            <input className="export-filter" value={selectionFilter} onChange={(event) => setSelectionFilter(event.target.value)} placeholder={translate("export.searchPlaceholder", language)} aria-label={translate("export.searchPlaceholder", language)} />
            <div className="file-list export-file-list">
              {!filteredSourceFiles.length && selectionFilter && <p className="export-empty">{translate("export.noMatch", language)}</p>}
              {filteredSourceFiles.map((item) => (
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
          </div>
        </div>
      </> : <>
        <div className="export-file-heading">
          <div>
            <strong>{translate("export.outputFiles", language)}</strong>
            <small>{translate("export.outputCount", language, { count: files.length })}</small>
          </div>
          <div className="button-group">
            <button className="quiet-button" onClick={openBrowse}>{translate("directory.refresh", language)}</button>
            <button className="quiet-button" disabled={!files.length} onClick={downloadAll}>{translate("export.downloadAll", language)}</button>
          </div>
        </div>
        <input className="export-filter" value={browseFilter} onChange={(event) => setBrowseFilter(event.target.value)} placeholder={translate("export.searchPlaceholder", language)} aria-label={translate("export.searchPlaceholder", language)} />
        <div className="file-list export-browse-list">
          {!files.length && <p className="export-empty">{translate("export.noFiles", language)}</p>}
          {files.length > 0 && !filteredExports.length && <p className="export-empty">{translate("export.noMatch", language)}</p>}
          {filteredExports.map((item) => (
            <div key={item.path} className={`file-row export-row${highlighted.includes(item.path) ? " selected" : ""}`}>
              <strong>{item.path}</strong>
              <small>{formatSize(item.size)} · {formatTime(item.mtime)}</small>
              <span className="export-actions">
                <button className="quiet-button" onClick={() => download(item.path)}>{translate("export.download", language)}</button>
                <button className="quiet-button" onClick={() => void removeFile(item.path)}>{translate("export.delete", language)}</button>
              </span>
            </div>
          ))}
        </div>
      </>}
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
