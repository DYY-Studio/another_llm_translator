import { useState } from "react";
import { api, apiErrorFromResponse } from "../api";
import { errorMessage, translate, type Language } from "../i18n";
import type { RelatedTerm, TermsResponse } from "../types";
import { Modal } from "./Modal";

export function ConfirmDialog({
  title,
  text,
  confirmLabel,
  language,
  confirming,
  onCancel,
  onConfirm,
}: {
  title: string;
  text: string;
  confirmLabel?: string;
  language: Language;
  confirming: boolean;
  onCancel: () => void;
  onConfirm: () => void;
}) {
  const effectiveConfirmLabel = confirmLabel ?? translate("terms.confirmRemoval", language);
  return (
    <Modal ariaLabel={title}>
      <h2>{title}</h2>
      <p>{text}</p>
      <div className="modal-actions">
        <button className="quiet-button" disabled={confirming} onClick={onCancel}>{translate("common.cancel", language)}</button>
        <button className="danger-button" disabled={confirming} onClick={onConfirm}>{effectiveConfirmLabel}</button>
      </div>
    </Modal>
  );
}

export function RelatedGroupDialog({
  language,
  candidate,
  primary,
  selectedRoot,
  selectedRootSource,
  confirming,
  onPrimaryChange,
  onCancel,
  onConfirm,
}: {
  language: Language;
  candidate: RelatedTerm;
  primary: string;
  selectedRoot: string;
  selectedRootSource: string;
  confirming: boolean;
  onPrimaryChange: (value: string) => void;
  onCancel: () => void;
  onConfirm: () => void;
}) {
  const options = [
    {
      normalized: selectedRoot,
      source: selectedRootSource,
    },
    {
      normalized: candidate.group_root_normalized,
      source: candidate.group_root_source,
    },
  ].filter(
    (option, index, all) =>
      all.findIndex((item) => item.normalized === option.normalized) === index,
  );
  return (
    <Modal ariaLabel={translate("terms.relatedGroupTitle", language)}>
      <h2>{translate("terms.relatedGroupTitle", language)}</h2>
      <p>{translate("terms.relatedGroupText", language, { source: candidate.source })}</p>
      <div className="related-primary-options">
        {options.map((option) => (
          <label key={option.normalized} className="radio-option">
            <input
              type="radio"
              name="related-primary"
              checked={primary === option.normalized}
              onChange={() => onPrimaryChange(option.normalized)}
            />
            <span>{option.source}</span>
          </label>
        ))}
      </div>
      <div className="modal-actions">
        <button className="quiet-button" disabled={confirming} onClick={onCancel}>{translate("common.cancel", language)}</button>
        <button className="primary-button" disabled={confirming} onClick={onConfirm}>{translate("terms.relatedGroup", language)}</button>
      </div>
    </Modal>
  );
}

export function TermImportDialog({
  project,
  language,
  onClose,
  onImported,
}: {
  project: string;
  language: Language;
  onClose: () => void;
  onImported: (value: TermsResponse) => void;
}) {
  const [file, setFile] = useState<File | null>(null);
  const [error, setError] = useState("");
  const [saving, setSaving] = useState(false);

  async function submit() {
    if (!file) return;
    const body = new FormData();
    body.append("file", file);
    setSaving(true);
    try {
      await api(`/api/v1/projects/${project}/terms/import`, {
        method: "POST",
        body,
      });
      onImported(await api<TermsResponse>(`/api/v1/projects/${project}/terms`));
    } catch (value) {
      setError(errorMessage(value, language));
    } finally {
      setSaving(false);
    }
  }

  return (
    <Modal ariaLabel={translate("terms.importDialogTitle", language)}>
      <h2>{translate("terms.importDialogTitle", language)}</h2>
      <p>{translate("terms.importHint", language)}</p>
      <label>{translate("terms.termFile", language)}<input type="file" accept=".json,.csv" onChange={(event) => setFile(event.target.files?.[0] ?? null)} /></label>
      {error && <p className="error-text">{error}</p>}
      <div className="modal-actions">
        <button className="quiet-button" disabled={saving} onClick={onClose}>{translate("common.cancel", language)}</button>
        <button className="primary-button" disabled={saving || !file} onClick={submit}>{translate("terms.import", language)}</button>
      </div>
    </Modal>
  );
}

export function TermExportDialog({
  project,
  language,
  hasScanned,
  defaultSource,
  onClose,
}: {
  project: string;
  language: Language;
  hasScanned: boolean;
  defaultSource: "published" | "scanned";
  onClose: () => void;
}) {
  const [format, setFormat] = useState<"json" | "csv">("json");
  const [source, setSource] = useState<"published" | "scanned">(defaultSource);
  const [includeDisabled, setIncludeDisabled] = useState(false);
  const [error, setError] = useState("");

  async function download() {
    try {
      const response = await fetch(
        `/api/v1/projects/${project}/terms/export?format=${format}&include_disabled=${includeDisabled}&source=${source}`,
      );
      if (!response.ok) {
        throw await apiErrorFromResponse(response);
      }
      const url = URL.createObjectURL(await response.blob());
      const link = document.createElement("a");
      link.href = url;
      link.download = `${project}-terms.${format}`;
      link.click();
      URL.revokeObjectURL(url);
      onClose();
    } catch (value) {
      setError(errorMessage(value, language));
    }
  }

  return (
    <Modal ariaLabel={translate("terms.exportDialogTitle", language)}>
      <h2>{translate("terms.exportDialogTitle", language)}</h2>
      <label>{translate("terms.source", language)}<select value={source} onChange={(event) => setSource(event.target.value as "published" | "scanned")}><option value="published">{translate("terms.publishedTerms", language)}</option>{hasScanned && <option value="scanned">{translate("terms.scanCandidatesOption", language)}</option>}</select></label>
      <label>{translate("terms.format", language)}<select value={format} onChange={(event) => setFormat(event.target.value as "json" | "csv")}><option value="json">JSON</option><option value="csv">CSV</option></select></label>
      <label className="check-row"><input type="checkbox" checked={includeDisabled} onChange={(event) => setIncludeDisabled(event.target.checked)} />{translate("terms.includeRemoved", language)}</label>
      {error && <p className="error-text">{error}</p>}
      <div className="modal-actions">
        <button className="quiet-button" onClick={onClose}>{translate("common.cancel", language)}</button>
        <button className="primary-button" onClick={download}>{translate("terms.download", language)}</button>
      </div>
    </Modal>
  );
}

export function PartialPublishDialog({
  project,
  language,
  count,
  onClose,
  onPublished,
}: {
  project: string;
  language: Language;
  count: number;
  onClose: () => void;
  onPublished: () => Promise<void>;
}) {
  const [working, setWorking] = useState(false);
  const [error, setError] = useState("");

  async function confirm() {
    setWorking(true);
    setError("");
    try {
      await api(`/api/v1/projects/${project}/terms/publish-partial`, { method: "POST", body: JSON.stringify({ confirm: true }) });
      await onPublished();
    } catch (value) {
      setError(errorMessage(value, language));
    } finally {
      setWorking(false);
    }
  }

  return (
    <div className="modal-backdrop">
      <div className="modal" role="dialog" aria-modal="true" aria-label={translate("terms.publishTitle", language)}>
        <h2>{translate("terms.publishTitle", language)}</h2>
        <p>{translate("terms.publishText", language, { count })}</p>
        {error && <p className="error-text">{error}</p>}
        <div className="modal-actions">
          <button className="quiet-button" disabled={working} onClick={onClose}>{translate("common.cancel", language)}</button>
          <button className="primary-button" disabled={working || !count} onClick={confirm}>{working ? translate("terms.publishing", language) : translate("terms.publish", language)}</button>
        </div>
      </div>
    </div>
  );
}
