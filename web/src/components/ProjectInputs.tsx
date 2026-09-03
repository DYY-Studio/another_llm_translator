import { useEffect, useRef, useState, type RefObject } from "react";
import { api } from "../api";
import { nativeBridgeAvailable, pickNativeFile, pickNativeFolder } from "../native";
import { errorMessage, translate, type Language } from "../i18n";
export type InputKind = "file" | "folder";

export interface AdapterSummary {
  adapter_id: string; capabilities: string[]; extensions: string[];
  import_options: Array<{ option_id: string; label: string; default: string; choices: Array<{ value: string; label: string }> }>;
  run_options: Array<{ option_id: string; label: string; default: string; choices: Array<{ value: string; label: string }> }>;
}
export type AdapterOptions = Record<string, Record<string, string>>;
export interface PendingInput { file?: File; serverPath?: string; path: string; kind: InputKind; adapterId: string; }
export const NATURAL_NUMBER = /^[0-9]+$/;
export const NATURAL_PARTS = /([0-9]+)/;
export function extensionOf(path: string) { const dot = path.lastIndexOf("."); return dot < 0 ? "" : path.slice(dot).toLocaleLowerCase(); }
export function compareText(left: string, right: string) { const leftPoints = Array.from(left, (value) => value.codePointAt(0) ?? 0); const rightPoints = Array.from(right, (value) => value.codePointAt(0) ?? 0); const length = Math.min(leftPoints.length, rightPoints.length); for (let index = 0; index < length; index += 1) { if (leftPoints[index] !== rightPoints[index]) return leftPoints[index] - rightPoints[index]; } return leftPoints.length - rightPoints.length; }
export function compareNaturalPaths(left: string, right: string) { const leftFolded = left.toLowerCase(); const rightFolded = right.toLowerCase(); const leftParts = leftFolded.split(NATURAL_PARTS); const rightParts = rightFolded.split(NATURAL_PARTS); const length = Math.min(leftParts.length, rightParts.length); for (let index = 0; index < length; index += 1) { const leftPart = leftParts[index]; const rightPart = rightParts[index]; const leftIsNumber = NATURAL_NUMBER.test(leftPart); const rightIsNumber = NATURAL_NUMBER.test(rightPart); if (leftIsNumber && rightIsNumber) { const difference = BigInt(leftPart) - BigInt(rightPart); if (difference !== 0n) return difference < 0n ? -1 : 1; continue; } if (leftIsNumber !== rightIsNumber) return leftIsNumber ? -1 : 1; const comparison = compareText(leftPart, rightPart); if (comparison) return comparison; } if (leftParts.length !== rightParts.length) return leftParts.length - rightParts.length; return compareText(left, right); }

export function InputQueue({
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


export function AddFilesDialog({
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
