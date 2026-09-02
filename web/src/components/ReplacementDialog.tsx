import { useEffect, useRef, useState, useId, type DragEvent as ReactDragEvent, type KeyboardEvent as ReactKeyboardEvent, type RefObject } from "react";
import { api, apiErrorFromResponse, errorPayloadFrom } from "../api";
import { nativeBridgeAvailable, pickNativeFile, pickNativeFolder, saveExport } from "../native";
import { moveFileBlock, moveFilesByCommand, type DropPosition, type FileMoveCommand } from "../fileOrder";
import { useClassicSelection } from "../useClassicSelection";
import { errorMessage, formatErrorPayload, translate, type Language } from "../i18n";
import type { ProjectFile, AdapterSummary, ReplacementImpact, ReplacementSource, ReplacementOptionsResponse } from "./ProjectInputs";

export function ReplacementDialog({
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
  onCompleted: (result: ReplacementImpact) => void;
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
                      value={options[option.option_id] ?? ""}
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
