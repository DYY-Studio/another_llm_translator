import { useCallback, useEffect, useLayoutEffect, useRef, useState } from "react";
import { api } from "../api";
import type { DiagnosticsRequestDetail, DiagnosticsResponse } from "../types";
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

function number(value: number | null, language: Language, suffix = "", unavailable = "不可用") {
  return value === null ? unavailable : `${value.toLocaleString(language === "en" ? "en-US" : "zh-CN")}${suffix}`;
}

function waitingRequests(value: number | undefined, language: Language) {
  return value ? (language === "en" ? `${number(value, language)} requests` : `${number(value, language)} 个请求`) : (language === "en" ? "None" : "无");
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

export function DiagnosticsView({ language }: { language: Language }) {
  const en = language === "en";
  const statusLabels = en ? {
    running: "Requesting", retrying: "Retrying", completed: "Completed", failed: "Failed", interrupted: "Interrupted",
  } : {
    running: "请求中", retrying: "重试中", completed: "已完成", failed: "失败", interrupted: "已中断",
  };
  const [value, setValue] = useState<DiagnosticsResponse | null>(null);
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
    const params = new URLSearchParams();
    if (level) params.set("level", level);
    if (project) params.set("project", project);
    if (stage) params.set("stage", stage);
    if (query) params.set("q", query);
    try {
      const summary = api<DiagnosticsResponse>(
        `/api/v1/diagnostics${params.size ? `?${params}` : ""}`,
      );
      const requestDetail = selectedRequest
        ? api<DiagnosticsRequestDetail>(
            `/api/v1/diagnostics/requests/${encodeURIComponent(selectedRequest)}`,
          ).then(
            (next) => ({ value: next, error: "" }),
            (reason) => ({ value: null, error: String(reason) }),
          )
        : Promise.resolve({ value: null, error: "" });
      const [nextValue, nextDetail] = await Promise.all([summary, requestDetail]);
      setValue(nextValue);
      if (selectedRequest) {
        setDetail(nextDetail.value);
        setDetailError(nextDetail.error);
      }
      setError("");
    } catch (reason) {
      setError(String(reason));
    }
  }, [level, project, stage, query, selectedRequest]);

  useEffect(() => {
    void load();
    const timer = window.setInterval(() => void load(), 1000);
    return () => window.clearInterval(timer);
  }, [load]);

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

  const openDetail = (requestId: string) => {
    setSelectedRequest(requestId);
    setDetail(null);
    setDetailError("");
    setDetailTab("request");
  };

  const closeDetail = () => {
    setSelectedRequest(null);
    setDetail(null);
    setDetailError("");
  };

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
            ? (en ? `Current run: ${metrics.project} · ${metrics.stage}` : `当前运行：${metrics.project} · ${metrics.stage}`)
              : translate("diagnostics.noRun", language)}
          </p>
        </div>
        <span className="diagnostics-live"><i />{translate("diagnostics.live", language)}</span>
      </header>

      {error && <div className="warning-banner">{error}</div>}
      <div className="diagnostics-metrics">
        <article><span>{translate("diagnostics.currentRequests", language)}</span><strong>{number(metrics?.active_requests ?? 0, language)}</strong><small>{translate("diagnostics.concurrency", language)}</small></article>
        <article><span>{translate("diagnostics.totalRequests", language)}</span><strong>{number(metrics?.total_requests ?? 0, language)}</strong><small>{translate("diagnostics.logicalRequests", language)}</small></article>
        <article><span>{translate("diagnostics.inputTokens", language)}</span><strong>{metrics?.usage_available ? number(metrics.input_tokens, language, "", en ? "Unavailable" : "不可用") : (en ? "Unavailable" : "不可用")}</strong><small>{translate("diagnostics.runTotal", language)}</small></article>
        <article><span>{translate("diagnostics.outputTokens", language)}</span><strong>{metrics?.usage_available ? number(metrics.output_tokens, language, "", en ? "Unavailable" : "不可用") : (en ? "Unavailable" : "不可用")}</strong><small>{translate("diagnostics.runTotal", language)}</small></article>
        <article><span>{translate("diagnostics.throughput", language)}</span><strong>{number(throughput, language, "", en ? "Unavailable" : "不可用")}</strong><small>{translate("diagnostics.tokensPerSecond", language)}</small></article>
      </div>

      <div className="diagnostics-details" ref={detailsRef} aria-label={en ? "Request diagnostics summary" : "请求诊断摘要"}>
        <span>Usage <strong>{metrics?.usage_available ? (en ? "Complete" : "完整") : (en ? "Unavailable" : "不可用")}</strong></span>
        <span>{en ? "Latency" : "请求延迟"} <strong>{number(metrics?.latest_latency_ms ?? null, language, " ms", en ? "Unavailable" : "不可用")}</strong></span>
        <span>{en ? "HTTP errors" : "HTTP 错误"} <strong>{number(metrics?.http_errors ?? 0, language)}</strong></span>
        <span>{en ? "Retries" : "重试"} <strong>{number(metrics?.retry_count ?? 0, language)}</strong></span>
        <span>{en ? "Rate-limit waits" : "限流等待"} <strong>{waitingRequests(metrics?.rate_limit_waiting_requests, language)}</strong></span>
        <span>{translate("diagnostics.throughputMetric", language)} <select aria-label={translate("diagnostics.throughputMetric", language)} value={throughputMetric} onChange={(event) => changeThroughputMetric(event.target.value as ThroughputMetric)}><option value="total">{translate("diagnostics.throughputTotal", language)}</option><option value="input">{translate("diagnostics.throughputInput", language)}</option><option value="output">{translate("diagnostics.throughputOutput", language)}</option></select></span>
      </div>

      <div className="diagnostics-grid">
        <section className="diagnostics-panel log-panel">
          <div className="diagnostics-panel-heading">
            <div><h2>{en ? "Global logs" : "全局日志"}</h2><span>{value?.logs.length ?? 0} {en ? "entries" : "条"}</span></div>
            <button
              className="quiet-button"
              aria-pressed={!autoScroll}
              onClick={() => setAutoScroll((current) => !current)}
            >
              {autoScroll ? (en ? "Pause auto-scroll" : "暂停自动滚动") : (en ? "Resume auto-scroll" : "恢复自动滚动")}
            </button>
          </div>
          <div className="diagnostics-filters">
            <select aria-label={en ? "Log level" : "日志级别"} value={level} onChange={(event) => setLevel(event.target.value)}>
              <option value="">{en ? "All levels" : "全部级别"}</option>
              {value?.filters.levels.map((item) => <option key={item}>{item}</option>)}
            </select>
            <select aria-label={en ? "Log project" : "日志项目"} value={project} onChange={(event) => setProject(event.target.value)}>
              <option value="">{en ? "All projects" : "全部项目"}</option>
              {value?.filters.projects.map((item) => <option key={item}>{item}</option>)}
            </select>
            <select aria-label={en ? "Log stage" : "日志阶段"} value={stage} onChange={(event) => setStage(event.target.value)}>
              <option value="">{en ? "All stages" : "全部阶段"}</option>
              {value?.filters.stages.map((item) => <option key={item}>{item}</option>)}
            </select>
            <input aria-label={en ? "Search logs" : "搜索日志"} placeholder={en ? "Search messages" : "搜索消息"} value={query} onChange={(event) => setQuery(event.target.value)} />
          </div>
          <div className="diagnostics-log" ref={logRef} role="log" aria-live="off">
            {value?.logs.length ? value.logs.map((item, index) => (
              <div className="diagnostics-log-row" key={`${item.timestamp}-${index}`}>
                <time>{clock(item.timestamp, language)}</time>
                <b className={`log-${item.level.toLowerCase()}`}>{item.level}</b>
                <span>{item.project} · {item.stage}</span>
                <code>{item.message}</code>
              </div>
            )) : <div className="diagnostics-empty">{en ? "No matching logs." : "没有符合条件的日志。"}</div>}
          </div>
        </section>

        <section className="diagnostics-panel request-panel">
          <div className="diagnostics-panel-heading">
            <div><h2>{en ? "Current run requests/responses" : "本次运行请求/响应"}</h2><span>{en ? "Latest 50 · memory only" : "最近 50 条 · 仅保存在内存"}</span></div>
          </div>
          <div className="request-list">
            {value?.requests.length ? [...value.requests].reverse().map((item) => (
              <article
                key={item.request_id}
                onDoubleClick={() => openDetail(item.request_id)}
              >
                <div className="request-row-main">
                  <header><code>{item.request_id}</code><time>{clock(item.timestamp, language)}</time></header>
                  <strong>{item.model}</strong>
                  <span>
                    <i className={`request-status status-${item.status}`}>{statusLabels[item.status]}</i>
                    {en ? `${item.attempt_count} attempts` : `${item.attempt_count} 次尝试`}
                    {item.last_http_status ? ` · HTTP ${item.last_http_status}` : ""}
                    {item.latest_latency_ms !== null ? ` · ${item.latest_latency_ms} ms` : ""}
                  </span>
                </div>
                <button className="quiet-button" onClick={() => openDetail(item.request_id)}>{en ? "View" : "查看"}</button>
              </article>
            )) : <div className="diagnostics-empty">{en ? "No requests in this run." : "本次运行尚无请求。"}</div>}
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
                <h2 id="exchange-dialog-title">{en ? "Request / response details" : "请求 / 响应详情"}</h2>
                <code>{selectedRequest}</code>
              </div>
              <button className="quiet-button" onClick={closeDetail} aria-label={en ? "Close details" : "关闭详情"}>{en ? "Close" : "关闭"}</button>
            </header>
            <nav className="exchange-tabs" aria-label={en ? "Detail tabs" : "详情分类"}>
              {([
                ["request", en ? "Request" : "请求"],
                ["content", "Content"],
                ["reasoning", "Reasoning"],
                ["attempts", en ? "Attempts" : "尝试"],
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
            {detailError ? (
              <div className="warning-banner">{detailError}</div>
            ) : !detail ? (
              <div className="diagnostics-empty">{en ? "Loading request details…" : "正在读取请求详情…"}</div>
            ) : (
              <div className="exchange-detail">
                <div className="exchange-meta">
                  <span>{en ? "Model" : "模型"} <strong>{detail.model}</strong></span>
                  <span>{en ? "Status" : "状态"} <strong>{statusLabels[detail.status]}</strong></span>
                </div>
                {detailTab === "request" && (
                  <div className="exchange-request-detail">
                    {Object.keys(detail.segment_id_map).length > 0 && (
                      <div className="exchange-id-map">
                        <strong>{en ? "Request-local IDs" : "请求内短 ID"}</strong>
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
                            {message.truncated && <span>{en ? "Truncated to 100,000 characters" : "已截断至 100,000 字符"}</span>}
                          </header>
                          <pre>{message.content}</pre>
                        </article>
                      ))}
                    </div>
                  </div>
                )}
                {detailTab === "content" && (
                  <article className="exchange-body">
                    {detail.response_content_truncated && <p>{en ? "Truncated to 100,000 characters." : "已截断至 100,000 字符。"}</p>}
                    <pre>{detail.response_content ?? (en ? "No Content." : "尚无 Content。")}</pre>
                  </article>
                )}
                {detailTab === "reasoning" && (
                  <article className="exchange-body">
                    {detail.reasoning_content_truncated && <p>{en ? "Truncated to 20,000 characters." : "已截断至 20,000 字符。"}</p>}
                    <pre>{detail.reasoning_content ?? (en ? "This request has no Reasoning." : "本请求没有 Reasoning。")}</pre>
                  </article>
                )}
                {detailTab === "attempts" && (
                  <div className="exchange-attempts">
                    {detail.attempts.length ? detail.attempts.map((attempt) => (
                      <article key={attempt.attempt}>
                        <strong>{en ? `Attempt ${attempt.attempt}` : `第 ${attempt.attempt} 次`}</strong>
                        <span>{attempt.http_status === null ? (en ? "Network error" : "网络错误") : `HTTP ${attempt.http_status}`}</span>
                        <span>{attempt.latency_ms} ms</span>
                      </article>
                    )) : <div className="diagnostics-empty">{en ? "No HTTP attempts yet." : "尚未开始 HTTP 尝试。"}</div>}
                    {detail.error && <p className="error-text">{en ? "Error category: " : "错误类别："}{detail.error}</p>}
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
