import { useCallback, useEffect, useRef, useState } from "react";
import { api } from "../api";
import type { DiagnosticsResponse } from "../types";

function number(value: number | null, suffix = "") {
  return value === null ? "不可用" : `${value.toLocaleString()}${suffix}`;
}

function clock(value: string) {
  return new Date(value).toLocaleTimeString([], {
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  });
}

export function DiagnosticsView() {
  const [value, setValue] = useState<DiagnosticsResponse | null>(null);
  const [level, setLevel] = useState("");
  const [project, setProject] = useState("");
  const [stage, setStage] = useState("");
  const [query, setQuery] = useState("");
  const [autoScroll, setAutoScroll] = useState(true);
  const [error, setError] = useState("");
  const logRef = useRef<HTMLDivElement>(null);

  const load = useCallback(async () => {
    const params = new URLSearchParams();
    if (level) params.set("level", level);
    if (project) params.set("project", project);
    if (stage) params.set("stage", stage);
    if (query) params.set("q", query);
    try {
      setValue(await api<DiagnosticsResponse>(
        `/api/v1/diagnostics${params.size ? `?${params}` : ""}`,
      ));
      setError("");
    } catch (reason) {
      setError(String(reason));
    }
  }, [level, project, stage, query]);

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
        <span>限流等待 <strong>{number(metrics?.rate_limit_wait_count ?? 0)}</strong></span>
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

        <section className="diagnostics-panel reasoning-panel">
          <div className="diagnostics-panel-heading">
            <div><h2>本次运行 Reasoning</h2><span>仅保存在内存</span></div>
          </div>
          <div className="reasoning-list">
            {value?.reasoning.length ? value.reasoning.map((item, index) => (
              <article key={`${item.request_id}-${index}`}>
                <header><code>{item.request_id}</code><time>{clock(item.timestamp)}</time></header>
                <pre>{item.content}</pre>
              </article>
            )) : <div className="diagnostics-empty">本次运行尚无 Reasoning 内容。</div>}
          </div>
        </section>
      </div>
    </section>
  );
}
