import { useEffect, useRef, useState, useId, type DragEvent as ReactDragEvent, type KeyboardEvent as ReactKeyboardEvent, type RefObject } from "react";
import { api, apiErrorFromResponse, errorPayloadFrom } from "../api";
import { nativeBridgeAvailable, pickNativeFile, pickNativeFolder, saveExport } from "../native";
import { moveFileBlock, moveFilesByCommand, type DropPosition, type FileMoveCommand } from "../fileOrder";
import { useClassicSelection } from "../useClassicSelection";
import { errorMessage, formatErrorPayload, translate, type Language } from "../i18n";
import type { AdapterOptions, PendingInput } from "./ProjectInputs";
import type { ProjectOverview, ProjectSummary } from "../types";
import { ProjectBar } from "./ProjectPicker";
import { InputQueue, AddFilesDialog } from "./ProjectInputs";
import { ReplacementDialog } from "./ReplacementDialog";

function formatSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  if (bytes < 1024 * 1024 * 1024) return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
  return `${(bytes / (1024 * 1024 * 1024)).toFixed(1)} GB`;
}

type ProjectFile = ProjectOverview["files"][number];
interface OptimisticFileOrder { project: string; before: string[]; after: string[]; }
interface ButtonReorderState { project: string; }
function sameOrder(left: string[], right: string[]) {
  return left.length === right.length && left.every((fileId, index) => fileId === right[index]);
}
function filesInOrder(files: ProjectFile[], fileIds: string[]) {
  const byId = new Map(files.map((item) => [item.file_id, item]));
  return fileIds.map((fileId) => byId.get(fileId)!);
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

  useEffect(() => {
    if (!storageMessage) return;
    const timer = window.setTimeout(() => setStorageMessage(""), 5_000);
    return () => window.clearTimeout(timer);
  }, [storageMessage]);

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
      const result = await api<{
        before_bytes: number;
        after_bytes: number;
        reclaimed_bytes: number;
      }>(
        `/api/v1/projects/${project}/storage/compact`,
        { method: "POST" },
      );
      await onFilesChanged();
      setStorageMessage(
        translate("overview.compactDone", language, {
          reclaimed: formatSize(result.reclaimed_bytes),
          after: formatSize(result.after_bytes),
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
        <div className="overview-identity">
          <h1>{value.name}</h1>
          <p>{value.path}</p>
          <dl className="overview-storage" aria-label={translate("overview.storage", language)}>
            <div>
              <dt>{translate("overview.storageTotal", language)}</dt>
              <dd>{formatSize(value.storage.total_bytes)}</dd>
            </div>
            <div>
              <dt>{translate("overview.storageSqlite", language)}</dt>
              <dd>{formatSize(value.storage.sqlite_bytes)}</dd>
            </div>
          </dl>
        </div>
        <div className="summary-strip">
          <div><strong>{value.files.length}</strong><span>{translate("overview.files", language)}</span></div>
          <div><strong>{value.nonempty_segment_count}</strong><span>{translate("overview.nonempty", language)}</span></div>
          <div><strong>{completed}</strong><span>{translate("overview.translated", language)}</span></div>
        </div>
        <div className="overview-heading-controls">
          <div className="overview-project-actions">
            <button className="quiet-button" disabled={busy || compacting || buttonReorderMode} onClick={() => void compactStorage()}>
              {compacting ? translate("overview.compacting", language) : translate("overview.compact", language)}
            </button>
            <button className="danger-button" disabled={busy || compacting} onClick={() => setDeleting(true)}>{translate("overview.delete", language)}</button>
          </div>
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
                <span>{item.file_id}</span>
                <strong>{item.name}</strong>
                <span className="file-row-meta">
                  <small>{item.document_adapter_id.toUpperCase()}</small>
                  <small className="file-row-size">{formatSize(item.size_bytes)}</small>
                </span>
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
