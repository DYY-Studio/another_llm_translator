import { useVirtualizer } from "@tanstack/react-virtual";
import { memo, useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import { api } from "../api";
import { RequestOverview } from "./RequestOverview";
import { HistoricalDiagnosticsView } from "./HistoricalDiagnosticsView";
import type {
  DecisionActivity,
  DiagnosticsRequestDetail,
  DiagnosticsRequestStatus,
  DiagnosticsRequestSummary,
  DiagnosticsResponse,
} from "../types";
import type { ProjectSummary } from "../types";
import { errorMessage, translate, type Language } from "../i18n";
import { STORAGE_KEYS } from "../storageKeys";

type DetailTab = "overview" | "request" | "content" | "reasoning" | "attempts";
type ThroughputMetric = "input" | "output" | "total";

const THROUGHPUT_STORAGE_KEY = STORAGE_KEYS.throughput;

function readThroughputMetric(): ThroughputMetric {
  try {
    const stored = window.localStorage.getItem(THROUGHPUT_STORAGE_KEY);
    if (stored === "input" || stored === "output" || stored === "total") {
      return stored;
    }
  } catch {
    // Browser storage is optional; use the default for this page.
  }
  return "total";
}

function number(value: number | null, language: Language, suffix = "", unavailable = translate("diagnostics.unavailable", language)) {
  return value === null ? unavailable : `${value.toLocaleString(language === "en" ? "en-US" : "zh-CN")}${suffix}`;
}

function bytes(value: number, language: Language) {
  if (value < 1024) return `${value.toLocaleString(language === "en" ? "en-US" : "zh-CN")} B`;
  return `${(value / 1024).toLocaleString(language === "en" ? "en-US" : "zh-CN", { maximumFractionDigits: 1 })} KiB`;
}

function waitingRequests(value: number | undefined, language: Language) {
  return value
    ? translate("diagnostics.requestsLabel", language, { count: number(value, language) })
    : translate("diagnostics.none", language);
}

function clock(value: string, language: Language) {
  return new Date(value).toLocaleTimeString(language === "en" ? "en-US" : "zh-CN", {
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  });
}

function layoutDetailsColumns(bar: HTMLDivElement) {
  const spans = Array.from(bar.querySelectorAll<HTMLElement>(":scope > span"));
  if (!spans.length) return;
  const maxColumns = spans.length;
  bar.style.gridTemplateColumns = `repeat(${maxColumns}, max-content)`;
  const widths = spans.map((span) => span.getBoundingClientRect().width);
  const inner = bar.clientWidth - 32;
  const gap = 24;
  let columns = 1;
  for (let k = maxColumns; k >= 1; k--) {
    const tracks = new Array<number>(k).fill(0);
    for (let index = 0; index < widths.length; index++) {
      tracks[index % k] = Math.max(tracks[index % k], widths[index]);
    }
    const rowWidth = tracks.reduce((sum, width) => sum + width, 0) + (k - 1) * gap;
    if (rowWidth <= inner + 1) {
      columns = k;
      break;
    }
  }
  bar.style.gridTemplateColumns = `repeat(${columns}, max-content)`;
}

const RequestGroup = memo(function RequestGroup({
  className,
  items,
  language,
  onOpen,
  statusLabels,
  title,
}: {
  className: string;
  items: DiagnosticsRequestSummary[];
  language: Language;
  onOpen: (requestId: string) => void;
  statusLabels: Record<DiagnosticsRequestStatus, string>;
  title: string;
}) {
  const listRef = useRef<HTMLDivElement>(null);
  const virtualizer = useVirtualizer({
    count: items.length,
    getScrollElement: () => listRef.current,
    getItemKey: (index) => items[index]?.request_id ?? index,
    estimateSize: () => 82,
    overscan: 8,
  });

  return (
    <section className={`request-group ${className}`}>
      <header className="request-group-heading">
        <h3>{title}</h3>
        <span>{translate("diagnostics.requestsLabel", language, { count: items.length })}</span>
      </header>
      <div className="request-list" ref={listRef}>
        <div className="request-list-content" style={{ height: virtualizer.getTotalSize() }}>
          {virtualizer.getVirtualItems().map((virtualRow) => {
            const item = items[virtualRow.index];
            return (
              <article
                key={item.request_id}
                data-index={virtualRow.index}
                ref={virtualizer.measureElement}
                style={{ transform: `translateY(${virtualRow.start}px)` }}
                onDoubleClick={() => {
                  if (item.detail_available) onOpen(item.request_id);
                }}
              >
                <div className="request-row-main">
                  <header><code>{item.request_id}</code><time>{clock(item.timestamp, language)}</time></header>
                  <strong>{item.model}</strong>
                  <span>
                    <i className={`request-status status-${item.status}`}>{statusLabels[item.status]}</i>
                    {translate("diagnostics.attempts", language, { count: item.attempt_count })}
                    {item.last_http_status ? ` · HTTP ${item.last_http_status}` : ""}
                    {item.provider_error_status !== null ? ` · ${translate("diagnostics.providerErrorStatus", language, { status: item.provider_error_status })}` : ""}
                    {item.latest_latency_ms !== null ? ` · ${item.latest_latency_ms} ms` : ""}
                    {item.transport === "sse" ? ` · SSE ${item.stream_event_count} · ${bytes(item.stream_received_bytes, language)}${item.stream_first_event_latency_ms === null ? "" : ` · ${item.stream_first_event_latency_ms} ms first`}` : ""}
                  </span>
                </div>
                <button
                  className="quiet-button"
                  disabled={!item.detail_available}
                  title={!item.detail_available ? translate("diagnostics.detailExpired", language) : undefined}
                  onClick={() => onOpen(item.request_id)}
                >
                  {item.detail_available
                    ? translate("diagnostics.view", language)
                    : translate("diagnostics.expired", language)}
                </button>
              </article>
            );
          })}
        </div>
      </div>
    </section>
  );
});

function RuntimeDiagnosticsView({ language }: { language: Language }) {
  const statusLabels = useMemo(() => Object.fromEntries(
    ["running", "retrying", "completed", "failed", "interrupted"]
      .map((key) => [key, translate(`reqStatus.${key}`, language)]),
  ) as Record<DiagnosticsRequestStatus, string>, [language]);
  const [value, setValue] = useState<DiagnosticsResponse | null>(null);
  const [requestSummaries, setRequestSummaries] = useState<Map<string, DiagnosticsRequestSummary>>(
    () => new Map(),
  );
  const [requestKind, setRequestKind] = useState<"llm" | "decision">("llm");
  const [selectedActivity, setSelectedActivity] = useState<DecisionActivity | null>(null);
  const [decisionErrorsOnly, setDecisionErrorsOnly] = useState(false);
  const [decisionPage, setDecisionPage] = useState(0);
  const [pausedDecisionRequests, setPausedDecisionRequests] = useState<DiagnosticsRequestSummary[] | null>(null);
  const [level, setLevel] = useState("");
  const [project, setProject] = useState("");
  const [stage, setStage] = useState("");
  const [query, setQuery] = useState("");
  const [autoScroll, setAutoScroll] = useState(true);
  const [throughputMetric, setThroughputMetric] = useState<ThroughputMetric>(readThroughputMetric);
  const [error, setError] = useState("");
  const [selectedRequest, setSelectedRequest] = useState<string | null>(null);
  const [detail, setDetail] = useState<DiagnosticsRequestDetail | null>(null);
  const [detailError, setDetailError] = useState("");
  const [detailTab, setDetailTab] = useState<DetailTab>("overview");
  const logRef = useRef<HTMLDivElement>(null);
  const detailsRef = useRef<HTMLDivElement>(null);
  const requestFeedRef = useRef({ sessionId: "", cursor: 0 });
  const summaryLoadRef = useRef(0);

  useLayoutEffect(() => {
    const bar = detailsRef.current;
    if (!bar) return;
    layoutDetailsColumns(bar);
    if (typeof ResizeObserver === "undefined") return;
    const observer = new ResizeObserver(() => layoutDetailsColumns(bar));
    observer.observe(bar);
    return () => observer.disconnect();
  }, []);

  useEffect(() => {
    const bar = detailsRef.current;
    if (bar) layoutDetailsColumns(bar);
  }, [language, requestKind]);

  const load = useCallback(async () => {
    const loadId = ++summaryLoadRef.current;
    const payload: Record<string, unknown> = {};
    if (level) payload.level = level;
    if (project) payload.project = project;
    if (stage) payload.stage = stage;
    if (query) payload.q = query;
    const currentFeed = requestFeedRef.current;
    if (currentFeed.sessionId) {
      payload.request_session = currentFeed.sessionId;
      payload.request_after = currentFeed.cursor;
    }
    try {
      const nextValue = await api<DiagnosticsResponse>(
        "/api/v1/diagnostics",
        { method: "POST", body: JSON.stringify(payload) },
      );
      if (loadId !== summaryLoadRef.current) return;
      const feed = nextValue.requests;
      const previousSession = requestFeedRef.current.sessionId;
      if (feed.reset) {
        setRequestSummaries(new Map(
          feed.items.map((item) => [item.request_id, item]),
        ));
        if (previousSession && previousSession !== feed.session_id) {
          const retainedIds = new Set(feed.items.map((item) => item.request_id));
          setSelectedRequest((current) => current && retainedIds.has(current) ? current : null);
          setDetail((current) => current && retainedIds.has(current.request_id) ? current : null);
          setDetailError("");
        }
      } else if (feed.items.length) {
        setRequestSummaries((current) => {
          const next = new Map(current);
          for (const item of feed.items) next.set(item.request_id, item);
          return next;
        });
      }
      requestFeedRef.current = {
        sessionId: feed.session_id,
        cursor: feed.cursor,
      };
      setValue(nextValue);
      setError("");
    } catch (reason) {
      if (loadId === summaryLoadRef.current) setError(errorMessage(reason, language));
    }
  }, [level, project, stage, query]);

  useEffect(() => {
    summaryLoadRef.current += 1;
    requestFeedRef.current = { sessionId: "", cursor: 0 };
    setRequestSummaries(new Map());
    setSelectedActivity(null);
    setPausedDecisionRequests(null);
    setValue(null);
    setSelectedRequest(null);
    setDetail(null);
    setDetailError("");
    setError("");
  }, [level, project, stage, query]);

  useEffect(() => {
    let stopped = false;
    let timer: number | undefined;
    const poll = async () => {
      await load();
      if (!stopped) timer = window.setTimeout(() => void poll(), 1000);
    };
    void poll();
    return () => {
      stopped = true;
      if (timer !== undefined) window.clearTimeout(timer);
    };
  }, [load]);

  useEffect(() => {
    if (!selectedRequest) return;
    let stopped = false;
    let timer: number | undefined;
    const pollDetail = async () => {
      try {
        const next = await api<DiagnosticsRequestDetail>(
          `/api/v1/diagnostics/requests/${encodeURIComponent(selectedRequest)}`,
        );
        if (stopped) return;
        setDetail(next);
        setDetailError("");
        if (next.status === "running" || next.status === "retrying") {
          timer = window.setTimeout(() => void pollDetail(), 1000);
        }
      } catch (reason) {
        if (!stopped) setDetailError(errorMessage(reason, language));
      }
    };
    void pollDetail();
    return () => {
      stopped = true;
      if (timer !== undefined) window.clearTimeout(timer);
    };
  }, [selectedRequest]);

  useEffect(() => {
    if (autoScroll && logRef.current) {
      logRef.current.scrollTop = logRef.current.scrollHeight;
    }
  }, [autoScroll, value?.logs]);

  useEffect(() => {
    if (!selectedRequest && !selectedActivity) return;
    const closeOnEscape = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        if (selectedRequest) setSelectedRequest(null);
        else setSelectedActivity(null);
      }
    };
    window.addEventListener("keydown", closeOnEscape);
    return () => window.removeEventListener("keydown", closeOnEscape);
  }, [selectedRequest, selectedActivity]);

  const openDetail = useCallback((requestId: string) => {
    setSelectedRequest(requestId);
    setDetail(null);
    setDetailError("");
    setDetailTab("overview");
  }, []);

  const closeDetail = () => {
    setSelectedRequest(null);
    setDetail(null);
    setDetailError("");
  };

  const requestGroups = useMemo(() => {
    const active: DiagnosticsRequestSummary[] = [];
    const finished: DiagnosticsRequestSummary[] = [];
    for (const item of requestSummaries.values()) {
      if (item.request_kind !== "llm") continue;
      if (item.status === "running" || item.status === "retrying") {
        active.push(item);
      } else {
        finished.push(item);
      }
    }
    active.sort((left, right) => left.timestamp.localeCompare(right.timestamp));
    finished.sort((left, right) => (
      right.finished_at ?? right.timestamp
    ).localeCompare(left.finished_at ?? left.timestamp));
    return { active, finished };
  }, [requestSummaries]);

  const requestTotal = requestGroups.active.length + requestGroups.finished.length;
  const decisionActivities = value?.decision.activities ?? [];
  const decisionPending = decisionActivities.reduce((total, item) => total + item.pending, 0);
  const decisionFailures = decisionActivities.reduce((total, item) => total + item.failed + item.interrupted, 0);
  const metrics = value ? { ...value.metrics, ...(requestKind === "decision" ? value.decision.metrics : {}) } : undefined;
  const liveDecisionRequests = Array.from(requestSummaries.values()).filter((item) => (
    item.request_kind === "decision" && selectedActivity !== null
    && (item.task_id ?? null) === selectedActivity.task_id && item.model === selectedActivity.model
  )).sort((a, b) => b.timestamp.localeCompare(a.timestamp));
  const decisionRequests = (pausedDecisionRequests ?? liveDecisionRequests).filter((item) => (
    !decisionErrorsOnly || item.status === "failed" || item.status === "interrupted"
  ));
  const decisionPageCount = Math.max(1, Math.ceil(decisionRequests.length / 20));
  const currentDecisionPage = Math.min(decisionPage, decisionPageCount - 1);
  const isDecisionDetail = (detail?.request_kind ?? requestSummaries.get(selectedRequest ?? "")?.request_kind) === "decision";
  function openActivity(activity: DecisionActivity, errorsOnly = false) {
    setSelectedActivity(activity); setDecisionErrorsOnly(errorsOnly); setDecisionPage(0);
    setPausedDecisionRequests(null);
  }
  function closeActivity() {
    setSelectedActivity(null);
    setPausedDecisionRequests(null);
  }
  const throughput = metrics
    ? {
        input: metrics.throughput_input_tokens_per_second,
        output: metrics.throughput_output_tokens_per_second,
        total: metrics.throughput_tokens_per_second,
      }[throughputMetric]
    : null;

  function changeThroughputMetric(metric: ThroughputMetric) {
    setThroughputMetric(metric);
    try {
      window.localStorage.setItem(THROUGHPUT_STORAGE_KEY, metric);
    } catch {
      // The selected metric still applies for this page when storage is unavailable.
    }
  }

  return (
    <div className="diagnostics-runtime">
      {error && <div className="warning-banner">{error}</div>}
      <div className="diagnostics-metrics">
        <article><span>{translate("diagnostics.currentRequests", language)}</span><strong>{number(metrics?.active_requests ?? 0, language)}</strong><small>{translate("diagnostics.concurrency", language)}</small></article>
        <article><span>{translate("diagnostics.totalRequests", language)}</span><strong>{number(metrics?.total_requests ?? 0, language)}</strong><small>{translate("diagnostics.logicalRequests", language)}</small></article>
        <article><span>{translate("diagnostics.inputTokens", language)}</span><strong>{metrics?.usage_available || metrics?.usage_partial ? number(metrics.input_tokens, language) : translate("diagnostics.unavailable", language)}</strong><small>{translate("diagnostics.combinedUsage", language)}</small></article>
        <article><span>{translate("diagnostics.outputTokens", language)}</span><strong>{metrics?.usage_available || metrics?.usage_partial ? number(metrics.output_tokens, language) : translate("diagnostics.unavailable", language)}</strong><small>{translate("diagnostics.combinedUsage", language)}</small></article>
        <article><span>{translate(requestKind === "decision" ? "diagnostics.decisionRate" : "diagnostics.throughput", language)}</span><strong>{number(requestKind === "decision" ? decisionActivities.reduce((total, item) => total + (item.requests_per_second ?? 0), 0) : throughput, language)}</strong><small>{translate(requestKind === "decision" ? "diagnostics.requestsPerSecond" : "diagnostics.tokensPerSecond", language)}</small></article>
      </div>

      <div className="diagnostics-details" ref={detailsRef} aria-label={translate("diagnostics.requestSummary", language)}>
        <span>Usage <strong>{metrics?.usage_available ? translate("diagnostics.usageComplete", language) : metrics?.usage_partial ? translate("diagnostics.usagePartial", language) : translate("diagnostics.unavailable", language)}</strong></span>
        <span>{translate("diagnostics.averageLatency", language)} <strong>{number(metrics?.average_latency_ms ?? null, language, " ms")}</strong></span>
        <span title={requestKind === "decision" ? translate("diagnostics.decisionP95Hint", language) : undefined}>{translate("diagnostics.p95Latency", language)} <strong>{number(metrics?.p95_latency_ms ?? null, language, " ms")}</strong></span>
        <span>{translate("diagnostics.httpErrors", language)} <strong>{number(metrics?.http_errors ?? 0, language)}</strong></span>
        <span>{translate("diagnostics.retries", language)} <strong>{number(metrics?.retry_count ?? 0, language)}</strong></span>
        <span>{translate("diagnostics.rateLimitWaits", language)} <strong>{waitingRequests(metrics?.rate_limit_waiting_requests, language)}</strong></span>
        {requestKind === "llm" && <span>{translate("diagnostics.throughputMetric", language)} <select aria-label={translate("diagnostics.throughputMetric", language)} value={throughputMetric} onChange={(event) => changeThroughputMetric(event.target.value as ThroughputMetric)}><option value="total">{translate("diagnostics.throughputTotal", language)}</option><option value="input">{translate("diagnostics.throughputInput", language)}</option><option value="output">{translate("diagnostics.throughputOutput", language)}</option></select></span>}
      </div>

      <div className="diagnostics-grid">
        <section className="diagnostics-panel log-panel">
          <div className="diagnostics-panel-heading">
            <div><h2>{translate("diagnostics.globalLogs", language)}</h2><span>{value?.logs.length ?? 0} {translate("diagnostics.entries", language)}</span></div>
            <button
              className="quiet-button"
              aria-pressed={!autoScroll}
              onClick={() => setAutoScroll((current) => !current)}
            >
              {autoScroll ? translate("diagnostics.pauseAutoScroll", language) : translate("diagnostics.resumeAutoScroll", language)}
            </button>
          </div>
          <div className="diagnostics-filters">
            <select aria-label={translate("diagnostics.logLevel", language)} value={level} onChange={(event) => setLevel(event.target.value)}>
              <option value="">{translate("diagnostics.allLevels", language)}</option>
              {value?.filters.levels.map((item) => <option key={item}>{item}</option>)}
            </select>
            <select aria-label={translate("diagnostics.logProject", language)} value={project} onChange={(event) => setProject(event.target.value)}>
              <option value="">{translate("diagnostics.allProjects", language)}</option>
              {value?.filters.projects.map((item) => <option key={item}>{item}</option>)}
            </select>
            <select aria-label={translate("diagnostics.logStage", language)} value={stage} onChange={(event) => setStage(event.target.value)}>
              <option value="">{translate("diagnostics.allStages", language)}</option>
              {value?.filters.stages.map((item) => <option key={item}>{item}</option>)}
            </select>
            <input aria-label={translate("diagnostics.searchLogs", language)} placeholder={translate("diagnostics.searchMessages", language)} value={query} onChange={(event) => setQuery(event.target.value)} />
          </div>
          <div className="diagnostics-log" ref={logRef} role="log" aria-live="off">
            {value?.logs.length ? value.logs.map((item, index) => (
              <div className="diagnostics-log-row" key={`${item.timestamp}-${index}`}>
                <time>{clock(item.timestamp, language)}</time>
                <b className={`log-${item.level.toLowerCase()}`}>{item.level}</b>
                <span>{item.project} · {item.stage}</span>
                <code>{item.message}</code>
              </div>
            )) : <div className="diagnostics-empty">{translate("diagnostics.noLogs", language)}</div>}
          </div>
        </section>

        <section className="diagnostics-panel request-panel">
          <div className="diagnostics-panel-heading">
            <nav className="diagnostics-subtabs" aria-label={translate("diagnostics.requestKind", language)}>
              <button className={requestKind === "llm" ? "active" : ""} aria-pressed={requestKind === "llm"} onClick={() => setRequestKind("llm")}>LLM</button>
              <button className={requestKind === "decision" ? "active" : ""} aria-pressed={requestKind === "decision"} title={translate("diagnostics.decisionIndicator", language, { pending: decisionPending, failed: decisionFailures })} onClick={() => setRequestKind("decision")}>
                Decision{decisionPending > 0 ? ` (${decisionPending})` : ""}{decisionFailures > 0 && <i className="request-status status-failed"> · !{decisionFailures}</i>}
              </button>
            </nav>
            <span>{translate(requestKind === "llm" ? "diagnostics.requestRetention" : "diagnostics.decisionCumulative", language, { count: requestTotal })}</span>
          </div>
          {requestKind === "decision" ? <div className="request-list decision-activities">
            {decisionActivities.length ? decisionActivities.map((activity) => <article key={`${activity.task_id ?? ""}:${activity.model}`}>
              <div className="request-row-main">
                <header><span>{activity.project} · {activity.stage}</span><code>{activity.task_id}</code></header>
                <strong>{activity.model}</strong>
                <span>{translate("diagnostics.decisionCounts", language, { pending: activity.pending, completed: activity.completed, total: activity.total_requests, questions: activity.questions })}</span>
                <span>{number(activity.requests_per_second, language)} {translate("diagnostics.requestsPerSecond", language)} · {translate("diagnostics.averageLatency", language)} {number(activity.average_latency_ms, language, " ms")}</span>
                <span title={translate("diagnostics.decisionP95Hint", language)}>P95 {number(activity.p95_latency_ms, language, " ms")}</span>
              </div>
              <div className="button-group">
                <button className="quiet-button" onClick={() => openActivity(activity)}>{translate("diagnostics.view", language)}</button>
                {(activity.failed + activity.interrupted > 0) && <button className="quiet-button error-text" onClick={() => openActivity(activity, true)}>{translate("diagnostics.decisionErrors", language, { count: activity.failed + activity.interrupted })}</button>}
              </div>
            </article>) : <div className="diagnostics-empty">{translate("diagnostics.decisionEmpty", language)}</div>}
          </div> :
          <div className={`request-groups ${requestTotal ? "has-requests" : ""} ${!requestGroups.active.length || !requestGroups.finished.length ? "single" : ""}`}>
            {requestGroups.active.length > 0 && (
              <RequestGroup
                className="request-group-active"
                items={requestGroups.active}
                language={language}
                onOpen={openDetail}
                statusLabels={statusLabels}
                title={translate("diagnostics.activeRequests", language)}
              />
            )}
            {requestGroups.finished.length > 0 && (
              <RequestGroup
                className="request-group-finished"
                items={requestGroups.finished}
                language={language}
                onOpen={openDetail}
                statusLabels={statusLabels}
                title={translate("diagnostics.finishedRequests", language)}
              />
            )}
            {!requestTotal && (
              <div className="diagnostics-empty request-groups-empty">
                {translate("diagnostics.noRequests", language)}
              </div>
            )}
          </div>}
        </section>
      </div>

      {selectedActivity && !selectedRequest && <div className="modal-backdrop" onMouseDown={(event) => { if (event.target === event.currentTarget) closeActivity(); }}>
        <section className="modal exchange-dialog decision-dialog" role="dialog" aria-modal="true" aria-label={translate("diagnostics.decisionDetails", language)}>
          <header className="exchange-dialog-heading">
            <div><h2>{translate("diagnostics.decisionDetails", language)}</h2><code>{selectedActivity.project} · {selectedActivity.model}</code></div>
            <div className="button-group">
              <button className="quiet-button" aria-pressed={pausedDecisionRequests !== null} onClick={() => {
                setPausedDecisionRequests(pausedDecisionRequests === null ? liveDecisionRequests : null);
              }}>{translate(pausedDecisionRequests === null ? "diagnostics.pauseRefresh" : "diagnostics.resumeRefresh", language)}</button>
              <button className="quiet-button" onClick={closeActivity}>{translate("diagnostics.close", language)}</button>
            </div>
          </header>
          <p className="muted">{translate("diagnostics.decisionRetention", language)}</p>
          <nav className="exchange-tabs">
            <button className={!decisionErrorsOnly ? "active" : ""} onClick={() => { setDecisionErrorsOnly(false); setDecisionPage(0); }}>{translate("diagnostics.decisionAll", language)}</button>
            <button className={decisionErrorsOnly ? "active" : ""} onClick={() => { setDecisionErrorsOnly(true); setDecisionPage(0); }}>{translate("diagnostics.decisionFailed", language)}</button>
          </nav>
          <div className="exchange-detail">
            {decisionRequests.length ? <div className="decision-request-cards">{decisionRequests.slice(currentDecisionPage * 20, (currentDecisionPage + 1) * 20).map((item) => <article key={item.request_id}>
              <header className="overview-request-heading"><code>{item.segment_id ?? "—"}</code><time>{clock(item.timestamp, language)}</time><span className={`request-status status-${item.status}`}>{statusLabels[item.status]}</span><span>{number(item.latest_latency_ms, language, " ms")} · {translate("diagnostics.tabAttempts", language)} {item.attempt_count}</span><button className="quiet-button" onClick={() => openDetail(item.request_id)}>{translate("diagnostics.view", language)}</button></header>
              <RequestOverview snapshot={item.overview} truncated={item.overview_truncated} language={language} />
            </article>)}</div> : <div className="diagnostics-empty">{translate("diagnostics.decisionNoRecent", language)}</div>}
          </div>
          <div className="history-pagination">
            <button className="quiet-button" disabled={currentDecisionPage === 0} onClick={() => setDecisionPage(currentDecisionPage - 1)}>{translate("diagnostics.history.previous", language)}</button>
            <span>{currentDecisionPage + 1} / {decisionPageCount}</span>
            <button className="quiet-button" disabled={currentDecisionPage + 1 >= decisionPageCount} onClick={() => setDecisionPage(currentDecisionPage + 1)}>{translate("diagnostics.history.next", language)}</button>
          </div>
        </section>
      </div>}

      {selectedRequest && (
        <div
          className="modal-backdrop"
          onMouseDown={(event) => {
            if (event.target === event.currentTarget) closeDetail();
          }}
        >
          <section className="modal exchange-dialog request-overview-dialog" role="dialog" aria-modal="true" aria-labelledby="exchange-dialog-title">
            <header className="exchange-dialog-heading">
              <div>
                <h2 id="exchange-dialog-title">{translate("diagnostics.detailsTitle", language)}</h2>
                <code>{selectedRequest}</code>
              </div>
              <button className="quiet-button" onClick={closeDetail} aria-label={translate("diagnostics.closeDetails", language)}>{translate("diagnostics.close", language)}</button>
            </header>
            <nav className="exchange-tabs" aria-label={translate("diagnostics.detailTabs", language)}>
              {([
                ["overview", translate("overview.title", language)],
                ["request", translate("diagnostics.tabRequest", language)],
                ["content", isDecisionDetail ? translate("diagnostics.decisionResponse", language) : "Content"],
                ["reasoning", "Reasoning"],
                ["attempts", translate("diagnostics.tabAttempts", language)],
              ] as const).filter(([tab]) => tab !== "reasoning" || !isDecisionDetail).map(([tab, label]) => (
                <button
                  key={tab}
                  className={detailTab === tab ? "active" : ""}
                  aria-pressed={detailTab === tab}
                  onClick={() => setDetailTab(tab)}
                >
                  {label}
                </button>
              ))}
            </nav>
            {detailError && <div className="warning-banner">{detailError}</div>}
            {!detail ? (
              !detailError && <div className="diagnostics-empty">{translate("diagnostics.loadingDetails", language)}</div>
            ) : (
              <div className="exchange-detail">
                <div className="exchange-meta">
                  {detail.request_kind === "decision" && <span>Decision · {detail.segment_id} · {translate("diagnostics.decisionQuestions", language)} {detail.question_count}</span>}
                  <span>{translate("diagnostics.model", language)} <strong>{detail.model}</strong></span>
                  <span>{translate("diagnostics.status", language)} <strong>{statusLabels[detail.status]}</strong></span>
                  {detail.transport === "sse" && <span>{translate("diagnostics.streamProgress", language)} <strong>{detail.stream_event_count} events · {bytes(detail.stream_received_bytes, language)}{detail.stream_first_event_latency_ms === null ? "" : ` · ${detail.stream_first_event_latency_ms} ms first`}</strong></span>}
                </div>
                {detailTab === "overview" && <RequestOverview snapshot={detail.overview} truncated={detail.overview_truncated || detail.response_content_truncated} language={language} />}
                {detailTab === "request" && (
                  <div className="exchange-request-detail">
                    {Object.keys(detail.segment_id_map).length > 0 && (
                      <div className="exchange-id-map">
                        <strong>{translate("diagnostics.requestLocalIds", language)}</strong>
                        {Object.entries(detail.segment_id_map).map(([shortId, segmentId]) => (
                          <code key={shortId}>{shortId} → {segmentId}</code>
                        ))}
                      </div>
                    )}
                    {detail.request_kind === "decision" && <article className="exchange-body">
                      {detail.request_body_truncated && <p>{translate("diagnostics.truncated100kDot", language)}</p>}
                      <pre>{detail.request_body}</pre>
                    </article>}
                    <div className="exchange-messages">
                      {detail.messages.map((message, index) => (
                        <article key={`${message.role}-${index}`}>
                          <header>
                            <strong>{message.role || "message"}</strong>
                            {message.truncated && <span>{translate("diagnostics.truncated100k", language)}</span>}
                          </header>
                          <pre>{message.content}</pre>
                        </article>
                      ))}
                    </div>
                  </div>
                )}
                {detailTab === "content" && (
                  <article className="exchange-body">
                    {detail.response_content_truncated && <p>{translate("diagnostics.truncated100kDot", language)}</p>}
                    <pre>{detail.response_content ?? translate("diagnostics.noContent", language)}</pre>
                  </article>
                )}
                {detailTab === "reasoning" && (
                  <article className="exchange-body">
                    {detail.reasoning_content_truncated && <p>{translate("diagnostics.truncated20k", language)}</p>}
                    <pre>{detail.reasoning_content ?? translate("diagnostics.noReasoning", language)}</pre>
                  </article>
                )}
                {detailTab === "attempts" && (
                  <div className="exchange-attempts">
                    {detail.attempts.length ? detail.attempts.map((attempt) => (
                      <article key={attempt.attempt}>
                        <strong>{translate("diagnostics.attempt", language, { count: attempt.attempt })}{attempt.retry_round == null ? "" : ` · ${translate("diagnostics.retryRound", language, { count: attempt.retry_round })}`}{attempt.key_index == null ? "" : ` · ${translate("diagnostics.key", language, { count: attempt.key_index })}`}</strong>
                        <span>{attempt.http_status === null ? translate("diagnostics.networkError", language) : `HTTP ${attempt.http_status}`}</span>
                        {attempt.provider_error_status !== null && <span>{translate("diagnostics.providerErrorStatus", language, { status: attempt.provider_error_status })}</span>}
                        <span>{translate("diagnostics.outcome", language, { outcome: attempt.outcome })}</span>
                        <span>{attempt.latency_ms} ms</span>
                        {detail.transport === "sse" && <span>{attempt.stream_event_count ?? 0} events · {bytes(attempt.stream_received_bytes ?? 0, language)}{attempt.stream_first_event_latency_ms == null ? "" : ` · ${attempt.stream_first_event_latency_ms} ms first`}</span>}
                      </article>
                    )) : <div className="diagnostics-empty">{translate(detail.request_kind === "decision" && detail.status === "failed" ? "diagnostics.decisionPreflight" : "diagnostics.noAttempts", language)}</div>}
                    {detail.error && <p className="error-text">{translate("diagnostics.errorCategory", language)}{detail.error}</p>}
                  </div>
                )}
              </div>
            )}
          </section>
        </div>
      )}
    </div>
  );
}

export function DiagnosticsView({
  language,
  project,
  projects,
}: {
  language: Language;
  project: string;
  projects: ProjectSummary[];
}) {
  const [tab, setTab] = useState<"runtime" | "history">("runtime");
  return (
    <section className="diagnostics-page">
      <header className="diagnostics-heading diagnostics-shell-heading">
        <div>
          <h1>{translate("diagnostics.title", language)}</h1>
          <p>{translate(tab === "runtime" ? "diagnostics.runtimeDescription" : "diagnostics.historyDescription", language)}</p>
        </div>
        <nav className="diagnostics-subtabs" role="tablist" aria-label={translate("diagnostics.subtabs", language)}>
          <button role="tab" aria-selected={tab === "runtime"} className={tab === "runtime" ? "active" : ""} onClick={() => setTab("runtime")}>{translate("diagnostics.runtimeTab", language)}</button>
          <button role="tab" aria-selected={tab === "history"} className={tab === "history" ? "active" : ""} onClick={() => setTab("history")}>{translate("diagnostics.historyTab", language)}</button>
        </nav>
      </header>
      <div className="diagnostics-view-content">
        {tab === "runtime"
          ? <RuntimeDiagnosticsView language={language} />
          : <HistoricalDiagnosticsView language={language} currentProject={project} projects={projects} />}
      </div>
    </section>
  );
}
