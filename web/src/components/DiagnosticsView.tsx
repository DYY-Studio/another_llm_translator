import { useCallback, useEffect, useRef, useState } from "react";
import { api } from "../api";
import type { DiagnosticsRequestDetail, DiagnosticsResponse } from "../types";

type DetailTab = "request" | "content" | "reasoning" | "attempts";

function number(value: number | null, suffix = "") {
  return value === null ? "不可用" : `${value.toLocaleString()}${suffix}`;
}

function waitingRequests(value: number | undefined) {
  return value ? `${value.toLocaleString()} 个请求` : "无";
}

function clock(value: string) {
  return new Date(value).toLocaleTimeString([], {
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  });
}

const statusLabels = {
  running: "请求中",
  retrying: "重试中",
  completed: "已完成",
  failed: "失败",
  interrupted: "已中断",
};

export function DiagnosticsView() {
  const [value, setValue] = useState<DiagnosticsResponse | null>(null);
  const [level, setLevel] = useState("");
  const [project, setProject] = useState("");
  const [stage, setStage] = useState("");
  const [query, setQuery] = useState("");
  const [autoScroll, setAutoScroll] = useState(true);
  const [error, setError] = useState("");
  const [selectedRequest, setSelectedRequest] = useState<string | null>(null);
  const [detail, setDetail] = useState<DiagnosticsRequestDetail | null>(null);
  const [detailError, setDetailError] = useState("");
  const [detailTab, setDetailTab] = useState<DetailTab>("request");
  const logRef = useRef<HTMLDivElement>(null);

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
  return (
    <section className="diagnostics-page">
      <header className="diagnostics-heading">
        <div>
          <h1>诊断仪表盘</h1>
          <p>
            {metrics?.project
              ? `当前运行：${metrics.project} · ${metrics.stage}`
              : "当前没有运行中的 LLM 任务"}
          </p>
        </div>
        <span className="diagnostics-live"><i />每秒刷新</span>
      </header>

      {error && <div className="warning-banner">{error}</div>}
      <div className="diagnostics-metrics">
        <article><span>当前请求</span><strong>{number(metrics?.active_requests ?? 0)}</strong><small>并发数</small></article>
        <article><span>输入 Tokens</span><strong>{metrics?.usage_available ? number(metrics.input_tokens) : "不可用"}</strong><small>当前 Run 精确累计</small></article>
        <article><span>输出 Tokens</span><strong>{metrics?.usage_available ? number(metrics.output_tokens) : "不可用"}</strong><small>当前 Run 精确累计</small></article>
        <article><span>总吞吐量</span><strong>{number(metrics?.throughput_tokens_per_second ?? null)}</strong><small>Tokens / 秒</small></article>
      </div>

      <div className="diagnostics-details" aria-label="请求诊断摘要">
        <span>Usage <strong>{metrics?.usage_available ? "完整" : "不可用"}</strong></span>
        <span>请求延迟 <strong>{number(metrics?.latest_latency_ms ?? null, " ms")}</strong></span>
        <span>HTTP 错误 <strong>{number(metrics?.http_errors ?? 0)}</strong></span>
        <span>重试 <strong>{number(metrics?.retry_count ?? 0)}</strong></span>
        <span>限流等待 <strong>{waitingRequests(metrics?.rate_limit_waiting_requests)}</strong></span>
      </div>

      <div className="diagnostics-grid">
        <section className="diagnostics-panel log-panel">
          <div className="diagnostics-panel-heading">
            <div><h2>全局日志</h2><span>{value?.logs.length ?? 0} 条</span></div>
            <button
              className="quiet-button"
              aria-pressed={!autoScroll}
              onClick={() => setAutoScroll((current) => !current)}
            >
              {autoScroll ? "暂停自动滚动" : "恢复自动滚动"}
            </button>
          </div>
          <div className="diagnostics-filters">
            <select aria-label="日志级别" value={level} onChange={(event) => setLevel(event.target.value)}>
              <option value="">全部级别</option>
              {value?.filters.levels.map((item) => <option key={item}>{item}</option>)}
            </select>
            <select aria-label="日志项目" value={project} onChange={(event) => setProject(event.target.value)}>
              <option value="">全部项目</option>
              {value?.filters.projects.map((item) => <option key={item}>{item}</option>)}
            </select>
            <select aria-label="日志阶段" value={stage} onChange={(event) => setStage(event.target.value)}>
              <option value="">全部阶段</option>
              {value?.filters.stages.map((item) => <option key={item}>{item}</option>)}
            </select>
            <input aria-label="搜索日志" placeholder="搜索消息" value={query} onChange={(event) => setQuery(event.target.value)} />
          </div>
          <div className="diagnostics-log" ref={logRef} role="log" aria-live="off">
            {value?.logs.length ? value.logs.map((item, index) => (
              <div className="diagnostics-log-row" key={`${item.timestamp}-${index}`}>
                <time>{clock(item.timestamp)}</time>
                <b className={`log-${item.level.toLowerCase()}`}>{item.level}</b>
                <span>{item.project} · {item.stage}</span>
                <code>{item.message}</code>
              </div>
            )) : <div className="diagnostics-empty">没有符合条件的日志。</div>}
          </div>
        </section>

        <section className="diagnostics-panel request-panel">
          <div className="diagnostics-panel-heading">
            <div><h2>本次运行请求/响应</h2><span>最近 50 条 · 仅保存在内存</span></div>
          </div>
          <div className="request-list">
            {value?.requests.length ? [...value.requests].reverse().map((item) => (
              <article
                key={item.request_id}
                onDoubleClick={() => openDetail(item.request_id)}
              >
                <div className="request-row-main">
                  <header><code>{item.request_id}</code><time>{clock(item.timestamp)}</time></header>
                  <strong>{item.model}</strong>
                  <span>
                    <i className={`request-status status-${item.status}`}>{statusLabels[item.status]}</i>
                    {item.attempt_count} 次尝试
                    {item.last_http_status ? ` · HTTP ${item.last_http_status}` : ""}
                    {item.latest_latency_ms !== null ? ` · ${item.latest_latency_ms} ms` : ""}
                  </span>
                </div>
                <button className="quiet-button" onClick={() => openDetail(item.request_id)}>查看</button>
              </article>
            )) : <div className="diagnostics-empty">本次运行尚无请求。</div>}
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
                <h2 id="exchange-dialog-title">请求 / 响应详情</h2>
                <code>{selectedRequest}</code>
              </div>
              <button className="quiet-button" onClick={closeDetail} aria-label="关闭详情">关闭</button>
            </header>
            <nav className="exchange-tabs" aria-label="详情分类">
              {([
                ["request", "请求"],
                ["content", "Content"],
                ["reasoning", "Reasoning"],
                ["attempts", "尝试"],
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
              <div className="diagnostics-empty">正在读取请求详情…</div>
            ) : (
              <div className="exchange-detail">
                <div className="exchange-meta">
                  <span>模型 <strong>{detail.model}</strong></span>
                  <span>状态 <strong>{statusLabels[detail.status]}</strong></span>
                </div>
                {detailTab === "request" && (
                  <div className="exchange-request-detail">
                    {Object.keys(detail.segment_id_map).length > 0 && (
                      <div className="exchange-id-map">
                        <strong>请求内短 ID</strong>
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
                            {message.truncated && <span>已截断至 100,000 字符</span>}
                          </header>
                          <pre>{message.content}</pre>
                        </article>
                      ))}
                    </div>
                  </div>
                )}
                {detailTab === "content" && (
                  <article className="exchange-body">
                    {detail.response_content_truncated && <p>已截断至 100,000 字符。</p>}
                    <pre>{detail.response_content ?? "尚无 Content。"}</pre>
                  </article>
                )}
                {detailTab === "reasoning" && (
                  <article className="exchange-body">
                    {detail.reasoning_content_truncated && <p>已截断至 20,000 字符。</p>}
                    <pre>{detail.reasoning_content ?? "本请求没有 Reasoning。"}</pre>
                  </article>
                )}
                {detailTab === "attempts" && (
                  <div className="exchange-attempts">
                    {detail.attempts.length ? detail.attempts.map((attempt) => (
                      <article key={attempt.attempt}>
                        <strong>第 {attempt.attempt} 次</strong>
                        <span>{attempt.http_status === null ? "网络错误" : `HTTP ${attempt.http_status}`}</span>
                        <span>{attempt.latency_ms} ms</span>
                      </article>
                    )) : <div className="diagnostics-empty">尚未开始 HTTP 尝试。</div>}
                    {detail.error && <p className="error-text">错误类别：{detail.error}</p>}
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
