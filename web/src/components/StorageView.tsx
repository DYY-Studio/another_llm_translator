import { useEffect, useMemo, useState } from "react";
import { errorMessage, translate, type Language } from "../i18n";
import {
  clearDebugAttachments,
  clearGlobalLogs,
  clearOutputFile,
  clearProjectLogs,
  fetchStorage,
  fetchStorageProject,
} from "../queries";
import type {
  StorageCategory,
  StorageCategoryId,
  StorageCleanupResult,
  StorageDebugRun,
  StorageLogGroup,
  StorageOutputFile,
  StorageProjectDetail,
  StorageSummary,
} from "../types";

function formatSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  if (bytes < 1024 * 1024 * 1024) return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
  return `${(bytes / (1024 * 1024 * 1024)).toFixed(1)} GB`;
}

function formatScannedAt(value: string, language: Language): string {
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) return value;
  return parsed.toLocaleString(language);
}

function categoryLabel(id: StorageCategoryId, language: Language): string {
  return translate(`storage.category.${id}`, language);
}

function stageLabel(stage: string | null, language: Language): string {
  if (!stage) return translate("storage.unknown", language);
  const key = stage === "terminology_decision"
    ? "stage.terminologyDecision"
    : stage === "content_summary"
      ? "stage.contentSummary"
      : `stage.${stage}`;
  return translate(key, language);
}

function statusLabel(status: string | null, language: Language): string {
  if (!status) return translate("storage.unknown", language);
  const translated = translate(`run.${status}`, language);
  return translated === `run.${status}` ? status : translated;
}

function blockedLabel(reason: string | null, language: Language): string {
  if (!reason) return translate("storage.readOnly", language);
  const knownReasons: Record<string, string> = {
    "按策略只读": "storage.readOnly",
    "没有可清理日志": "storage.noReclaimableLogs",
    "没有可清理文件": "storage.noReclaimableFiles",
    "没有可清理附件": "storage.noReclaimableDebug",
    "扫描未完成或目标路径不安全": "storage.scanUnsafe",
    "目标路径不安全": "storage.unsafe",
    "扫描未完成": "storage.scanIncomplete",
    "项目存在活动 Web 任务": "storage.projectTaskRunning",
    "项目存在运行中的 Run": "storage.projectRunRunning",
    "无法确认项目 Run 状态": "storage.projectRunUnknown",
    "存在活动 Web 任务": "storage.globalTaskRunning",
    "Run 正在运行": "storage.runRunning",
    "该输出文件不可清理": "storage.outputProtected",
  };
  const key = knownReasons[reason];
  return key ? translate(key, language) : reason;
}

function ItemStatus({
  canClear,
  blockedReason,
  language,
}: {
  canClear: boolean;
  blockedReason: string | null;
  language: Language;
}) {
  return (
    <span className={canClear ? "storage-status ready" : "storage-status blocked"}>
      {canClear ? translate("storage.ready", language) : blockedLabel(blockedReason, language)}
    </span>
  );
}

function EmptyStorage({ language, text }: { language: Language; text?: string }) {
  return <p className="storage-empty muted">{text ?? translate("storage.noItems", language)}</p>;
}

export function StorageView({ language }: { language: Language }) {
  const [summary, setSummary] = useState<StorageSummary | null>(null);
  const [selectedProject, setSelectedProject] = useState<string | null>(null);
  const [detail, setDetail] = useState<StorageProjectDetail | null>(null);
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const [detailLoading, setDetailLoading] = useState(false);
  const [detailError, setDetailError] = useState("");
  const [error, setError] = useState("");
  const [message, setMessage] = useState("");
  const [busyAction, setBusyAction] = useState<string | null>(null);
  const [detailRevision, setDetailRevision] = useState(0);

  async function loadSummary(signal?: AbortSignal): Promise<StorageSummary> {
    const value = await fetchStorage(signal);
    setSummary(value);
    setSelectedProject((current) => (
      current && value.projects.some((item) => item.selector === current)
        ? current
        : null
    ));
    return value;
  }

  useEffect(() => {
    const controller = new AbortController();
    setLoading(true);
    setError("");
    void loadSummary(controller.signal)
      .catch((reason: unknown) => {
        if (!controller.signal.aborted) setError(errorMessage(reason, language));
      })
      .finally(() => {
        if (!controller.signal.aborted) setLoading(false);
      });
    return () => controller.abort();
  }, []);

  useEffect(() => {
    if (!selectedProject) {
      setDetail(null);
      setDetailLoading(false);
      setDetailError("");
      return;
    }
    const controller = new AbortController();
    setDetail(null);
    setDetailError("");
    setDetailLoading(true);
    void fetchStorageProject(selectedProject, controller.signal)
      .then((value) => setDetail(value))
      .catch((reason: unknown) => {
        if (!controller.signal.aborted) setDetailError(errorMessage(reason, language));
      })
      .finally(() => {
        if (!controller.signal.aborted) setDetailLoading(false);
      });
    return () => controller.abort();
  }, [selectedProject, detailRevision]);

  const projects = useMemo(
    () => [...(summary?.projects ?? [])].sort((left, right) => right.total_bytes - left.total_bytes),
    [summary?.projects],
  );
  const selectedSummary = summary?.projects.find((item) => item.selector === selectedProject) ?? null;

  function openProjectDetail(selector: string) {
    setError("");
    setMessage("");
    setSelectedProject(selector);
  }

  function closeProjectDetail() {
    if (busyAction !== null) return;
    setSelectedProject(null);
    setDetail(null);
    setDetailError("");
    setError("");
    setMessage("");
  }

  useEffect(() => {
    if (!selectedProject) return;
    function closeOnEscape(event: globalThis.KeyboardEvent) {
      if (event.key === "Escape" && busyAction === null) closeProjectDetail();
    }
    window.addEventListener("keydown", closeOnEscape);
    return () => window.removeEventListener("keydown", closeOnEscape);
  }, [busyAction, selectedProject]);

  async function refresh() {
    setRefreshing(true);
    setError("");
    setMessage("");
    try {
      await loadSummary();
      setDetailRevision((current) => current + 1);
    } catch (reason) {
      setError(errorMessage(reason, language));
    } finally {
      setRefreshing(false);
    }
  }

  async function runCleanup(
    key: string,
    confirmation: string,
    operation: () => Promise<StorageCleanupResult>,
  ) {
    if (!window.confirm(confirmation)) return;
    setBusyAction(key);
    setError("");
    setMessage("");
    try {
      const result = await operation();
      await loadSummary();
      setDetailRevision((current) => current + 1);
      setMessage(translate("storage.cleanupDone", language, {
        files: result.affected_files,
        size: formatSize(result.reclaimed_bytes),
      }));
    } catch (reason) {
      setError(errorMessage(reason, language));
    } finally {
      setBusyAction(null);
    }
  }

  function clearGlobalLogCategory(category: StorageCategory) {
    if (!category.can_clear || !summary?.complete) return;
    void runCleanup(
      "global-logs",
      translate("storage.confirmGlobalLogs", language),
      () => clearGlobalLogs(),
    );
  }

  function clearDebugRun(run: StorageDebugRun) {
    if (!selectedProject || !detail?.complete || !detail.project.complete || !run.can_clear) return;
    void runCleanup(
      `debug-${run.run_id}`,
      translate("storage.confirmDebug", language, { run: run.run_id, size: formatSize(run.bytes) }),
      () => clearDebugAttachments(selectedProject, run.run_id),
    );
  }

  function clearOutput(output: StorageOutputFile) {
    if (!selectedProject || !detail?.complete || !detail.project.complete || !output.can_clear) return;
    void runCleanup(
      `output-${output.path}`,
      translate("storage.confirmOutput", language, { path: output.path, size: formatSize(output.bytes) }),
      () => clearOutputFile(selectedProject, output.path),
    );
  }

  function clearProjectLog(log: StorageLogGroup) {
    if (!selectedProject || !detail?.complete || !detail.project.complete || !log.can_clear) return;
    void runCleanup(
      `project-logs-${log.id}`,
      translate("storage.confirmProjectLogs", language),
      () => clearProjectLogs(selectedProject),
    );
  }

  if (loading && !summary) {
    return <section className="storage-page"><p className="muted">{translate("storage.loading", language)}</p></section>;
  }

  return (
    <section className="storage-page">
      <header className="page-heading settings-action-heading storage-heading">
        <div>
          <h1>{translate("storage.title", language)}</h1>
          <p>{translate("storage.subtitle", language)}</p>
        </div>
        <button className="quiet-button" type="button" disabled={refreshing || busyAction !== null} onClick={() => void refresh()}>
          {refreshing ? translate("storage.refreshing", language) : translate("storage.refresh", language)}
        </button>
      </header>
      {!selectedProject && error && <div className="error-banner" role="alert">{error}</div>}
      {!selectedProject && message && <p className="success-text storage-message">{message}</p>}
      {summary && (
        <>
          <div className="storage-summary-strip">
            <div><span>{translate("storage.total", language)}</span><strong>{formatSize(summary.total_bytes)}</strong></div>
            <div><span>{translate("storage.reclaimable", language)}</span><strong>{formatSize(summary.reclaimable_bytes)}</strong></div>
            <div><span>{translate("storage.scannedAt", language)}</span><strong>{formatScannedAt(summary.scanned_at, language)}</strong></div>
          </div>
          {!summary.complete && (
            <div className="warning-banner" role="status">
              <strong>{translate("storage.scanIncomplete", language)}</strong>
              <p>{translate("storage.scanIncompleteHint", language)}</p>
              {summary.errors.length > 0 && <ul>{summary.errors.map((item) => <li key={item}>{item}</li>)}</ul>}
            </div>
          )}
          <div className="storage-overview-grid">
            <section className="storage-panel">
              <div className="storage-panel-heading">
                <div><h2>{translate("storage.global", language)}</h2><p>{translate("storage.globalHint", language)}</p></div>
              </div>
              <div className="storage-global-list">
                {summary.global.map((category) => (
                  <div className="storage-global-row" key={category.id}>
                    <div className="storage-global-main">
                      <strong>{categoryLabel(category.id, language)}</strong>
                      <span>{formatSize(category.bytes)} · {translate("storage.fileCount", language, { count: category.file_count })}</span>
                    </div>
                    <div className="storage-global-reclaimable">
                      <span>{translate("storage.reclaimable", language)}</span>
                      <strong>{formatSize(category.reclaimable_bytes)}</strong>
                    </div>
                    {category.id === "logs" ? (
                      <div className="storage-action-cell">
                        <button className="danger-button" type="button" disabled={busyAction !== null || !category.can_clear || !summary.complete} onClick={() => clearGlobalLogCategory(category)}>
                          {translate("storage.clearLogs", language)}
                        </button>
                        {!category.can_clear && <ItemStatus canClear={false} blockedReason={category.blocked_reason} language={language} />}
                      </div>
                    ) : <ItemStatus canClear={false} blockedReason={category.blocked_reason} language={language} />}
                  </div>
                ))}
              </div>
            </section>
            <section className="storage-panel storage-projects-panel">
              <div className="storage-panel-heading">
                <div><h2>{translate("storage.projects", language)}</h2><p>{translate("storage.projectsHint", language)}</p></div>
              </div>
              {projects.length === 0 ? <EmptyStorage language={language} text={translate("storage.noProjects", language)} /> : (
                <div className="storage-table-wrap storage-project-list">
                  <table className="storage-table storage-project-table">
                    <thead><tr><th>{translate("storage.project", language)}</th><th>{translate("storage.size", language)}</th><th>{translate("storage.reclaimable", language)}</th><th>{translate("storage.status", language)}</th></tr></thead>
                    <tbody>
                      {projects.map((item) => (
                        <tr key={item.selector}>
                          <td>
                            <button className="storage-project-select" type="button" aria-haspopup="dialog" onClick={() => openProjectDetail(item.selector)}>
                              <strong>{item.name}</strong><code>{item.path}</code>
                            </button>
                            {item.external && <small className="storage-project-meta">{translate("storage.external", language)}</small>}
                          </td>
                          <td>{formatSize(item.total_bytes)}</td>
                          <td>{formatSize(item.reclaimable_bytes)}</td>
                          <td><ItemStatus canClear={item.complete && item.reclaimable_bytes > 0} blockedReason={item.complete ? "没有可清理文件" : "扫描未完成"} language={language} /></td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              )}
            </section>
          </div>
        </>
      )}
      {selectedSummary && (
        <div className="modal-backdrop storage-detail-backdrop" onMouseDown={closeProjectDetail}>
          <section className="modal storage-detail-modal" role="dialog" aria-modal="true" aria-labelledby="storage-detail-title" onMouseDown={(event) => event.stopPropagation()}>
            <header className="storage-panel-heading storage-detail-heading">
              <div>
                <h2 id="storage-detail-title">{translate("storage.projectDetails", language)} · {selectedSummary.name}</h2>
                <p><code>{selectedSummary.path}</code></p>
              </div>
              <div className="storage-detail-actions">
                <span className="storage-detail-total">{formatSize(selectedSummary.total_bytes)}</span>
                <button className="quiet-button" type="button" disabled={busyAction !== null} onClick={closeProjectDetail}>{translate("storage.closeDetails", language)}</button>
              </div>
            </header>
            <div className="storage-detail-body">
              {error && <div className="error-banner" role="alert">{error}</div>}
              {message && <p className="success-text storage-message">{message}</p>}
              {detailLoading && <p className="muted">{translate("storage.detailLoading", language)}</p>}
              {detailError && <div className="error-banner" role="alert">{detailError}<button className="quiet-button" type="button" onClick={() => setDetailRevision((current) => current + 1)}>{translate("common.retry", language)}</button></div>}
              {detail && (
                <>
                  {!detail.complete && <div className="warning-banner"><strong>{translate("storage.scanIncomplete", language)}</strong>{detail.errors.length > 0 && <ul>{detail.errors.map((item) => <li key={item}>{item}</li>)}</ul>}</div>}
                  <div className="storage-detail-categories">
                    {detail.project.categories.map((category) => (
                      <div key={category.id}><span>{categoryLabel(category.id, language)}</span><strong>{formatSize(category.bytes)}</strong></div>
                    ))}
                  </div>
                  <StorageDebugSection language={language} runs={detail.debug_runs} busyAction={busyAction} onClear={clearDebugRun} />
                  <StorageOutputSection language={language} files={detail.output_files} busyAction={busyAction} onClear={clearOutput} />
                  <StorageLogsSection language={language} logs={detail.logs} busyAction={busyAction} onClear={clearProjectLog} />
                </>
              )}
            </div>
          </section>
        </div>
      )}
    </section>
  );
}

function StorageDebugSection({
  language,
  runs,
  busyAction,
  onClear,
}: {
  language: Language;
  runs: StorageDebugRun[];
  busyAction: string | null;
  onClear: (run: StorageDebugRun) => void;
}) {
  return (
    <section className="storage-detail-section">
      <div className="storage-section-heading"><div><h3>{translate("storage.debug", language)}</h3><p>{translate("storage.debugHint", language)}</p></div></div>
      {runs.length === 0 ? <EmptyStorage language={language} /> : (
        <div className="storage-table-wrap">
          <table className="storage-table">
            <thead><tr><th>{translate("storage.run", language)}</th><th>{translate("storage.stage", language)}</th><th>{translate("storage.status", language)}</th><th>{translate("storage.size", language)}</th><th>{translate("storage.action", language)}</th></tr></thead>
            <tbody>{runs.map((run) => (
              <tr key={run.run_id}>
                <td><code>{run.run_id}</code></td>
                <td>{stageLabel(run.stage, language)}</td>
                <td>{statusLabel(run.status, language)}</td>
                <td>{formatSize(run.bytes)}<small className="storage-cell-note">{translate("storage.fileCount", language, { count: run.file_count })}</small></td>
                <td><div className="storage-action-cell"><button className="danger-button" type="button" disabled={busyAction !== null || !run.can_clear} onClick={() => onClear(run)}>{translate("storage.clearDebug", language)}</button>{!run.can_clear && <ItemStatus canClear={false} blockedReason={run.blocked_reason} language={language} />}</div></td>
              </tr>
            ))}</tbody>
          </table>
        </div>
      )}
    </section>
  );
}

function StorageOutputSection({
  language,
  files,
  busyAction,
  onClear,
}: {
  language: Language;
  files: StorageOutputFile[];
  busyAction: string | null;
  onClear: (file: StorageOutputFile) => void;
}) {
  return (
    <section className="storage-detail-section">
      <div className="storage-section-heading"><div><h3>{translate("storage.output", language)}</h3><p>{translate("storage.outputHint", language)}</p></div></div>
      {files.length === 0 ? <EmptyStorage language={language} /> : (
        <div className="storage-table-wrap storage-output-list">
          <table className="storage-table">
            <thead><tr><th>{translate("storage.file", language)}</th><th>{translate("storage.size", language)}</th><th>{translate("storage.action", language)}</th></tr></thead>
            <tbody>{files.map((file) => (
              <tr key={file.path}>
                <td><code className="storage-path">{file.path}</code></td>
                <td>{formatSize(file.bytes)}</td>
                <td><div className="storage-action-cell"><button className="danger-button" type="button" disabled={busyAction !== null || !file.can_clear} onClick={() => onClear(file)}>{translate("storage.clearOutput", language)}</button>{!file.can_clear && <ItemStatus canClear={false} blockedReason={file.blocked_reason} language={language} />}</div></td>
              </tr>
            ))}</tbody>
          </table>
        </div>
      )}
    </section>
  );
}

function StorageLogsSection({
  language,
  logs,
  busyAction,
  onClear,
}: {
  language: Language;
  logs: StorageLogGroup[];
  busyAction: string | null;
  onClear: (log: StorageLogGroup) => void;
}) {
  return (
    <section className="storage-detail-section">
      <div className="storage-section-heading"><div><h3>{translate("storage.logs", language)}</h3><p>{translate("storage.logsHint", language)}</p></div></div>
      {logs.length === 0 ? <EmptyStorage language={language} /> : (
        <div className="storage-table-wrap">
          <table className="storage-table">
            <thead><tr><th>{translate("storage.logGroup", language)}</th><th>{translate("storage.size", language)}</th><th>{translate("storage.files", language)}</th><th>{translate("storage.action", language)}</th></tr></thead>
            <tbody>{logs.map((log) => (
              <tr key={log.id}>
                <td><code>{log.id}</code></td>
                <td>{formatSize(log.bytes)}</td>
                <td>{log.file_count}</td>
                <td><div className="storage-action-cell"><button className="danger-button" type="button" disabled={busyAction !== null || !log.can_clear} onClick={() => onClear(log)}>{translate("storage.clearLogs", language)}</button>{!log.can_clear && <ItemStatus canClear={false} blockedReason={log.blocked_reason} language={language} />}</div></td>
              </tr>
            ))}</tbody>
          </table>
        </div>
      )}
    </section>
  );
}
