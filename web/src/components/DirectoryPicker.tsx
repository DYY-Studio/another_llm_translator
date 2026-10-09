import { useEffect, useRef, useState } from "react";
import { api } from "../api";
import { errorMessage, translate, type Language } from "../i18n";

type DirectoryPickerMode = "parent" | "project";
interface DirectoryEntry { name: string; path: string; is_project: boolean; }
interface DriveEntry { name: string; path: string; type: string; available: boolean; }
interface DirectoryListing { path: string; parent: string | null; is_project: boolean; directories: DirectoryEntry[]; drives: DriveEntry[]; }

function driveTypeLabel(type: string, language: Language) {
  const labels: Record<string, string> = { unknown: "drive.unknown", unavailable: "drive.unavailable", removable: "drive.removable", fixed: "drive.fixed", network: "drive.network", cdrom: "drive.cdrom", ramdisk: "drive.ramdisk" };
  return translate(labels[type] ?? type, language);
}

export function DirectoryPicker({
  initialPath,
  mode = "parent",
  language,
  onClose,
  onSelect,
}: {
  initialPath: string;
  mode?: DirectoryPickerMode;
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
      if (revision === requestRevision.current) setError(errorMessage(reason, language));
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
            <button type="button" className={`directory-entry directory-drive${entry.available ? "" : " unavailable"}`} disabled={loading || !entry.available} key={entry.path} onClick={() => void load(entry.path)}>
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
