import { useEffect, useRef, useState, useId, type DragEvent as ReactDragEvent, type KeyboardEvent as ReactKeyboardEvent, type RefObject } from "react";
import { api, apiErrorFromResponse, errorPayloadFrom } from "../api";
import { nativeBridgeAvailable, pickNativeFile, pickNativeFolder, saveExport } from "../native";
import { moveFileBlock, moveFilesByCommand, type DropPosition, type FileMoveCommand } from "../fileOrder";
import { useClassicSelection } from "../useClassicSelection";
import { errorMessage, formatErrorPayload, translate, type Language } from "../i18n";
import type { ErrorPayload, ProjectOverview, SettingsField, Stage } from "../types";

interface ExportFile { path: string; size: number; mtime: number; }
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

function formatSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  if (bytes < 1024 * 1024 * 1024) return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
  return `${(bytes / (1024 * 1024 * 1024)).toFixed(1)} GB`;
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
