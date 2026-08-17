import { useCallback, useEffect, useLayoutEffect, useRef, useState } from "react";
import { useVirtualizer } from "@tanstack/react-virtual";
import { api } from "../api";
import { translate, type Language } from "../i18n";
import type { ProjectOverview, Segment, SegmentDetail } from "../types";
import { useClassicSelection } from "../useClassicSelection";
import { Modal } from "./Modal";

interface WorkspaceCacheEntry {
  orderedIds: string[];
  total: number;
  records: Record<string, Segment>;
  focusedId: string;
  scrollTop: number;
}

// Survives tab switches so returning to a stage renders instantly. Keyed by
// the full query (project + stage + filters); cleared when the project
// changes so cached data never leaks across projects.
const workspaceCache = new Map<string, WorkspaceCacheEntry>();
const workspaceProjectRef = { current: "" };
const pageSize = 100;

// Warms each stage's head window when a project is opened so the first visit
// to a stage renders instantly. Best-effort: failures are left to the
// workspace, which fetches and surfaces them on visit; the write guard keeps
// a mounted workspace's fresher entry intact. The cache key matches the
// workspace's initial default-filter query, and focusedId is the first
// segment so the mount-time index refresh preserves the restored window.
export function prefetchWorkspace(project: string) {
  for (const stage of ["translation", "proofreading", "polishing"] as const) {
    const key = JSON.stringify([project, stage, "all", "all", ""]);
    if (workspaceCache.has(key)) continue;
    void Promise.all([
      api<{ segment_ids: string[]; total: number }>(
        `/api/v1/projects/${project}/segments/ids`,
        { method: "POST", body: JSON.stringify({ stage }) },
      ),
      api<ProjectOverview>(
        `/api/v1/projects/${project}/segments/query`,
        { method: "POST", body: JSON.stringify({ stage, offset: 0, limit: pageSize }) },
      ),
    ])
      .then(([index, page]) => {
        if (workspaceCache.has(key)) return;
        const records: Record<string, Segment> = {};
        for (const item of page.segments) records[item.segment_id] = item;
        workspaceCache.set(key, {
          orderedIds: index.segment_ids,
          total: index.total,
          records,
          focusedId: index.segment_ids[0] ?? "",
          scrollTop: 0,
        });
      })
      .catch(() => {});
  }
}

function resultFor(segment: Segment, stage: "translation" | "proofreading" | "polishing") {
  if (stage === "translation") return segment.translation;
  return segment.reviews[stage].suggestion;
}

function statusFor(segment: Segment, stage: "translation" | "proofreading" | "polishing") {
  if (segment.stage_errors?.[stage]) return "error";
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
  pendingJump,
  onJumpConsumed,
}: {
  project: string;
  stage: "translation" | "proofreading" | "polishing";
  overview: ProjectOverview;
  onRefresh: () => Promise<void>;
  focusFailures?: boolean;
  language: Language;
  pendingJump?: { search: string; segmentId: string } | null;
  onJumpConsumed?: () => void;
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
  const [loading, setLoading] = useState(true);
  const listRef = useRef<HTMLDivElement>(null);
  const indexRequestRef = useRef(0);
  const indexInFlightRef = useRef(false);
  const preserveFocusRef = useRef("");
  const scrollToFocusRef = useRef(false);
  const restoredScrollTopRef = useRef<number | null>(null);
  const jumpConsumedRef = useRef(false);
  const jumpTargetRef = useRef("");
  const jumpSearchRef = useRef("");
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

  // A term-hit jump arrives with the workspace (fresh mount): apply the
  // prefilled search and keep the target segment focused once its window
  // loads. Cached windows from previous visits are dropped so the cache
  // restore cannot overwrite the jump focus. Consumed once per mount so
  // re-renders and later visits never replay the navigation.
  useEffect(() => {
    if (!pendingJump || jumpConsumedRef.current) return;
    jumpConsumedRef.current = true;
    jumpTargetRef.current = pendingJump.segmentId;
    jumpSearchRef.current = pendingJump.search;
    const prefix = JSON.stringify([project, stage]);
    for (const key of [...workspaceCache.keys()]) {
      if (key.startsWith(prefix)) workspaceCache.delete(key);
    }
    preserveFocusRef.current = pendingJump.segmentId;
    setFile("all");
    setStatus("all");
    setSearch(pendingJump.search);
    selection.reset(pendingJump.segmentId);
    onJumpConsumed?.();
  }, [pendingJump]);
  const normalizedSearch = search.trim();
  const filterPayload = {
    stage,
    ...(file !== "all" ? { file_id: file } : {}),
    ...(status !== "all" ? { status: status === "error" ? "failed" : status } : {}),
    ...(normalizedSearch ? { q: normalizedSearch } : {}),
  };
  const pageQueryKey = JSON.stringify([project, stage, file, status, normalizedSearch]);
  const showContext = status !== "all" || normalizedSearch !== "";
  const resetPageCache = useCallback(() => {
    pageGenerationRef.current += 1;
    pageCacheRef.current.clear();
    pageRequestsRef.current.clear();
  }, []);

  useEffect(() => {
    if (workspaceProjectRef.current !== project) {
      workspaceProjectRef.current = project;
      // Drop only entries of other projects. Entries are keyed by project, so
      // nothing leaks across projects, while the current project's prefetched
      // entries for unvisited stages survive for the first mount.
      for (const key of [...workspaceCache.keys()]) {
        if ((JSON.parse(key) as string[])[0] !== project) {
          workspaceCache.delete(key);
        }
      }
    }
  }, [project]);

  // Restore a cached window synchronously during render so the browser never
  // paints an empty frame when switching back to this stage. reloadIndex below
  // refreshes the cached data in the background (preserving focus when the
  // segment set is unchanged), so the list converges to the latest state.
  if (activePageQueryRef.current !== pageQueryKey) {
    const cached = workspaceCache.get(pageQueryKey);
    if (cached) {
      activePageQueryRef.current = pageQueryKey;
      resetPageCache();
      restoredScrollTopRef.current = cached.scrollTop;
      setOrderedIds(cached.orderedIds);
      setTotal(cached.total);
      setRecords(cached.records);
      // Switching to a previously visited filter set keeps the segment the
      // user just selected instead of yanking focus to the cached row; the
      // cached id only applies when the current focus is not in this window.
      const keep = selection.focusedKey && cached.orderedIds.includes(selection.focusedKey)
        ? selection.focusedKey
        : cached.focusedId;
      preserveFocusRef.current = keep;
      selection.reset(keep);
    }
  }

  useEffect(() => {
    if (activePageQueryRef.current === pageQueryKey) return;
    activePageQueryRef.current = pageQueryKey;
    resetPageCache();
    // Keep the focused segment across filter changes: reloadIndex restores
    // its focus when it survives the new filters and falls back to the first
    // row otherwise. Term-hit jumps keep their explicit target focus.
    preserveFocusRef.current = selection.focusedKey;
  }, [pageQueryKey, resetPageCache]);

  useLayoutEffect(() => {
    if (restoredScrollTopRef.current === null) return;
    if (listRef.current) listRef.current.scrollTop = restoredScrollTopRef.current;
    restoredScrollTopRef.current = null;
  });

  useEffect(() => {
    workspaceCache.set(pageQueryKey, {
      orderedIds,
      total,
      records,
      focusedId: selection.focusedKey,
      scrollTop: listRef.current?.scrollTop ?? 0,
    });
  }, [pageQueryKey, orderedIds, total, records, selection.focusedKey]);

  const reloadIndex = useCallback(async (preserveSegmentId?: string, scrollToFocus = false) => {
    const requestId = ++indexRequestRef.current;
    indexInFlightRef.current = true;
    setLoading(true);
    setListError("");
    try {
      const index = await api<{ segment_ids: string[]; total: number }>(
        `/api/v1/projects/${project}/segments/ids`,
        { method: "POST", body: JSON.stringify(filterPayload) },
      );
      if (requestId !== indexRequestRef.current) return [];
      setOrderedIds(index.segment_ids);
      setTotal(index.total);
      if (!preserveSegmentId || !index.segment_ids.includes(preserveSegmentId)) {
        // A save can move the focused row out of a filter set. In that case
        // reset only the now-invalid focus; an unchanged row keeps its
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
        // A filter change kept the focus: bring the row back into view once
        // the new index renders (saves and jumps scroll themselves).
        if (scrollToFocus && !jumpTargetRef.current) scrollToFocusRef.current = true;
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
      if (requestId === indexRequestRef.current) {
        indexInFlightRef.current = false;
        setLoading(false);
      }
    }
  }, [project, stage, file, status, normalizedSearch, resetPageCache]);

  useEffect(() => { void reloadIndex(preserveFocusRef.current, true); }, [reloadIndex]);

  const virtualizer = useVirtualizer({
    count: orderedIds.length,
    getScrollElement: () => listRef.current,
    getItemKey: (index) => orderedIds[index] ?? index,
    estimateSize: () => 82,
    overscan: 8,
  });
  const virtualItems = virtualizer.getVirtualItems();

  // A filter change kept the focused segment: bring its row back into view
  // once the new index renders. auto leaves an already-visible row alone,
  // so saves (which scroll nothing on purpose) are unaffected.
  useEffect(() => {
    if (!scrollToFocusRef.current) return;
    scrollToFocusRef.current = false;
    const index = orderedIds.indexOf(selection.focusedKey);
    if (index >= 0) virtualizer.scrollToIndex(index, { align: "auto" });
  }, [orderedIds, selection.focusedKey]);

  // Once the jump query's index has loaded, scroll to and focus the target
  // segment. The search is prefilled with the term source, so the segment is
  // normally in the index; if it is not (normalization-only hits), the
  // prefilled list still narrows the view and the first row stays focused.
  // Only the jump query's own index may consume the target: an earlier
  // unfiltered index would consume it and leave later cache restores (which
  // re-focus the cached row) without a corrective reset.
  useEffect(() => {
    const target = jumpTargetRef.current;
    if (!target) return;
    if (normalizedSearch !== jumpSearchRef.current) return;
    const index = orderedIds.indexOf(target);
    if (index < 0) return;
    jumpTargetRef.current = "";
    jumpSearchRef.current = "";
    selection.reset(target);
    virtualizer.scrollToIndex(index, { align: "auto" });
  }, [orderedIds, normalizedSearch]);

  useEffect(() => {
    if (indexInFlightRef.current) return;
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
      void api<ProjectOverview>(`/api/v1/projects/${project}/segments/query`, {
        method: "POST",
        body: JSON.stringify({ ...filterPayload, offset, limit: pageSize }),
      })
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
  }, [project, focusedId, showContext]);

  useLayoutEffect(() => {
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
            setBatchMessage("");
          }}>
            <option value="all">{translate("workspace.allFiles", language)}</option>
            {overview.files.map((item) => <option value={item.file_id} key={item.file_id}>{item.name}</option>)}
          </select>
          <div className="filter-row">
            <select value={status} onChange={(event) => {
              setStatus(event.target.value);
              setBatchMessage("");
            }}>
              {Object.entries(statusLabels).map(([value, label]) => <option key={value} value={value}>{label}</option>)}
            </select>
            <input value={search} onChange={(event) => {
              setSearch(event.target.value);
              setBatchMessage("");
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
        <div className="segment-list" ref={listRef} onScroll={(event) => {
          const cached = workspaceCache.get(pageQueryKey);
          if (cached) cached.scrollTop = event.currentTarget.scrollTop;
        }}>
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
          {!total && !loading && <div className="empty">{translate("workspace.noSegments", language)}</div>}
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
    <Modal ariaLabel={kind === "apply" ? translate("workspace.batchApply", language) : translate("workspace.batchClear", language)}>
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
    </Modal>
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
