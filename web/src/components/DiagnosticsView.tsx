import { useVirtualizer } from "@tanstack/react-virtual";
import { memo, useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import { api } from "../api";
import type {
  DiagnosticsRequestDetail,
  DiagnosticsRequestStatus,
  DiagnosticsRequestSummary,
  DiagnosticsResponse,
} from "../types";
import { translate, type Language } from "../i18n";

type DetailTab = "request" | "content" | "reasoning" | "attempts";
type ThroughputMetric = "input" | "output" | "total";

const THROUGHPUT_STORAGE_KEY = "minimal-llm-translator.throughput.v1";

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
  bar.style.gridTemplateColumns = "repeat(6, max-content)";
  const widths = spans.map((span) => span.getBoundingClientRect().width);
  const inner = bar.clientWidth - 32;
  const gap = 24;
  let columns = 1;
  for (let k = 6; k >= 1; k--) {
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
                    {item.latest_latency_ms !== null ? ` · ${item.latest_latency_ms} ms` : ""}
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

export function DiagnosticsView({ language }: { language: Language }) {
  const statusLabels = useMemo(() => Object.fromEntries(
    ["running", "retrying", "completed", "failed", "interrupted"]
      .map((key) => [key, translate(`reqStatus.${key}`, language)]),
  ) as Record<DiagnosticsRequestStatus, string>, [language]);
  const [value, setValue] = useState<DiagnosticsResponse | null>(null);
  const [requestSummaries, setRequestSummaries] = useState<Map<string, DiagnosticsRequestSummary>>(
    () => new Map(),
  );
  const [requestTotal, setRequestTotal] = useState(0);
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
  const [detailTab, setDetailTab] = useState<DetailTab>("request");
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
  }, [language]);

  const load = useCallback(async () => {
    const loadId = ++summaryLoadRef.current;
    const params = new URLSearchParams();
    if (level) params.set("level", level);
    if (project) params.set("project", project);
    if (stage) params.set("stage", stage);
    if (query) params.set("q", query);
    const currentFeed = requestFeedRef.current;
    if (currentFeed.sessionId) {
      params.set("request_session", currentFeed.sessionId);
      params.set("request_after", String(currentFeed.cursor));
    }
    try {
      const nextValue = await api<DiagnosticsResponse>(
        `/api/v1/diagnostics${params.size ? `?${params}` : ""}`,
      );
      if (loadId !== summaryLoadRef.current) return;
      const feed = nextValue.requests;
      const previousSession = requestFeedRef.current.sessionId;
      if (feed.reset) {
        setRequestSummaries(new Map(
          feed.items.map((item) => [item.request_id, item]),
        ));
        if (previousSession && previousSession !== feed.session_id) {
          setSelectedRequest(null);
          setDetail(null);
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
      setRequestTotal(feed.total);
      setValue(nextValue);
      setError("");
    } catch (reason) {
      if (loadId === summaryLoadRef.current) setError(String(reason));
    }
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
        if (!stopped) setDetailError(String(reason));
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
    if (!selectedRequest) return;
    const closeOnEscape = (event: KeyboardEvent) => {
      if (event.key === "Escape") setSelectedRequest(null);
    };
    window.addEventListener("keydown", closeOnEscape);
    return () => window.removeEventListener("keydown", closeOnEscape);
  }, [selectedRequest]);

  const openDetail = useCallback((requestId: string) => {
    setSelectedRequest(requestId);
    setDetail(null);
    setDetailError("");
    setDetailTab("request");
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

  const metrics = value?.metrics;
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
    <section className="diagnostics-page">
      <header className="diagnostics-heading">
        <div>
          <h1>{translate("diagnostics.title", language)}</h1>
          <p>
            {metrics?.project
            ? translate("diagnostics.currentRun", language, { project: metrics.project ?? "", stage: metrics.stage ?? "" })
              : translate("diagnostics.noRun", language)}
          </p>
        </div>
        <span className="diagnostics-live"><i />{translate("diagnostics.live", language)}</span>
      </header>

      {error && <div className="warning-banner">{error}</div>}
      <div className="diagnostics-metrics">
        <article><span>{translate("diagnostics.currentRequests", language)}</span><strong>{number(metrics?.active_requests ?? 0, language)}</strong><small>{translate("diagnostics.concurrency", language)}</small></article>
        <article><span>{translate("diagnostics.totalRequests", language)}</span><strong>{number(metrics?.total_requests ?? 0, language)}</strong><small>{translate("diagnostics.logicalRequests", language)}</small></article>
        <article><span>{translate("diagnostics.inputTokens", language)}</span><strong>{metrics?.usage_available ? number(metrics.input_tokens, language) : translate("diagnostics.unavailable", language)}</strong><small>{translate("diagnostics.runTotal", language)}</small></article>
        <article><span>{translate("diagnostics.outputTokens", language)}</span><strong>{metrics?.usage_available ? number(metrics.output_tokens, language) : translate("diagnostics.unavailable", language)}</strong><small>{translate("diagnostics.runTotal", language)}</small></article>
        <article><span>{translate("diagnostics.throughput", language)}</span><strong>{number(throughput, language)}</strong><small>{translate("diagnostics.tokensPerSecond", language)}</small></article>
      </div>

      <div className="diagnostics-details" ref={detailsRef} aria-label={translate("diagnostics.requestSummary", language)}>
        <span>Usage <strong>{metrics?.usage_available ? translate("diagnostics.usageComplete", language) : translate("diagnostics.unavailable", language)}</strong></span>
        <span>{translate("diagnostics.latency", language)} <strong>{number(metrics?.latest_latency_ms ?? null, language, " ms")}</strong></span>
        <span>{translate("diagnostics.httpErrors", language)} <strong>{number(metrics?.http_errors ?? 0, language)}</strong></span>
        <span>{translate("diagnostics.retries", language)} <strong>{number(metrics?.retry_count ?? 0, language)}</strong></span>
        <span>{translate("diagnostics.rateLimitWaits", language)} <strong>{waitingRequests(metrics?.rate_limit_waiting_requests, language)}</strong></span>
        <span>{translate("diagnostics.throughputMetric", language)} <select aria-label={translate("diagnostics.throughputMetric", language)} value={throughputMetric} onChange={(event) => changeThroughputMetric(event.target.value as ThroughputMetric)}><option value="total">{translate("diagnostics.throughputTotal", language)}</option><option value="input">{translate("diagnostics.throughputInput", language)}</option><option value="output">{translate("diagnostics.throughputOutput", language)}</option></select></span>
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
            <div>
              <h2>{translate("diagnostics.currentRunRequests", language)}</h2>
              <span>{translate("diagnostics.requestRetention", language, { count: requestTotal })}</span>
            </div>
          </div>
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
          </div>
        </section>
      </div>

      {selectedRequest && (
        <div
          className="modal-backdrop"
          onMouseDown={(event) => {
            if (event.target === event.currentTarget) closeDetail();
          }}
        >
          <section className="modal exchange-dialog" role="dialog" aria-modal="true" aria-labelledby="exchange-dialog-title">
            <header className="exchange-dialog-heading">
              <div>
                <h2 id="exchange-dialog-title">{translate("diagnostics.detailsTitle", language)}</h2>
                <code>{selectedRequest}</code>
              </div>
              <button className="quiet-button" onClick={closeDetail} aria-label={translate("diagnostics.closeDetails", language)}>{translate("diagnostics.close", language)}</button>
            </header>
            <nav className="exchange-tabs" aria-label={translate("diagnostics.detailTabs", language)}>
              {([
                ["request", translate("diagnostics.tabRequest", language)],
                ["content", "Content"],
                ["reasoning", "Reasoning"],
                ["attempts", translate("diagnostics.tabAttempts", language)],
              ] as const).map(([tab, label]) => (
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
                  <span>{translate("diagnostics.model", language)} <strong>{detail.model}</strong></span>
                  <span>{translate("diagnostics.status", language)} <strong>{statusLabels[detail.status]}</strong></span>
                </div>
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
                        <strong>{translate("diagnostics.attempt", language, { count: attempt.attempt })}</strong>
                        <span>{attempt.http_status === null ? translate("diagnostics.networkError", language) : `HTTP ${attempt.http_status}`}</span>
                        <span>{attempt.latency_ms} ms</span>
                      </article>
                    )) : <div className="diagnostics-empty">{translate("diagnostics.noAttempts", language)}</div>}
                    {detail.error && <p className="error-text">{translate("diagnostics.errorCategory", language)}{detail.error}</p>}
                  </div>
                )}
              </div>
            )}
          </section>
        </div>
      )}
    </section>
  );
}
