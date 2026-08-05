import { useCallback, useEffect, useRef, useState } from "react";
import { useVirtualizer } from "@tanstack/react-virtual";
import { api } from "../api";
import { translate, type Language } from "../i18n";
import type { ProjectOverview, Segment, SegmentDetail, Stage } from "../types";
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

export function SegmentWorkspace({
  project,
  stage,
  overview,
  onRefresh,
  focusFailures,
  language,
}: {
  project: string;
  stage: "translation" | "proofreading" | "polishing";
  overview: ProjectOverview;
  onRefresh: () => Promise<void>;
  focusFailures?: boolean;
  language: Language;
}) {
  const statusLabels: Record<string, string> = Object.fromEntries(
    ["all", "pending", "completed", "warning", "missing-base", "outdated", "accepted", "suggested", "applied", "error"]
      .map((key) => [key, translate(`status.${key}`, language)]),
  );
  const selection = useClassicSelection();
  const [file, setFile] = useState("all");
  const [status, setStatus] = useState("all");
  const [search, setSearch] = useState("");
  const [orderedIds, setOrderedIds] = useState<string[]>([]);
  const [records, setRecords] = useState<Record<string, Segment>>({});
  const [focusedDetail, setFocusedDetail] = useState<SegmentDetail | null>(null);
  const [total, setTotal] = useState(0);
  const [listError, setListError] = useState("");
  const [loading, setLoading] = useState(false);
  const listRef = useRef<HTMLDivElement>(null);
  const indexRequestRef = useRef(0);
  const pageSize = 100;
  const pageCacheRef = useRef(new Set<string>());
  const pageRequestsRef = useRef(new Set<string>());
  const pageGenerationRef = useRef(0);
  const activePageQueryRef = useRef("");
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

  const normalizedSearch = search.trim();
  const query = new URLSearchParams({ stage });
  if (file !== "all") query.set("file_id", file);
  if (status !== "all") query.set("status", status === "error" ? "failed" : status);
  if (normalizedSearch) query.set("q", normalizedSearch);
  const pageQueryKey = JSON.stringify([project, stage, file, status, normalizedSearch]);
  const showContext = status !== "all" || normalizedSearch !== "";
  const resetPageCache = useCallback(() => {
    pageGenerationRef.current += 1;
    pageCacheRef.current.clear();
    pageRequestsRef.current.clear();
  }, []);

  useEffect(() => {
    if (activePageQueryRef.current === pageQueryKey) return;
    activePageQueryRef.current = pageQueryKey;
    resetPageCache();
  }, [pageQueryKey, resetPageCache]);

  const reloadIndex = useCallback(async (preserveSegmentId?: string) => {
    const requestId = ++indexRequestRef.current;
    setLoading(true);
    setListError("");
    try {
      const index = await api<{ segment_ids: string[]; total: number }>(
        `/api/v1/projects/${project}/segments/ids?${query.toString()}`,
      );
      if (requestId !== indexRequestRef.current) return [];
      setOrderedIds(index.segment_ids);
      setTotal(index.total);
      if (!preserveSegmentId) {
        resetPageCache();
        setRecords({});
        setFocusedDetail(null);
        selection.reset(index.segment_ids[0] ?? "");
        listRef.current?.scrollTo({ top: 0 });
      } else if (!index.segment_ids.includes(preserveSegmentId)) {
        // A save can move the focused row out of a status filter. In that
        // case reset only the now-invalid focus; an unchanged row keeps its
        // window, selection, and scroll position.
        resetPageCache();
        setRecords({});
        setFocusedDetail(null);
        selection.reset(index.segment_ids[0] ?? "");
        listRef.current?.scrollTo({ top: 0 });
      } else {
        const allowed = new Set(index.segment_ids);
        setRecords((current) => Object.fromEntries(
          Object.entries(current).filter(([segmentId]) => allowed.has(segmentId)),
        ));
      }
      return index.segment_ids;
    } catch (value) {
      if (requestId !== indexRequestRef.current) return [];
      resetPageCache();
      setListError(String(value));
      setOrderedIds([]);
      setTotal(0);
      setFocusedDetail(null);
      selection.reset();
      return [];
    } finally {
      if (requestId === indexRequestRef.current) setLoading(false);
    }
  }, [project, stage, file, status, normalizedSearch, resetPageCache]);

  useEffect(() => { void reloadIndex(); }, [reloadIndex]);

  const virtualizer = useVirtualizer({
    count: orderedIds.length,
    getScrollElement: () => listRef.current,
    getItemKey: (index) => orderedIds[index] ?? index,
    estimateSize: () => 82,
    overscan: 8,
  });
  const virtualItems = virtualizer.getVirtualItems();

  useEffect(() => {
    const offsets = new Set(
      virtualItems.map((item) => Math.floor(item.index / pageSize) * pageSize),
    );
    if (!offsets.size) return;
    const requestQuery = pageQueryKey;
    const requestGeneration = pageGenerationRef.current;
    for (const offset of offsets) {
      const pageKey = JSON.stringify([requestQuery, offset]);
      const requestToken = JSON.stringify([requestGeneration, pageKey]);
      if (
        pageCacheRef.current.has(pageKey)
        || pageRequestsRef.current.has(requestToken)
      ) continue;
      pageRequestsRef.current.add(requestToken);
      const params = new URLSearchParams({ stage, offset: String(offset), limit: String(pageSize) });
      if (file !== "all") params.set("file_id", file);
      if (status !== "all") params.set("status", status === "error" ? "failed" : status);
      if (normalizedSearch) params.set("q", normalizedSearch);
      void api<ProjectOverview>(`/api/v1/projects/${project}?${params.toString()}`)
        .then((page) => {
          if (
            requestGeneration !== pageGenerationRef.current
            || requestQuery !== activePageQueryRef.current
          ) return;
          pageCacheRef.current.add(pageKey);
          setRecords((current) => {
            const next = { ...current };
            for (const item of page.segments) next[item.segment_id] = item;
            return next;
          });
        })
        .catch((value) => {
          if (
            requestGeneration === pageGenerationRef.current
            && requestQuery === activePageQueryRef.current
          ) setListError(String(value));
        })
        .finally(() => {
          pageRequestsRef.current.delete(requestToken);
        });
    }
  }, [
    project,
    stage,
    file,
    status,
    normalizedSearch,
    pageQueryKey,
    orderedIds,
    virtualItems.map((item) => item.index).join(","),
  ]);

  const focusedId = selection.focusedKey || orderedIds[0] || "";

  useEffect(() => {
    if (!focusedId) {
      setFocusedDetail(null);
      return;
    }
    if (!showContext && records[focusedId]) {
      setFocusedDetail(null);
      return;
    }
    let active = true;
    setFocusedDetail(null);
    void api<SegmentDetail>(`/api/v1/projects/${project}/segments/${focusedId}`)
      .then((item) => {
        if (!active) return;
        setRecords((current) => ({ ...current, [item.segment_id]: item }));
        setFocusedDetail(item);
      })
      .catch((value) => { if (active) setListError(String(value)); });
    return () => { active = false; };
  }, [project, focusedId, showContext, file, status, normalizedSearch]);

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

  const context = focusedDetail && selected
    && focusedDetail.segment_id === selected.segment_id
    ? focusedDetail.context
    : null;
  const before = context?.before ?? [];
  const after = context?.after ?? [];

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
    const visibleIds = await reloadIndex(selected.segment_id);
    if (visibleIds.includes(selected.segment_id)) {
      try {
        const fresh = await api<SegmentDetail>(
          `/api/v1/projects/${project}/segments/${selected.segment_id}`,
        );
        setRecords((current) => ({ ...current, [fresh.segment_id]: fresh }));
        setFocusedDetail(fresh);
      } catch (value) {
        setListError(String(value));
      }
    }
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
      setBatchMessage(translate("workspace.clearedResults", language, { count: result.cleared }));
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
      setBatchMessage(translate("workspace.appliedSegments", language, { count: result.completed }));
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
            <option value="all">{translate("workspace.allFiles", language)}</option>
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
            }} placeholder={translate("workspace.searchSourceResult", language)} />
          </div>
          <div className="batch-toolbar segment-batch-toolbar">
            <span>{translate("workspace.selectedCount", language, { selected: selectedVisibleIds.length, total })}</span>
            <div className="segment-batch-actions">
              {stage !== "translation" && (
                <>
                  <button className="quiet-button" disabled={!selectedVisibleIds.length} onClick={() => setBatchAction({ kind: "apply", scope: "selected" })}>{translate("workspace.applySelected", language)}</button>
                  <button className="quiet-button" disabled={!total} onClick={() => setBatchAction({ kind: "apply", scope: "filtered" })}>{translate("workspace.applyAll", language)}</button>
                </>
              )}
              <button className="danger-button" disabled={!selectedVisibleIds.length} onClick={() => setBatchAction({ kind: "reset", scope: "selected" })}>{translate("workspace.clearSelected", language)}</button>
              <button className="danger-button" disabled={!total} onClick={() => setBatchAction({ kind: "reset", scope: "filtered" })}>{translate("workspace.clearAll", language)}</button>
            </div>
          </div>
          {batchMessage && <span className="success-text">{batchMessage}</span>}
        </div>
        <div className="list-header"><span>{translate("workspace.idStatus", language)}</span><span>{translate("workspace.sourceResultPreview", language)}</span></div>
        <div className="segment-list" ref={listRef}>
          <div className="segment-row-stack" style={{ height: virtualizer.getTotalSize(), position: "relative" }}>
            {virtualItems.map((virtualItem) => {
              const item = records[orderedIds[virtualItem.index]];
              const rowPosition = {
                position: "absolute" as const,
                top: 0,
                left: 0,
                width: "100%",
                transform: `translateY(${virtualItem.start}px)`,
              };
              if (!item) return <div key={virtualItem.key} className="segment-row-placeholder" style={{ ...rowPosition, height: virtualItem.size }} />;
              const itemStatus = statusFor(item, stage);
              const result = resultFor(item, stage);
              const error = item.stage_errors?.[stage];
              const preview = error
                ? `${statusLabels.error}: ${error.error_class} · ${error.error_message}`
                : stage === "translation" ? result?.text : result?.suggested_text ?? item.reviews[stage].base?.text;
              return (
                <button
                  key={virtualItem.key}
                  ref={virtualizer.measureElement}
                  data-index={virtualItem.index}
                  style={rowPosition}
                  className={`segment-row${selection.selectedKeys.has(item.segment_id) ? " selected" : ""}${selection.focusedKey === item.segment_id ? " focused" : ""}`}
                  onClick={(event) => selection.select(
                    item.segment_id,
                    visibleKeys,
                    event,
                  )}
                >
                  <span className={`status-dot ${itemStatus}`} />
                  <span className="segment-id">{item.segment_id.replace("F0001-S", "")}</span>
                  <span className="preview"><strong>{item.source}</strong><small>{item.format_count ? translate("workspace.formatRanges", language, { count: item.format_count }) : ""}{preview || translate("workspace.noResultYet", language)}</small></span>
                </button>
              );
            })}
          </div>
          {!total && <div className="empty">{translate("workspace.noSegments", language)}</div>}
          {loading && <div className="list-loading">{translate("workspace.loadingSegments", language)}</div>}
          {listError && <div className="error-text">{listError}</div>}
        </div>
      </section>
      <section className="editor-pane">
        {!selected ? <div className="empty">{translate("workspace.noEditable", language)}</div> : (
          <>
            <h2>{translate("workspace.source", language)}</h2>
            <div className="source-box">{selected.source}</div>
            {selected.model_source && selected.model_source !== selected.source && (
              <details className="source-model-preview"><summary>{translate("workspace.modelText", language)}</summary><div className="source-box">{selected.model_source}</div></details>
            )}
            {review?.outdated && <div className="warning-banner">{translate("workspace.baseChanged", language)}</div>}
            <div className={stage === "translation" ? "comparison single" : "comparison"}>
              {stage !== "translation" && (
                <label><span>{translate("workspace.currentBase", language)}</span><textarea readOnly value={review?.base?.text ?? ""} /></label>
              )}
              <label>
                <span>{stage === "translation" ? translate("workspace.currentTranslation", language) : translate("workspace.suggestedResult", language)}</span>
                <textarea
                  value={text}
                  disabled={reviewStatus === "accepted"}
                  onChange={(event) => setText(event.target.value)}
                  placeholder={review?.base ? translate("workspace.enterSuggestion", language) : translate("workspace.noUsableBase", language)}
                />
              </label>
            </div>
            {stage !== "translation" && (
              <div className="review-controls">
                <label className="radio-option"><input type="radio" checked={reviewStatus === "accepted"} onChange={() => setReviewStatus("accepted")} />{translate("workspace.acceptCurrentBase", language)}</label>
                <label className="radio-option"><input type="radio" checked={reviewStatus === "suggested"} onChange={() => setReviewStatus("suggested")} />{translate("workspace.useSuggestedEdit", language)}</label>
                <input value={reason} onChange={(event) => setReason(event.target.value)} placeholder={translate("workspace.reasonOptional", language)} />
              </div>
            )}
            {showContext && (
              <>
                <div className="context-heading"><h2>{translate("workspace.context", language)}</h2></div>
                <div className="context-groups">
                  <ContextGroup title={translate("workspace.before", language)} items={before} empty={translate("workspace.noMoreBefore", language)} stage={stage} />
                  <ContextGroup title={translate("workspace.after", language)} items={after} empty={translate("workspace.noMoreAfter", language)} stage={stage} />
                </div>
              </>
            )}
            <div className="editor-actions">
              <button className="primary-button" disabled={!!review && !review.base} onClick={() => save(false)}>{translate("common.save", language)}</button>
              {stage !== "translation" && <button className="quiet-button" disabled={!review?.base} onClick={() => save(true)}>{translate("workspace.saveAndApply", language)}</button>}
            </div>
          </>
        )}
      </section>
      {batchAction && (
      <BatchActionDialog
          kind={batchAction.kind}
          stage={stage}
          language={language}
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
  language,
  count,
  missing,
  outdated,
  onClose,
  onConfirm,
}: {
  kind: "reset" | "apply";
  stage: "translation" | "proofreading" | "polishing";
  language: Language;
  count: number;
  missing: number;
  outdated: number;
  onClose: () => void;
  onConfirm: (allowOutdated?: boolean) => Promise<void>;
}) {
  const [allowOutdated, setAllowOutdated] = useState(false);
  const [working, setWorking] = useState(false);
  const [error, setError] = useState("");
  const reviewLabel = translate(stage === "proofreading" ? "stage.proofreading" : "stage.polishing", language);
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
      <div className="modal" role="dialog" aria-modal="true" aria-label={kind === "apply" ? translate("workspace.batchApply", language) : translate("workspace.batchClear", language)}>
        <h2>{kind === "apply" ? translate("workspace.batchApplyTitle", language, { stage: reviewLabel }) : translate("workspace.batchClearTitle", language)}</h2>
        <p>{translate("workspace.batchScopeCount", language, { count })}</p>
        {kind === "reset" ? (
          <p>
            {stage === "translation"
              ? translate("workspace.batchResetTranslation", language)
              : translate("workspace.batchResetReview", language, { stage: reviewLabel })}
          </p>
        ) : (
          <>
            {!!missing && <div className="warning-banner">{translate("workspace.batchMissing", language, { count: missing })}</div>}
            {!!outdated && (
              <label className="check-row">
                <input type="checkbox" checked={allowOutdated} onChange={(event) => setAllowOutdated(event.target.checked)} />
                {translate("workspace.batchAllowOutdated", language, { count: outdated })}
              </label>
            )}
          </>
        )}
        {error && <p className="error-text">{error}</p>}
        <div className="modal-actions">
          <button className="quiet-button" disabled={working} onClick={onClose}>{translate("common.cancel", language)}</button>
          <button className={kind === "reset" ? "danger-button" : "primary-button"} disabled={working || blocked || !count} onClick={confirm}>
            {kind === "reset" ? translate("workspace.batchConfirmClear", language) : translate("workspace.batchConfirmApply", language)}
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
