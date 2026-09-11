import { useQuery } from "@tanstack/react-query";
import { useEffect, useMemo, useState } from "react";
import { errorMessage, translate, type Language } from "../i18n";
import { JsonPayloadViewer } from "./JsonPayloadViewer";
import {
  fetchHistoricalRequest,
  fetchHistoricalRun,
  fetchHistoricalRuns,
  queryKeys,
  type HistoricalRunQuery,
} from "../queries";
import type {
  HistoricalDebugAttempt,
  HistoricalDebugPayload,
  HistoricalExecutionSnapshots,
  HistoricalPromptVariant,
  HistoricalRequestIndex,
  HistoricalRunDetail,
  HistoricalRunSummary,
  HistoricalSnapshot,
  ProjectSummary,
} from "../types";

const HISTORY_LIMIT = 20;
const HISTORY_STAGES = [
  "terminology",
  "terminology_decision",
  "content_summary",
  "translation",
  "proofreading",
  "proofreading_applied",
  "polishing",
  "polishing_applied",
];
const HISTORY_STATUSES = [
  "active",
  "running",
  "completed",
  "failed",
  "interrupted",
  "reset",
  "partial_published",
];
type DetailTab = "summary" | "execution" | "requests";
type SnapshotTab = keyof HistoricalExecutionSnapshots;
type RequestTab = "request" | "response" | "attempts" | "error";

function stageLabel(stage: string, language: Language): string {
  const key = stage === "terminology_decision"
    ? "stage.terminologyDecision"
    : stage === "content_summary"
      ? "stage.contentSummary"
      : `stage.${stage}`;
  return translate(key, language);
}

function statusLabel(status: string, language: Language): string {
  const known = ["active", "running", "completed", "failed", "interrupted", "reset", "partial_published"];
  return known.includes(status)
    ? translate(`diagnostics.history.status.${status}`, language)
    : status;
}

function dateLabel(value: string | null | undefined, language: Language): string {
  if (!value) return translate("diagnostics.unavailable", language);
  const date = new Date(value);
  return Number.isNaN(date.getTime())
    ? value
    : date.toLocaleString(language === "en" ? "en-US" : "zh-CN", {
        year: "numeric",
        month: "2-digit",
        day: "2-digit",
        hour: "2-digit",
        minute: "2-digit",
      });
}

function countLabel(value: number | null | undefined, language: Language): string {
  return value == null
    ? translate("diagnostics.unavailable", language)
    : value.toLocaleString(language === "en" ? "en-US" : "zh-CN");
}

function snapshotStatusLabel(status: string, language: Language): string {
  return translate(`diagnostics.history.snapshot.${status}`, language);
}

function payloadStatus(value: string | HistoricalDebugPayload | undefined): string {
  return typeof value === "string" ? value : value?.status ?? "missing";
}

function formatValue(value: unknown): string {
  if (value == null) return "—";
  if (typeof value === "string") return value || "—";
  return JSON.stringify(value, null, 2);
}

function snapshotIsVariant(
  value: HistoricalExecutionSnapshots[SnapshotTab],
): value is { status: "available"; items: HistoricalPromptVariant[] } {
  return value.status === "available" && "items" in value;
}

function SnapshotBlock({
  snapshot,
  language,
}: {
  snapshot: HistoricalSnapshot;
  language: Language;
}) {
  if (snapshot.status !== "available" || snapshot.content == null) {
    return (
      <div className={`history-snapshot-state snapshot-${snapshot.status}`}>
        {snapshotStatusLabel(snapshot.status, language)}
      </div>
    );
  }
  return <pre className="history-code">{snapshot.content}</pre>;
}

function HistoricalRunRow({
  item,
  language,
  selected,
  onSelect,
}: {
  item: HistoricalRunSummary;
  language: Language;
  selected: boolean;
  onSelect: () => void;
}) {
  const completed = item.completed_segment_count ?? 0;
  const failed = item.failed_segment_count ?? 0;
  return (
    <button
      className={`history-run-row${selected ? " selected" : ""}`}
      aria-pressed={selected}
      onClick={onSelect}
    >
      <span className="history-run-main">
        <strong>{item.project_name}</strong>
        <span>{stageLabel(item.stage, language)} · {dateLabel(item.started_at, language)}</span>
      </span>
      <span className={`history-status status-${item.status}`}>
        {statusLabel(item.status, language)}
      </span>
      <span className="history-run-counts">
        {translate("diagnostics.history.segments", language, {
          completed: countLabel(completed, language),
          failed: countLabel(failed, language),
          requested: countLabel(item.requested_segment_count, language),
        })}
      </span>
      <span className="history-run-id">
        <code>{item.run_id}</code>
        <small>
          {item.debug_available
            ? translate("diagnostics.history.debugAvailable", language)
            : translate("diagnostics.history.debugUnavailable", language)}
        </small>
      </span>
    </button>
  );
}

function HistoricalRunSummaryPanel({
  detail,
  language,
}: {
  detail: HistoricalRunDetail;
  language: Language;
}) {
  const usage = detail.usage;
  const scopeEntries = detail.scope ? Object.entries(detail.scope) : [];
  return (
    <div className="history-summary-panel">
      <div className="history-info-grid">
        <div><span>{translate("diagnostics.history.project", language)}</span><strong>{detail.project_name}</strong></div>
        <div><span>{translate("diagnostics.history.stage", language)}</span><strong>{stageLabel(detail.stage, language)}</strong></div>
        <div><span>{translate("diagnostics.history.statusLabel", language)}</span><strong>{statusLabel(detail.status, language)}</strong></div>
        <div><span>{translate("diagnostics.history.startedAt", language)}</span><strong>{dateLabel(detail.started_at, language)}</strong></div>
        <div><span>{translate("diagnostics.history.completedAt", language)}</span><strong>{dateLabel(detail.completed_at, language)}</strong></div>
        <div><span>{translate("diagnostics.history.runId", language)}</span><code>{detail.run_id}</code></div>
      </div>
      <div className="history-count-grid">
        <div><span>{translate("diagnostics.history.selected", language)}</span><strong>{countLabel(detail.selected_segment_count, language)}</strong></div>
        <div><span>{translate("diagnostics.history.requested", language)}</span><strong>{countLabel(detail.requested_segment_count, language)}</strong></div>
        <div><span>{translate("diagnostics.history.reused", language)}</span><strong>{countLabel(detail.reused_segment_count, language)}</strong></div>
        <div><span>{translate("diagnostics.history.completed", language)}</span><strong>{countLabel(detail.completed_segment_count, language)}</strong></div>
        <div><span>{translate("diagnostics.history.failed", language)}</span><strong>{countLabel(detail.failed_segment_count, language)}</strong></div>
      </div>
      <section className="history-subsection">
        <h3>{translate("diagnostics.history.usage", language)}</h3>
        <p>
          {usage
            ? translate("diagnostics.history.usageValues", language, {
                input: countLabel(usage.input_tokens, language),
                output: countLabel(usage.output_tokens, language),
                total: countLabel(usage.total_tokens, language),
              })
            : translate("diagnostics.unavailable", language)}
        </p>
      </section>
      <section className="history-subsection">
        <h3>{translate("diagnostics.history.scope", language)}</h3>
        {scopeEntries.length ? (
          <dl className="history-scope-list">
            {scopeEntries.map(([key, value]) => <div key={key}><dt>{key}</dt><dd>{formatValue(value)}</dd></div>)}
          </dl>
        ) : <p>{translate("diagnostics.history.scopeUnavailable", language)}</p>}
      </section>
      <section className="history-subsection">
        <h3>{translate("diagnostics.history.warnings", language)}</h3>
        {detail.warnings.length ? (
          <ul className="history-warning-list">
            {detail.warnings.map((warning, index) => <li key={`${warning}-${index}`}>{warning}</li>)}
          </ul>
        ) : <p>{translate("diagnostics.history.noWarnings", language)}</p>}
      </section>
    </div>
  );
}

function HistoricalExecutionPanel({
  detail,
  language,
}: {
  detail: HistoricalRunDetail;
  language: Language;
}) {
  const [executionId, setExecutionId] = useState("root");
  const [snapshotTab, setSnapshotTab] = useState<SnapshotTab>("prompt");
  const execution = detail.executions.find((item) => item.id === executionId) ?? detail.executions[0];
  if (!execution) return <div className="diagnostics-empty">{translate("diagnostics.history.noExecutions", language)}</div>;
  const tabs: Array<[SnapshotTab, string]> = [
    ["prompt", translate("diagnostics.history.snapshotPrompt", language)],
    ["config", translate("diagnostics.history.snapshotConfig", language)],
    ["adapter", translate("diagnostics.history.snapshotAdapter", language)],
    ["preset", translate("diagnostics.history.snapshotPreset", language)],
    ["requirements", translate("diagnostics.history.snapshotRequirements", language)],
    ["prompt_variants", translate("diagnostics.history.snapshotVariants", language)],
  ];
  const snapshot = execution.snapshots[snapshotTab];
  return (
    <div className="history-execution-panel">
      <div className="history-execution-selector" role="tablist" aria-label={translate("diagnostics.history.executionSelector", language)}>
        {detail.executions.map((item, index) => (
          <button
            key={item.id}
            role="tab"
            aria-selected={execution.id === item.id}
            className={execution.id === item.id ? "active" : ""}
            onClick={() => setExecutionId(item.id)}
          >
            {item.kind === "root"
              ? translate("diagnostics.history.rootExecution", language)
              : translate("diagnostics.history.continuation", language, { count: index })}
          </button>
        ))}
      </div>
      <div className="history-execution-meta">
        <span>{translate("diagnostics.history.startedAt", language)} <strong>{dateLabel(execution.started_at, language)}</strong></span>
        {execution.fingerprint && <span>{translate("diagnostics.history.fingerprint", language)} <code>{execution.fingerprint}</code></span>}
        {execution.prompt_language && <span>{translate("diagnostics.history.promptLanguage", language)} <strong>{execution.prompt_language}</strong></span>}
      </div>
      <nav className="history-snapshot-tabs" role="tablist" aria-label={translate("diagnostics.history.snapshotSelector", language)}>
        {tabs.map(([key, label]) => (
          <button key={key} role="tab" aria-selected={snapshotTab === key} className={snapshotTab === key ? "active" : ""} onClick={() => setSnapshotTab(key)}>
            {label}
          </button>
        ))}
      </nav>
      {snapshotIsVariant(snapshot) ? (
        <div className="history-variant-list">
          {snapshot.items.length ? snapshot.items.map((variant) => (
            <article className="history-variant" key={variant.name}>
              <header><strong>{variant.name}</strong>{variant.primary_mode && <span>{variant.primary_mode}</span>}</header>
              {variant.requirements.length > 0 && <p>{variant.requirements.join(" · ")}</p>}
              <SnapshotBlock snapshot={variant.snapshot} language={language} />
            </article>
          )) : <div className="diagnostics-empty">{translate("diagnostics.history.noVariants", language)}</div>}
        </div>
      ) : (
        <SnapshotBlock snapshot={snapshot} language={language} />
      )}
    </div>
  );
}

function AttemptMeta({
  attempt,
  language,
}: {
  attempt: HistoricalDebugAttempt;
  language: Language;
}) {
  return (
    <span className="history-attempt-meta">
      {translate("diagnostics.history.attempt", language, { count: attempt.attempt })}
      {attempt.http_status == null ? ` · ${translate("diagnostics.networkError", language)}` : ` · HTTP ${attempt.http_status}`}
      {attempt.outcome ? ` · ${attempt.outcome}` : ""}
    </span>
  );
}

function PayloadPanel({
  payload,
  language,
}: {
  payload: HistoricalDebugPayload | undefined;
  language: Language;
}) {
  if (!payload || payload.status !== "available" || payload.value === undefined) {
    return <div className="history-payload-state">{snapshotStatusLabel(payload?.status ?? "missing", language)}</div>;
  }
  return (
    <>
      <p className="history-redaction-note">{translate("diagnostics.history.debugRedaction", language)}</p>
      <JsonPayloadViewer value={payload.value} language={language} />
    </>
  );
}

function HistoricalRequestDiagnostics({
  detail,
  language,
}: {
  detail: HistoricalRunDetail;
  language: Language;
}) {
  const index: HistoricalRequestIndex = detail.requests;
  const [requestId, setRequestId] = useState<string | null>(null);
  const [requestTab, setRequestTab] = useState<RequestTab>("request");
  const [attemptIndex, setAttemptIndex] = useState(0);
  const selectedRequest = index.items.find((item) => item.request_id === requestId) ?? null;
  const requestQuery = useQuery({
    queryKey: queryKeys.historicalRequest({
      project: detail.project_id,
      runId: detail.run_id,
      requestId: requestId ?? "",
      full: true,
    }),
    queryFn: ({ signal }) => fetchHistoricalRequest(
      detail.project_id,
      detail.run_id,
      requestId ?? "",
      true,
      signal,
    ),
    enabled: Boolean(requestId),
  });
  useEffect(() => {
    if (requestId && !selectedRequest) setRequestId(null);
  }, [requestId, selectedRequest]);
  useEffect(() => {
    setRequestTab("request");
    setAttemptIndex(0);
  }, [requestId]);
  const expanded = requestQuery.data;
  const activeAttempt = expanded?.attempts[Math.min(attemptIndex, Math.max(0, (expanded.attempts.length ?? 1) - 1))];
  const selectedPayload = requestTab === "request"
    ? activeAttempt?.request
    : requestTab === "response"
      ? activeAttempt?.response
      : requestTab === "error" && typeof activeAttempt?.error_payload !== "string"
        ? activeAttempt?.error_payload
        : undefined;
  return (
    <div className="history-request-panel">
      <div className="history-request-list">
        <header className="history-subsection-heading">
          <h3>{translate("diagnostics.history.requestList", language)}</h3>
          <span>{translate("diagnostics.history.requestCount", language, { count: index.items.length })}</span>
        </header>
        {index.status === "unavailable" && (
          <p className="history-unavailable">{translate("diagnostics.history.debugUnavailableDetail", language)}</p>
        )}
        {index.status === "partial" && (
          <p className="history-unavailable">{translate("diagnostics.history.debugPartial", language)}</p>
        )}
        {index.items.length ? index.items.map((item) => (
          <button className={`history-request-row${item.request_id === requestId ? " selected" : ""}`} key={item.request_id} onClick={() => setRequestId(item.request_id)}>
            <span><code>{item.request_id}</code>{item.parent_request_id && <small>{translate("diagnostics.history.parentRequest", language, { request: item.parent_request_id })}</small>}</span>
            <span>{translate("diagnostics.history.attemptCount", language, { count: item.attempt_count })}</span>
            <small>{item.attempts.length ? <AttemptMeta attempt={item.attempts[item.attempts.length - 1]} language={language} /> : ""}</small>
          </button>
        )) : <div className="diagnostics-empty">{translate("diagnostics.history.noRequests", language)}</div>}
        {index.errors?.map((error) => <small className="history-data-error" key={`${error.line}-${error.reason}`}>{translate("diagnostics.history.debugDataError", language, { line: error.line })}</small>)}
      </div>
      <div className="history-request-detail">
        {!requestId && <div className="diagnostics-empty">{translate("diagnostics.history.selectRequest", language)}</div>}
        {requestId && requestQuery.isPending && <div className="diagnostics-empty">{translate("diagnostics.history.loadingRequest", language)}</div>}
        {requestId && requestQuery.isError && <div className="warning-banner">{errorMessage(requestQuery.error, language)}</div>}
        {expanded && (
          <>
            <header className="history-subsection-heading">
              <div><h3>{expanded.request_id}</h3><span>{expanded.parent_request_id ? translate("diagnostics.history.parentRequest", language, { request: expanded.parent_request_id }) : ""}</span></div>
              <span>{expanded.status === "partial" ? translate("diagnostics.history.debugPartial", language) : translate("diagnostics.history.debugExpanded", language)}</span>
            </header>
            <div className="history-attempt-selector">
              {expanded.attempts.map((attempt, indexValue) => (
                <button key={attempt.attempt} className={indexValue === attemptIndex ? "active" : ""} onClick={() => setAttemptIndex(indexValue)}>
                  <AttemptMeta attempt={attempt} language={language} />
                </button>
              ))}
            </div>
            <nav className="history-request-tabs" role="tablist" aria-label={translate("diagnostics.history.requestTabs", language)}>
              {([
                ["request", translate("diagnostics.history.requestTab", language)],
                ["response", translate("diagnostics.history.responseTab", language)],
                ["attempts", translate("diagnostics.history.attemptsTab", language)],
                ["error", translate("diagnostics.history.errorTab", language)],
              ] as const).map(([tab, label]) => <button key={tab} role="tab" aria-selected={requestTab === tab} className={requestTab === tab ? "active" : ""} onClick={() => setRequestTab(tab)}>{label}</button>)}
            </nav>
            {requestTab === "attempts" ? (
              <div className="history-attempt-list">
                {expanded.attempts.map((attempt) => (
                  <article key={attempt.attempt}>
                    <strong><AttemptMeta attempt={attempt} language={language} /></strong>
                    <span>{translate("diagnostics.history.payloadStatuses", language, {
                      request: payloadStatus(attempt.request_payload),
                      response: payloadStatus(attempt.response_payload),
                      error: payloadStatus(attempt.error_payload),
                    })}</span>
                    {attempt.error && <p>{attempt.error}</p>}
                  </article>
                ))}
              </div>
            ) : (
              <PayloadPanel
                key={`${requestId ?? ""}-${attemptIndex}-${requestTab}`}
                payload={selectedPayload}
                language={language}
              />
            )}
          </>
        )}
      </div>
    </div>
  );
}

function HistoricalRunDetailPanel({
  detail,
  loading,
  error,
  language,
  onClose,
}: {
  detail: HistoricalRunDetail | undefined;
  loading: boolean;
  error: unknown;
  language: Language;
  onClose: () => void;
}) {
  const [tab, setTab] = useState<DetailTab>("summary");
  if (loading) return <section className="history-detail-panel"><div className="diagnostics-empty">{translate("diagnostics.history.loadingDetail", language)}</div></section>;
  if (error) return <section className="history-detail-panel"><div className="warning-banner">{errorMessage(error, language)}</div></section>;
  if (!detail) return null;
  return (
    <section className="history-detail-panel" aria-labelledby="history-detail-title">
      <header className="history-detail-heading">
        <div>
          <span className="history-eyebrow">{detail.project_name} · {stageLabel(detail.stage, language)}</span>
          <h2 id="history-detail-title"><code>{detail.run_id}</code></h2>
        </div>
        <div className="history-detail-actions">
          <span className="history-read-only">{translate("diagnostics.history.readOnly", language)}</span>
          <button className="quiet-button" onClick={onClose}>{translate("diagnostics.history.closeDetail", language)}</button>
        </div>
      </header>
      <nav className="history-detail-tabs" role="tablist" aria-label={translate("diagnostics.history.detailTabs", language)}>
        {([
          ["summary", translate("diagnostics.history.summaryTab", language)],
          ["execution", translate("diagnostics.history.executionTab", language)],
          ["requests", translate("diagnostics.history.requestsTab", language)],
        ] as const).map(([key, label]) => <button key={key} role="tab" aria-selected={tab === key} className={tab === key ? "active" : ""} onClick={() => setTab(key)}>{label}</button>)}
      </nav>
      {tab === "summary" && <HistoricalRunSummaryPanel detail={detail} language={language} />}
      {tab === "execution" && <HistoricalExecutionPanel detail={detail} language={language} />}
      {tab === "requests" && <HistoricalRequestDiagnostics detail={detail} language={language} />}
    </section>
  );
}

export function HistoricalDiagnosticsView({
  language,
  currentProject,
  projects,
}: {
  language: Language;
  currentProject: string;
  projects: ProjectSummary[];
}) {
  const [projectFilter, setProjectFilter] = useState(currentProject);
  const [stageFilter, setStageFilter] = useState("");
  const [statusFilter, setStatusFilter] = useState("");
  const [offset, setOffset] = useState(0);
  const [selectedRun, setSelectedRun] = useState<HistoricalRunSummary | null>(null);
  useEffect(() => setProjectFilter(currentProject), [currentProject]);
  useEffect(() => {
    setOffset(0);
    setSelectedRun(null);
  }, [projectFilter, stageFilter, statusFilter]);
  const query: HistoricalRunQuery = useMemo(() => ({
    project: projectFilter,
    stage: stageFilter,
    status: statusFilter,
    offset,
    limit: HISTORY_LIMIT,
  }), [projectFilter, stageFilter, statusFilter, offset]);
  const runsQuery = useQuery({
    queryKey: queryKeys.historicalRuns(query),
    queryFn: ({ signal }) => fetchHistoricalRuns(query, signal),
  });
  const detailQuery = useQuery({
    queryKey: queryKeys.historicalRun({ project: selectedRun?.project_id ?? "", runId: selectedRun?.run_id ?? "" }),
    queryFn: ({ signal }) => fetchHistoricalRun(selectedRun?.project_id ?? "", selectedRun?.run_id ?? "", signal),
    enabled: Boolean(selectedRun),
  });
  const items = runsQuery.data?.items ?? [];
  const total = runsQuery.data?.total ?? 0;
  const hasPrevious = offset > 0;
  const hasNext = offset + HISTORY_LIMIT < total;
  return (
    <div className="diagnostics-history">
      <header className="history-heading">
        <div>
          <h2>{translate("diagnostics.history.title", language)}</h2>
          <p>{translate("diagnostics.history.description", language)}</p>
        </div>
        <button className="quiet-button" onClick={() => void runsQuery.refetch()}>{translate("diagnostics.history.refresh", language)}</button>
      </header>
      <div className="history-filters">
        <label>{translate("diagnostics.history.projectFilter", language)}
          <select value={projectFilter} onChange={(event) => setProjectFilter(event.target.value)}>
            <option value="">{translate("diagnostics.history.allProjects", language)}</option>
            {projects.map((item) => <option value={item.selector} key={item.selector}>{item.name}</option>)}
          </select>
        </label>
        <label>{translate("diagnostics.history.stageFilter", language)}
          <select value={stageFilter} onChange={(event) => setStageFilter(event.target.value)}>
            <option value="">{translate("diagnostics.history.allStages", language)}</option>
            {HISTORY_STAGES.map((stage) => <option value={stage} key={stage}>{stageLabel(stage, language)}</option>)}
          </select>
        </label>
        <label>{translate("diagnostics.history.statusFilter", language)}
          <select value={statusFilter} onChange={(event) => setStatusFilter(event.target.value)}>
            <option value="">{translate("diagnostics.history.allStatuses", language)}</option>
            {HISTORY_STATUSES.map((status) => <option value={status} key={status}>{statusLabel(status, language)}</option>)}
          </select>
        </label>
      </div>
      {runsQuery.isError && <div className="warning-banner">{errorMessage(runsQuery.error, language)}</div>}
      <div className="history-list-panel">
        <div className="history-list-heading">
          <strong>{translate("diagnostics.history.total", language, { count: total })}</strong>
          <span>{total ? `${offset + 1}–${Math.min(offset + HISTORY_LIMIT, total)} / ${total}` : ""}</span>
        </div>
        {runsQuery.isPending ? <div className="diagnostics-empty">{translate("diagnostics.history.loading", language)}</div> : items.length ? (
          <div className="history-run-list">
            {items.map((item) => <HistoricalRunRow key={`${item.project_id}-${item.run_id}`} item={item} language={language} selected={selectedRun?.run_id === item.run_id && selectedRun.project_id === item.project_id} onSelect={() => setSelectedRun(item)} />)}
          </div>
        ) : <div className="diagnostics-empty">{translate("diagnostics.history.empty", language)}</div>}
        <div className="history-pagination">
          <button className="quiet-button" disabled={!hasPrevious || runsQuery.isFetching} onClick={() => setOffset((value) => Math.max(0, value - HISTORY_LIMIT))}>{translate("diagnostics.history.previous", language)}</button>
          <button className="quiet-button" disabled={!hasNext || runsQuery.isFetching} onClick={() => setOffset((value) => value + HISTORY_LIMIT)}>{translate("diagnostics.history.next", language)}</button>
        </div>
      </div>
      {selectedRun && <HistoricalRunDetailPanel detail={detailQuery.data} loading={detailQuery.isPending} error={detailQuery.isError ? detailQuery.error : null} language={language} onClose={() => setSelectedRun(null)} />}
    </div>
  );
}
