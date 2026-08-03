import { useCallback, useEffect, useRef, useState } from "react";
import { useVirtualizer } from "@tanstack/react-virtual";
import { api } from "../api";
import type { ProjectOverview, Segment, Stage } from "../types";
import { useClassicSelection } from "../useClassicSelection";

function resultFor(segment: Segment, stage: Stage) {
  if (stage === "translation") return segment.translation;
  if (stage === "proofreading" || stage === "polishing") {
    return segment.reviews[stage].suggestion;
  }
  return null;
}

function statusFor(segment: Segment, stage: Stage) {
  if (
    (stage === "translation" || stage === "proofreading" || stage === "polishing")
    && segment.stage_errors?.[stage]
  ) return "error";
  if (stage === "translation") {
    if (!segment.translation) return "pending";
    return segment.translation.validation_status === "warning" ? "warning" : "completed";
  }
  if (stage === "proofreading" || stage === "polishing") {
    const review = segment.reviews[stage];
    if (!review.base) return "missing-base";
    if (review.outdated) return "outdated";
    if (!review.suggestion) return "pending";
    if (review.suggestion.review_status === "accepted") return "accepted";
    return review.applied_current ? "applied" : "suggested";
  }
  return "pending";
}

const statusLabels: Record<string, string> = {
  all: "全部状态",
  pending: "待处理",
  completed: "已完成",
  warning: "校验警告",
  "missing-base": "缺少基准",
  outdated: "基准已变",
  accepted: "接受基准",
  suggested: "建议修改",
  applied: "已应用",
  error: "请求失败",
};

export function SegmentWorkspace({
  project,
  stage,
  overview,
  onRefresh,
  focusFailures,
}: {
  project: string;
  stage: "translation" | "proofreading" | "polishing";
  overview: ProjectOverview;
  onRefresh: () => Promise<void>;
  focusFailures?: boolean;
}) {
  const selection = useClassicSelection();
  const [file, setFile] = useState("all");
  const [status, setStatus] = useState("all");
  const [search, setSearch] = useState("");
  const [orderedIds, setOrderedIds] = useState<string[]>([]);
  const [records, setRecords] = useState<Record<string, Segment>>({});
  const [total, setTotal] = useState(0);
  const [listError, setListError] = useState("");
  const [loading, setLoading] = useState(false);
  const listRef = useRef<HTMLDivElement>(null);
  const pageSize = 100;
  const [batchAction, setBatchAction] = useState<{
    kind: "reset" | "apply";
    scope: "selected" | "filtered";
  } | null>(null);
  const [batchMessage, setBatchMessage] = useState("");
  const selected = records[selection.focusedKey] ?? records[orderedIds[0]];
  const [text, setText] = useState("");
  const [reason, setReason] = useState("");
  const [reviewStatus, setReviewStatus] = useState<"accepted" | "suggested">("suggested");

  useEffect(() => {
    if (focusFailures) setStatus("error");
  }, [focusFailures]);

  const query = new URLSearchParams({ stage });
  if (file !== "all") query.set("file_id", file);
  if (status !== "all") query.set("status", status === "error" ? "failed" : status === "warning" ? "completed" : status);
  if (search.trim()) query.set("q", search.trim());

  const reloadIndex = useCallback(async () => {
    setLoading(true);
    setListError("");
    try {
      const index = await api<{ segment_ids: string[]; total: number }>(
        `/api/v1/projects/${project}/segments/ids?${query.toString()}`,
      );
      setOrderedIds(index.segment_ids);
      setTotal(index.total);
      setRecords({});
      selection.reset(index.segment_ids[0] ?? "");
      listRef.current?.scrollTo({ top: 0 });
    } catch (value) {
      setListError(String(value));
      setOrderedIds([]);
      setTotal(0);
      selection.reset();
    } finally {
      setLoading(false);
    }
  }, [project, stage, file, status, search]);

  useEffect(() => { void reloadIndex(); }, [reloadIndex]);

  const virtualizer = useVirtualizer({
    count: orderedIds.length,
    getScrollElement: () => listRef.current,
    estimateSize: () => 82,
    overscan: 8,
  });
  const virtualItems = virtualizer.getVirtualItems();

  useEffect(() => {
    const offsets = new Set(
      virtualItems.map((item) => Math.floor(item.index / pageSize) * pageSize),
    );
    if (!offsets.size) return;
    let cancelled = false;
    void Promise.all([...offsets].map(async (offset) => {
      const params = new URLSearchParams({ stage, offset: String(offset), limit: String(pageSize) });
      if (file !== "all") params.set("file_id", file);
      if (status !== "all" && status !== "warning") params.set("status", status === "error" ? "failed" : status);
      if (search.trim()) params.set("q", search.trim());
      const page = await api<ProjectOverview>(`/api/v1/projects/${project}?${params.toString()}`);
      return page.segments;
    })).then((pages) => {
      if (cancelled) return;
      setRecords((current) => {
        const next = { ...current };
        for (const page of pages) for (const item of page) next[item.segment_id] = item;
        return next;
      });
    }).catch((value) => { if (!cancelled) setListError(String(value)); });
    return () => { cancelled = true; };
  }, [project, stage, file, status, search, orderedIds, virtualItems.map((item) => item.index).join(",")]);

  useEffect(() => {
    if (!selection.focusedKey || records[selection.focusedKey]) return;
    void api<Segment>(`/api/v1/projects/${project}/segments/${selection.focusedKey}`)
      .then((item) => setRecords((current) => ({ ...current, [item.segment_id]: item })))
      .catch((value) => setListError(String(value)));
  }, [project, selection.focusedKey, records]);

  useEffect(() => {
    if (!selected) return;
    if (stage === "translation") {
      setText(selected.translation?.text ?? "");
      return;
    }
    const suggestion = selected.reviews[stage].suggestion;
    setReviewStatus(suggestion?.review_status ?? "suggested");
    setText(suggestion?.suggested_text ?? selected.reviews[stage].base?.text ?? "");
    setReason(suggestion?.reason ?? "");
  }, [selection.focusedKey, stage, selected]);

  const visibleKeys = orderedIds;
  const selectedVisibleIds = visibleKeys.filter((segmentId) => (
    selection.selectedKeys.has(segmentId)
  ));

  const neighbors = selected
    ? orderedIds
      .map((id) => records[id])
      .filter((item): item is Segment => Boolean(item))
      .filter((item) => item.file_id === selected.file_id && item.part_id === selected.part_id)
    : [];
  const selectedIndex = neighbors.findIndex((item) => item.segment_id === selected?.segment_id);
  const before = neighbors.slice(Math.max(0, selectedIndex - 2), selectedIndex);
  const after = neighbors.slice(selectedIndex + 1, selectedIndex + 3);
  const showContext = status !== "all" || search.trim() !== "";

  function resetFilterSelection() {
    selection.reset();
    setBatchMessage("");
  }

  async function save(apply = false) {
    if (!selected) return;
    if (stage === "translation") {
      await api(`/api/v1/projects/${project}/translations`, {
        method: "POST",
        body: JSON.stringify({ segment_id: selected.segment_id, text }),
      });
    } else {
      await api(`/api/v1/projects/${project}/reviews`, {
        method: "POST",
        body: JSON.stringify({
          segment_id: selected.segment_id,
          stage,
          review_status: reviewStatus,
          suggested_text: reviewStatus === "suggested" ? text : null,
          reason: reason || null,
          apply,
        }),
      });
    }
    await onRefresh();
    await reloadIndex();
  }

  const batchIds = batchAction?.scope === "filtered"
    ? visibleKeys
    : selectedVisibleIds;
  const batchSegments = batchIds
    .map((segmentId) => records[segmentId])
    .filter((item): item is Segment => Boolean(item));
  const missingApply = batchAction?.kind === "apply" && stage !== "translation"
    ? batchSegments.filter((item) => {
      const review = item.reviews[stage];
      return !review.base || !review.suggestion;
    })
    : [];
  const outdatedApply = batchAction?.kind === "apply" && stage !== "translation"
    ? batchSegments.filter((item) => item.reviews[stage].outdated)
    : [];

  async function runBatch(allowOutdated = false) {
    if (!batchAction || !batchIds.length) return;
    if (batchAction.kind === "reset") {
      const result = await api<{ cleared: number }>(
        `/api/v1/projects/${project}/results/reset`,
        {
          method: "POST",
          body: JSON.stringify({ stage, segment_ids: batchIds }),
        },
      );
      setBatchMessage(`已清除 ${result.cleared} 个 Segment 的当前结果`);
    } else {
      const result = await api<{ completed: number }>(
        `/api/v1/projects/${project}/apply`,
        {
          method: "POST",
          body: JSON.stringify({
            stage,
            segment_ids: batchIds,
            all: true,
            allow_outdated_base: allowOutdated,
          }),
        },
      );
      setBatchMessage(`已应用 ${result.completed} 个 Segment`);
    }
    setBatchAction(null);
    selection.reset();
    await onRefresh();
    await reloadIndex();
  }

  const review = stage === "translation" || !selected ? null : selected.reviews[stage];
  return (
    <div className="workspace">
      <section className="segment-browser">
        <div className="filters">
          <select value={file} onChange={(event) => {
            setFile(event.target.value);
            resetFilterSelection();
          }}>
            <option value="all">全部文件</option>
            {overview.files.map((item) => <option value={item.file_id} key={item.file_id}>{item.name}</option>)}
          </select>
          <div className="filter-row">
            <select value={status} onChange={(event) => {
              setStatus(event.target.value);
              resetFilterSelection();
            }}>
              {Object.entries(statusLabels).map(([value, label]) => <option key={value} value={value}>{label}</option>)}
            </select>
            <input value={search} onChange={(event) => {
              setSearch(event.target.value);
              resetFilterSelection();
            }} placeholder="搜索原文或译文" />
          </div>
          <div className="batch-toolbar segment-batch-toolbar">
            <span>已选择 {selectedVisibleIds.length} / 当前 {total}</span>
            <div className="segment-batch-actions">
              {stage !== "translation" && (
                <>
                  <button className="quiet-button" disabled={!selectedVisibleIds.length} onClick={() => setBatchAction({ kind: "apply", scope: "selected" })}>应用所选</button>
                  <button className="quiet-button" disabled={!total} onClick={() => setBatchAction({ kind: "apply", scope: "filtered" })}>全部应用</button>
                </>
              )}
              <button className="danger-button" disabled={!selectedVisibleIds.length} onClick={() => setBatchAction({ kind: "reset", scope: "selected" })}>清除所选</button>
              <button className="danger-button" disabled={!total} onClick={() => setBatchAction({ kind: "reset", scope: "filtered" })}>全部清除</button>
            </div>
          </div>
          {batchMessage && <span className="success-text">{batchMessage}</span>}
        </div>
        <div className="list-header"><span>ID / 状态</span><span>原文 / 结果预览</span></div>
        <div className="segment-list" ref={listRef}>
          <div className="segment-row-stack" style={{ height: virtualizer.getTotalSize(), position: "relative" }}>
            {virtualItems.map((virtualItem) => {
              const item = records[orderedIds[virtualItem.index]];
              if (!item) return <div key={virtualItem.key} className="segment-row-placeholder" style={{ height: virtualItem.size, transform: `translateY(${virtualItem.start}px)` }} />;
              const itemStatus = statusFor(item, stage);
              const result = resultFor(item, stage);
              const error = item.stage_errors?.[stage];
              const preview = error
                ? `${statusLabels.error}：${error.error_class} · ${error.error_message}`
                : stage === "translation" ? result?.text : result?.suggested_text ?? item.reviews[stage].base?.text;
              return (
                <button
                  key={item.segment_id}
                  ref={virtualizer.measureElement}
                  data-index={virtualItem.index}
                  style={{ position: "absolute", top: 0, transform: `translateY(${virtualItem.start}px)`, height: Math.max(virtualItem.size - 1, 1) }}
                  className={`segment-row${selection.selectedKeys.has(item.segment_id) ? " selected" : ""}${selection.focusedKey === item.segment_id ? " focused" : ""}`}
                  onClick={(event) => selection.select(
                    item.segment_id,
                    visibleKeys,
                    event,
                  )}
                >
                  <span className={`status-dot ${itemStatus}`} />
                  <span className="segment-id">{item.segment_id.replace("F0001-S", "")}</span>
                  <span className="preview"><strong>{item.source}</strong><small>{item.format_count ? `${item.format_count} 个格式范围 · ` : ""}{preview || "尚无结果"}</small></span>
                </button>
              );
            })}
          </div>
          {!total && <div className="empty">当前筛选下没有 Segment</div>}
          {loading && <div className="list-loading">正在加载 Segment…</div>}
          {listError && <div className="error-text">{listError}</div>}
        </div>
      </section>
      <section className="editor-pane">
        {!selected ? <div className="empty">项目没有可编辑 Segment</div> : (
          <>
            <h2>原文</h2>
            <div className="source-box">{selected.source}</div>
            {selected.model_source && selected.model_source !== selected.source && (
              <details className="source-model-preview"><summary>模型文本（受控格式标记）</summary><div className="source-box">{selected.model_source}</div></details>
            )}
            {review?.outdated && <div className="warning-banner">建议所依据的基准已经变化，请重新检查后保存。</div>}
            <div className={stage === "translation" ? "comparison single" : "comparison"}>
              {stage !== "translation" && (
                <label><span>当前基准</span><textarea readOnly value={review?.base?.text ?? ""} /></label>
              )}
              <label>
                <span>{stage === "translation" ? "当前译文" : "建议结果"}</span>
                <textarea
                  value={text}
                  disabled={reviewStatus === "accepted"}
                  onChange={(event) => setText(event.target.value)}
                  placeholder={review?.base ? "输入完整建议文本" : "缺少可用基准"}
                />
              </label>
            </div>
            {stage !== "translation" && (
              <div className="review-controls">
                <label className="radio-option"><input type="radio" checked={reviewStatus === "accepted"} onChange={() => setReviewStatus("accepted")} />接受当前基准</label>
                <label className="radio-option"><input type="radio" checked={reviewStatus === "suggested"} onChange={() => setReviewStatus("suggested")} />使用建议修改</label>
                <input value={reason} onChange={(event) => setReason(event.target.value)} placeholder="修改原因（可选）" />
              </div>
            )}
            {showContext && (
              <>
                <div className="context-heading"><h2>上下文</h2></div>
                <div className="context-groups">
                  <ContextGroup title="上文" items={before} empty="无更多上文" stage={stage} />
                  <ContextGroup title="下文" items={after} empty="无更多下文" stage={stage} />
                </div>
              </>
            )}
            <div className="editor-actions">
              <button className="primary-button" disabled={!!review && !review.base} onClick={() => save(false)}>保存</button>
              {stage !== "translation" && <button className="quiet-button" disabled={!review?.base} onClick={() => save(true)}>保存并应用</button>}
            </div>
          </>
        )}
      </section>
      {batchAction && (
        <BatchActionDialog
          kind={batchAction.kind}
          stage={stage}
          count={batchIds.length}
          missing={missingApply.length}
          outdated={outdatedApply.length}
          onClose={() => setBatchAction(null)}
          onConfirm={runBatch}
        />
      )}
    </div>
  );
}

function BatchActionDialog({
  kind,
  stage,
  count,
  missing,
  outdated,
  onClose,
  onConfirm,
}: {
  kind: "reset" | "apply";
  stage: "translation" | "proofreading" | "polishing";
  count: number;
  missing: number;
  outdated: number;
  onClose: () => void;
  onConfirm: (allowOutdated?: boolean) => Promise<void>;
}) {
  const [allowOutdated, setAllowOutdated] = useState(false);
  const [working, setWorking] = useState(false);
  const [error, setError] = useState("");
  const reviewLabel = stage === "proofreading" ? "校对" : "润色";
  const blocked = kind === "apply" && (
    missing > 0 || (outdated > 0 && !allowOutdated)
  );

  async function confirm() {
    setWorking(true);
    setError("");
    try {
      await onConfirm(allowOutdated);
    } catch (value) {
      setError(String(value));
    } finally {
      setWorking(false);
    }
  }

  return (
    <div className="modal-backdrop">
      <div className="modal" role="dialog" aria-modal="true" aria-label={kind === "apply" ? "批量应用" : "批量清除"}>
        <h2>{kind === "apply" ? `批量应用${reviewLabel}结果` : "批量清除结果"}</h2>
        <p>本次范围包含 {count} 个 Segment。</p>
        {kind === "reset" ? (
          <p>
            {stage === "translation"
              ? "当前译文将回到待处理；校对和润色历史不会级联删除。"
              : `${reviewLabel}建议及其已应用结果将一起撤销；其他阶段不会级联删除。`}
          </p>
        ) : (
          <>
            {!!missing && <div className="warning-banner">其中 {missing} 项缺少建议或基准，整批不能应用。请调整过滤范围。</div>}
            {!!outdated && (
              <label className="check-row">
                <input type="checkbox" checked={allowOutdated} onChange={(event) => setAllowOutdated(event.target.checked)} />
                允许应用 {outdated} 项基于旧上游的建议
              </label>
            )}
          </>
        )}
        {error && <p className="error-text">{error}</p>}
        <div className="modal-actions">
          <button className="quiet-button" disabled={working} onClick={onClose}>取消</button>
          <button className={kind === "reset" ? "danger-button" : "primary-button"} disabled={working || blocked || !count} onClick={confirm}>
            {kind === "reset" ? "确认清除" : "确认应用"}
          </button>
        </div>
      </div>
    </div>
  );
}

function ContextGroup({
  title,
  items,
  empty,
  stage,
}: {
  title: string;
  items: Segment[];
  empty: string;
  stage: "translation" | "proofreading" | "polishing";
}) {
  return (
    <div className="context-group">
      <h3>{title}</h3>
      {!items.length ? <p className="muted">{empty}</p> : items.map((item) => {
        const result = contextResult(item, stage);
        return (
          <div className="context-item" key={item.segment_id}>
            <small>{item.segment_id}</small>
            <p>{item.source}</p>
            {result && <p className="muted">{result}</p>}
          </div>
        );
      })}
    </div>
  );
}

function contextResult(
  segment: Segment,
  stage: "translation" | "proofreading" | "polishing",
) {
  if (stage === "translation") return segment.translation?.text;
  return segment.reviews[stage].base?.text;
}
