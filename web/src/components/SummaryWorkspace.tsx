import { useEffect, useMemo, useRef, useState } from "react";
import { api, usageErrorReason } from "../api";
import { errorMessage, translate, type Language } from "../i18n";
import type { ProjectOverview, Segment, SummaryArtifact, SummaryBoundary, SummariesResponse, TaskState } from "../types";
import {
  boundarySelectionState,
  fileSelectionState,
  toggleFilteredSelection,
  type SelectionState,
  type SummaryBoundary as SelectionBoundary,
} from "../summarySelection";
import {
  createSummaryWorkspaceState,
  restoreSummaryWorkspaceState,
  applySummaryParticipationFetch,
  beginSummaryParticipation,
  createSummaryParticipationState,
  rejectSummaryParticipationPut,
  resolveSummaryParticipationPut,
  summaryArtifactHasCompleteCoverage,
  summaryArtifactSegmentIds,
  summaryProgress,
  updateSummaryWorkspaceState,
  type SummaryTab,
  type SummaryWorkspaceState,
} from "../summaryWorkspaceState";
import { Modal } from "./Modal";

interface SummaryWorkspaceProps {
  project: string;
  overview: ProjectOverview;
  language: Language;
  task: TaskState | null;
  onTask: (task: TaskState) => void;
  onClose: () => void;
}

const workspaceCache = new Map<string, SummaryWorkspaceState>();

function boundaryKey(fileId: string, partId: string): string {
  return JSON.stringify([fileId, partId]);
}

function asSelectionBoundary(value: SummaryBoundary): SelectionBoundary {
  return {
    key: boundaryKey(value.file_id, value.part_id),
    fileId: value.file_id,
    partId: value.part_id,
  };
}

function artifactFor(
  artifacts: SummaryArtifact[],
  boundary: SummaryBoundary | null,
  kind: SummaryArtifact["kind"],
): SummaryArtifact | null {
  if (!boundary) return null;
  const values = artifacts.filter((item) => (
    item.file_id === boundary.file_id
    && item.part_id === boundary.part_id
    && item.kind === kind
  ));
  return values[values.length - 1] ?? null;
}

function hasUsableArtifact(artifacts: SummaryArtifact[], boundary: SummaryBoundary, kind: SummaryArtifact["kind"]): boolean {
  const artifact = artifactFor(artifacts, boundary, kind);
  if (!artifact || artifact.status !== "completed" || artifact.source_changed) return false;
  return kind === "full" ? summaryArtifactHasCompleteCoverage(artifact, boundary.segment_count) : true;
}

function boundaryText(boundary: SummaryBoundary, names: Map<string, string>, language: Language): string {
  return translate("terms.summaryBoundaryLabel", language, {
    file: names.get(boundary.file_id) ?? boundary.file_id,
    part: boundary.part_id,
  });
}

function selectionItems(values: Set<string>, boundaries: SummaryBoundary[]): Array<{ file_id: string; part_id: string }> {
  return boundaries
    .filter((item) => values.has(boundaryKey(item.file_id, item.part_id)))
    .map((item) => ({ file_id: item.file_id, part_id: item.part_id }));
}

function stateForInput(state: SelectionState): { checked: boolean; indeterminate: boolean } {
  return { checked: state === "checked", indeterminate: state === "partial" };
}

function BoundaryCheckbox({
  state,
  label,
  disabled = false,
  onChange,
}: {
  state: SelectionState;
  label: string;
  disabled?: boolean;
  onChange: (checked: boolean) => void;
}) {
  const inputRef = useRef<HTMLInputElement>(null);
  useEffect(() => {
    if (inputRef.current) inputRef.current.indeterminate = state === "partial";
  }, [state]);
  const inputState = stateForInput(state);
  return (
    <input
      ref={inputRef}
      type="checkbox"
      aria-label={label}
      disabled={disabled}
      checked={inputState.checked}
      onChange={(event) => onChange(event.target.checked)}
    />
  );
}

function SelectionControls({
  boundaries,
  selected,
  search,
  onSearch,
  onSelection,
  names,
  language,
  showBoundaryRows = true,
  emptyMessage,
  disabled = false,
}: {
  boundaries: SummaryBoundary[];
  selected: Set<string>;
  search: string;
  onSearch: (value: string) => void;
  onSelection: (next: Set<string>) => void;
  names: Map<string, string>;
  language: Language;
  showBoundaryRows?: boolean;
  emptyMessage: string;
  disabled?: boolean;
}) {
  const selectionBoundaries = useMemo(() => boundaries.map(asSelectionBoundary), [boundaries]);
  const normalized = search.trim().toLocaleLowerCase();
  const visible = useMemo(() => boundaries.filter((item) => (
    !normalized
    || boundaryText(item, names, language).toLocaleLowerCase().includes(normalized)
    || item.file_id.toLocaleLowerCase().includes(normalized)
    || item.part_id.toLocaleLowerCase().includes(normalized)
  )), [boundaries, language, names, normalized]);
  const visibleSelection = useMemo(() => visible.map(asSelectionBoundary), [visible]);
  const summary = boundarySelectionState(selectionBoundaries, selected, visibleSelection);
  const fileIds = useMemo(() => Array.from(new Set(boundaries.map((item) => item.file_id))), [boundaries]);
  const boundariesByFile = useMemo(() => {
    const value = new Map<string, SummaryBoundary[]>();
    for (const boundary of boundaries) {
      const current = value.get(boundary.file_id) ?? [];
      current.push(boundary);
      value.set(boundary.file_id, current);
    }
    return value;
  }, [boundaries]);

  function changeAll(checked: boolean) {
    onSelection(toggleFilteredSelection(selectionBoundaries, selected, null, checked));
  }

  function changeVisible(checked: boolean) {
    onSelection(toggleFilteredSelection(selectionBoundaries, selected, visibleSelection, checked));
  }

  function changeFile(fileId: string, checked: boolean) {
    const fileBoundaries = (boundariesByFile.get(fileId) ?? []).map(asSelectionBoundary);
    onSelection(toggleFilteredSelection(selectionBoundaries, selected, fileBoundaries, checked));
  }

  return (
    <div className="summary-selection-controls">
      <input
        className="summary-search"
        value={search}
        onChange={(event) => onSearch(event.target.value)}
        placeholder={translate("terms.summarySearch", language)}
      />
      <div className="summary-selection-actions">
        <button className="quiet-button" type="button" onClick={() => changeAll(true)} disabled={disabled}>{translate("terms.summarySelectAll", language)}</button>
        <button className="quiet-button" type="button" onClick={() => changeAll(false)} disabled={disabled}>{translate("terms.summaryDeselectAll", language)}</button>
        <button className="quiet-button" type="button" onClick={() => changeVisible(true)} disabled={disabled || !visible.length}>{translate("terms.summarySelectFiltered", language)}</button>
        <button className="quiet-button" type="button" onClick={() => changeVisible(false)} disabled={disabled || !visible.length}>{translate("terms.summaryDeselectFiltered", language)}</button>
      </div>
      <div className="summary-selection-count" aria-live="polite">
        <span>{translate("terms.summarySelectedCount", language, { selected: summary.selectedCount, total: boundaries.length })}</span>
        {summary.hiddenSelectedCount > 0 && <span>{translate("terms.summaryHiddenSelected", language, { count: summary.hiddenSelectedCount })}</span>}
      </div>
      <div className="summary-file-selection">
        {fileIds.map((fileId) => {
          const state = fileSelectionState(selectionBoundaries, selected, fileId);
          return (
            <label key={fileId} className="summary-file-checkbox">
              <BoundaryCheckbox state={state} label={names.get(fileId) ?? fileId} disabled={disabled} onChange={(checked) => changeFile(fileId, checked)} />
              <span>{names.get(fileId) ?? fileId}</span>
            </label>
          );
        })}
      </div>
      {showBoundaryRows && <div className="summary-boundary-selection-list">
          {visible.map((boundary) => {
            const key = boundaryKey(boundary.file_id, boundary.part_id);
            return (
              <label key={key} className="summary-boundary-selection-row">
                <input
                  type="checkbox"
                  checked={selected.has(key)}
                  aria-label={boundaryText(boundary, names, language)}
                  disabled={disabled}
                  onChange={(event) => onSelection(toggleFilteredSelection(selectionBoundaries, selected, [asSelectionBoundary(boundary)], event.target.checked))}
                />
                <span><strong>{boundary.part_id}</strong><small>{translate("terms.summaryBoundaryStats", language, { file: names.get(boundary.file_id) ?? boundary.file_id, count: boundary.segment_count })}</small></span>
              </label>
            );
          })}
          {!visible.length && <p className="summary-empty">{emptyMessage}</p>}
        </div>}
    </div>
  );
}

function SourcePanel({
  project,
  boundary,
  language,
  focusSegmentIds,
  onClose,
}: {
  project: string;
  boundary: SummaryBoundary | null;
  language: Language;
  focusSegmentIds: string[];
  onClose: () => void;
}) {
  const [segments, setSegments] = useState<Segment[]>([]);
  const [loading, setLoading] = useState(false);
  const [message, setMessage] = useState("");
  const articleRefs = useRef(new Map<string, HTMLElement>());
  const focusedIds = useMemo(() => new Set(focusSegmentIds), [focusSegmentIds]);
  useEffect(() => {
    if (!boundary) return;
    let active = true;
    setLoading(true);
    setMessage("");
    void (async () => {
      const values: Segment[] = [];
      let offset = 0;
      let total = 0;
      do {
        const page = await api<{ segments: Segment[]; total_segments: number }>(
          `/api/v1/projects/${project}/segments/query`,
          {
            method: "POST",
            body: JSON.stringify({
              stage: "translation",
              file_id: boundary.file_id,
              part_id: boundary.part_id,
              offset,
              limit: 500,
            }),
          },
        );
        values.push(...page.segments);
        total = page.total_segments;
        offset += page.segments.length;
        if (page.segments.length === 0) break;
      } while (offset < total);
      if (active) setSegments(values);
    })().catch((error) => {
      if (active) setMessage(errorMessage(error, language));
    }).finally(() => {
      if (active) setLoading(false);
    });
    return () => { active = false; };
  }, [boundary?.file_id, boundary?.part_id, language, project]);
  useEffect(() => {
    if (loading || !focusSegmentIds.length) return;
    articleRefs.current.get(focusSegmentIds[0])?.scrollIntoView({ block: "center" });
  }, [focusSegmentIds, loading]);
  return (
    <aside className="summary-source-panel">
      <header>
        <div><strong>{translate("terms.summarySourceTitle", language)}</strong><small>{boundary ? translate("terms.summaryBoundaryLabel", language, { file: boundary.file_id, part: boundary.part_id }) : ""}</small></div>
        <button className="quiet-button" type="button" onClick={onClose}>×</button>
      </header>
      {loading && <p className="summary-empty">{translate("terms.summarySourceLoading", language)}</p>}
      {message && <p className="error-text summary-message">{message}</p>}
      {!loading && !message && !segments.length && <p className="summary-empty">{translate("terms.summarySourceEmpty", language)}</p>}
      <div className="summary-source-list">
        {segments.map((segment) => <article
          className={focusedIds.has(segment.segment_id) ? "referenced" : ""}
          key={segment.segment_id}
          ref={(node) => {
            if (node) articleRefs.current.set(segment.segment_id, node);
            else articleRefs.current.delete(segment.segment_id);
          }}
        ><small>{segment.segment_id}</small><p>{segment.source}</p></article>)}
      </div>
    </aside>
  );
}

function SelectionDialog({
  mode,
  boundaries,
  selected,
  names,
  language,
  onSelection,
  onClose,
  onConfirm,
}: {
  mode: "aggregate" | "export";
  boundaries: SummaryBoundary[];
  selected: Set<string>;
  names: Map<string, string>;
  language: Language;
  onSelection: (next: Set<string>) => void;
  onClose: () => void;
  onConfirm: () => void;
}) {
  const [search, setSearch] = useState("");
  const hasSelection = selected.size > 0;
  return (
    <Modal ariaLabel={translate(mode === "aggregate" ? "terms.summaryChooseAggregate" : "terms.summaryChooseExport", language)}>
      <div className="summary-dialog-heading">
        <div><h2>{translate(mode === "aggregate" ? "terms.summaryChooseAggregate" : "terms.summaryChooseExport", language)}</h2><p>{translate("terms.summaryAggregateHint", language)}</p></div>
        <button className="quiet-button" type="button" onClick={onClose}>×</button>
      </div>
      <SelectionControls
        boundaries={boundaries}
        selected={selected}
        search={search}
        onSearch={setSearch}
        onSelection={onSelection}
        names={names}
        language={language}
        emptyMessage={translate("terms.summaryNoMatch", language)}
      />
      {!hasSelection && <p className="error-text summary-message">{translate(mode === "aggregate" ? "terms.summaryAggregateEmpty" : "terms.summaryExportEmpty", language)}</p>}
      <div className="button-group summary-dialog-actions">
        <button className="quiet-button" type="button" onClick={onClose}>{translate("common.cancel", language)}</button>
        <button className="primary-button" type="button" disabled={!hasSelection} onClick={onConfirm}>{translate(mode === "aggregate" ? "terms.summaryAggregate" : "terms.summaryExport", language)}</button>
      </div>
    </Modal>
  );
}

export function SummaryWorkspace({ project, overview, language, task, onTask, onClose }: SummaryWorkspaceProps) {
  const initial = restoreSummaryWorkspaceState(workspaceCache, project);
  const [data, setData] = useState<SummariesResponse | null>(null);
  const [search, setSearch] = useState(initial.search);
  const [focusedBoundary, setFocusedBoundary] = useState(initial.focusedBoundary);
  const [tab, setTab] = useState<SummaryTab>(initial.tab);
  const [sourceOpen, setSourceOpen] = useState(initial.sourceOpen);
  const [sourceSegmentIds, setSourceSegmentIds] = useState<string[]>([]);
  const [scrollTop, setScrollTop] = useState(initial.scrollTop);
  const [participation, setParticipation] = useState<Set<string>>(new Set());
  const [dialog, setDialog] = useState<"aggregate" | "export" | null>(null);
  const [dialogSelection, setDialogSelection] = useState<Set<string>>(new Set());
  const [message, setMessage] = useState("");
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [listOpen, setListOpen] = useState(true);
  const contentRef = useRef<HTMLDivElement>(null);
  const loadRequestRef = useRef(0);
  const workspaceProjectRef = useRef(project);
  const participationStateRef = useRef(createSummaryParticipationState());
  const participationQueueRef = useRef(Promise.resolve());
  const [participationSaving, setParticipationSaving] = useState(false);
  const [conflict, setConflict] = useState<"unfinished_run" | "mismatched_fingerprint" | null>(null);
  const [conflictResume, setConflictResume] = useState(false);
  const [conflictReuse, setConflictReuse] = useState(false);
  const [conflictForce, setConflictForce] = useState(false);
  workspaceProjectRef.current = project;

  const names = useMemo(() => new Map(overview.files.map((file) => [file.file_id, file.name])), [overview.files]);
  const boundaries = data?.boundaries ?? [];
  const selectionBoundaries = useMemo(() => boundaries.map(asSelectionBoundary), [boundaries]);
  const normalized = search.trim().toLocaleLowerCase();
  const visible = useMemo(() => boundaries.filter((item) => (
    !normalized
    || boundaryText(item, names, language).toLocaleLowerCase().includes(normalized)
    || item.file_id.toLocaleLowerCase().includes(normalized)
    || item.part_id.toLocaleLowerCase().includes(normalized)
  )), [boundaries, language, names, normalized]);
  const focused = boundaries.find((item) => boundaryKey(item.file_id, item.part_id) === focusedBoundary) ?? visible[0] ?? boundaries[0] ?? null;
  const full = artifactFor(data?.artifacts ?? [], focused, "full");
  const fragments = (data?.artifacts ?? []).filter((item) => item.file_id === focused?.file_id && item.part_id === focused?.part_id && item.kind === "fragment").slice().reverse();
  const summarySelected = boundarySelectionState(selectionBoundaries, participation, selectionBoundaries);
  const progressBoundaries = useMemo(() => boundaries.map((item) => ({
    key: boundaryKey(item.file_id, item.part_id),
    fileId: item.file_id,
    partId: item.part_id,
    segmentCount: item.segment_count,
  })), [boundaries]);
  const summaryProgressValue = summaryProgress(progressBoundaries, participation, data?.artifacts ?? []);
  const activeSummaryTask = Boolean(
    task && (task.stage === "content_summary" || (task.stage === "terminology" && task.include_summaries))
    && ["queued", "running", "cancelling"].includes(task.status),
  );

  async function loadData(targetProject = project) {
    const requestId = ++loadRequestRef.current;
    const participationVersionAtStart = participationStateRef.current.version;
    const participationDirtyAtStart = participationStateRef.current.dirty;
    try {
      const value = await api<SummariesResponse>(`/api/v1/projects/${targetProject}/summaries`);
      if (requestId !== loadRequestRef.current) return;
      setData(value);
      const currentParticipationState = participationStateRef.current;
      const nextParticipationState = applySummaryParticipationFetch(
        currentParticipationState,
        participationVersionAtStart,
        participationDirtyAtStart,
        value.participation,
      );
      participationStateRef.current = nextParticipationState;
      if (nextParticipationState !== currentParticipationState) {
        setParticipation(new Set(nextParticipationState.displayed));
      }
      setLoading(false);
    } catch (error) {
      if (requestId !== loadRequestRef.current) return;
      setLoading(false);
      setMessage(errorMessage(error, language));
    }
  }

  useEffect(() => {
    workspaceProjectRef.current = project;
    participationQueueRef.current = Promise.resolve();
    const restored = restoreSummaryWorkspaceState(workspaceCache, project);
    setSearch(restored.search);
    setFocusedBoundary(restored.focusedBoundary);
    setTab(restored.tab);
    setSourceOpen(restored.sourceOpen);
    setScrollTop(restored.scrollTop);
    setData(null);
    participationStateRef.current = createSummaryParticipationState();
    setParticipation(new Set());
    setParticipationSaving(false);
    setLoading(true);
    setMessage("");
    void loadData(project);
    return () => { loadRequestRef.current += 1; };
  }, [project]);

  useEffect(() => {
    if (!activeSummaryTask) return;
    const timer = window.setInterval(() => {
      void loadData(project);
    }, 1200);
    return () => window.clearInterval(timer);
  }, [activeSummaryTask, language, project]);

  useEffect(() => {
    if (task?.stage !== "content_summary" && !(task?.stage === "terminology" && task.include_summaries)) return;
    if (["completed", "failed", "cancelled"].includes(task.status)) void loadData(project);
  }, [task?.task_id, task?.status, task?.stage, task?.include_summaries, language]);

  useEffect(() => {
    if (loading) return;
    contentRef.current?.scrollTo({ top: scrollTop });
  }, [loading, project]);

  useEffect(() => {
    const next = updateSummaryWorkspaceState(createSummaryWorkspaceState(), { search, focusedBoundary, tab, sourceOpen, scrollTop });
    workspaceCache.set(project, next);
  }, [focusedBoundary, project, search, scrollTop, sourceOpen, tab]);

  useEffect(() => {
    if (!boundaries.length) return;
    if (!boundaries.some((item) => boundaryKey(item.file_id, item.part_id) === focusedBoundary)) {
      setFocusedBoundary(boundaryKey(boundaries[0].file_id, boundaries[0].part_id));
    }
  }, [boundaries, focusedBoundary]);

  function focusBoundary(value: SummaryBoundary) {
    setFocusedBoundary(boundaryKey(value.file_id, value.part_id));
    setSourceOpen(false);
    setSourceSegmentIds([]);
    contentRef.current?.scrollTo({ top: 0 });
    setScrollTop(0);
  }

  function saveParticipation(next: Set<string>) {
    const targetProject = project;
    const nextValue = new Set(next);
    const started = beginSummaryParticipation(participationStateRef.current, nextValue);
    participationStateRef.current = started.state;
    const version = started.version;
    setParticipation(new Set(started.state.displayed));
    setParticipationSaving(true);
    setMessage("");
    participationQueueRef.current = participationQueueRef.current.then(async () => {
      if (workspaceProjectRef.current !== targetProject) return;
      try {
        const result = await api<{ participation: Array<{ file_id: string; part_id: string; selected: boolean }> }>(`/api/v1/projects/${targetProject}/summaries/participation`, {
          method: "PUT",
          body: JSON.stringify({ boundaries: selectionItems(nextValue, boundaries), selected: true }),
        });
        if (workspaceProjectRef.current !== targetProject) return;
        participationStateRef.current = resolveSummaryParticipationPut(
          participationStateRef.current,
          version,
          result.participation,
        );
        setParticipation(new Set(participationStateRef.current.displayed));
      } catch (error) {
        if (workspaceProjectRef.current === targetProject && version === participationStateRef.current.version) {
          participationStateRef.current = rejectSummaryParticipationPut(participationStateRef.current, version);
          setParticipation(new Set(participationStateRef.current.displayed));
          setMessage(errorMessage(error, language));
        }
      } finally {
        if (workspaceProjectRef.current === targetProject
          && version === participationStateRef.current.version
          && !participationStateRef.current.dirty) setParticipationSaving(false);
      }
    });
  }

  async function startSummaries(decision: { resume: boolean; reuse: boolean; force: boolean }) {
    if (!participation.size) {
      setMessage(translate("terms.summarySelectionEmpty", language));
      return;
    }
    setBusy(true);
    setMessage("");
    try {
      const next = await api<TaskState>(`/api/v1/projects/${project}/tasks`, {
        method: "POST",
        body: JSON.stringify({
          stage: "terminology",
          language,
          include_summaries: true,
          run_action: decision.resume ? "resume" : decision.force ? "decline" : null,
          reuse_mixed_fingerprints: decision.reuse,
          force: decision.force,
        }),
      });
      onTask(next);
      setConflict(null);
    } catch (error) {
      const reason = usageErrorReason(error);
      if (reason === "unfinished_run" || reason === "mismatched_fingerprint") {
        setConflict(reason);
        setConflictResume(reason === "unfinished_run");
        setConflictReuse(false);
        setConflictForce(reason === "mismatched_fingerprint");
        return;
      }
      setMessage(errorMessage(error, language));
    } finally {
      setBusy(false);
    }
  }

  async function generateSummaries() {
    if (!participation.size) {
      setMessage(translate("terms.summarySelectionEmpty", language));
      return;
    }
    setBusy(true);
    setMessage("");
    try {
      const next = await api<TaskState>(`/api/v1/projects/${project}/tasks`, {
        method: "POST",
        body: JSON.stringify({ stage: "terminology", language, include_summaries: true }),
      });
      onTask(next);
    } catch (error) {
      const reason = usageErrorReason(error);
      if (reason === "unfinished_run" || reason === "mismatched_fingerprint") {
        setConflict(reason);
        setConflictResume(reason === "unfinished_run");
        setConflictReuse(false);
        setConflictForce(reason === "mismatched_fingerprint");
        return;
      }
      setMessage(errorMessage(error, language));
    } finally {
      setBusy(false);
    }
  }

  async function aggregate() {
    if (!dialogSelection.size) return;
    setBusy(true);
    setMessage(translate("terms.summaryPreflight", language));
    try {
      const selected = selectionItems(dialogSelection, boundaries);
      await api(`/api/v1/projects/${project}/summaries/aggregation-preflight`, { method: "POST", body: JSON.stringify({ boundaries: selected }) });
      const next = await api<TaskState>(`/api/v1/projects/${project}/summaries/aggregate`, { method: "POST", body: JSON.stringify({ boundaries: selected }) });
      onTask(next);
      setDialog(null);
      setMessage(translate("terms.summaryAggregateDone", language));
    } catch (error) {
      setMessage(errorMessage(error, language));
    } finally {
      setBusy(false);
    }
  }

  async function exportMarkdown() {
    if (!dialogSelection.size) return;
    setBusy(true);
    setMessage("");
    try {
      const result = await api<{ path: string }>(`/api/v1/projects/${project}/summaries/export`, {
        method: "POST",
        body: JSON.stringify({ boundaries: selectionItems(dialogSelection, boundaries), path: "summary.md" }),
      });
      setDialog(null);
      setMessage(translate("terms.summaryExportDone", language, { path: result.path }));
    } catch (error) {
      setMessage(errorMessage(error, language));
    } finally {
      setBusy(false);
    }
  }

  return (
    <section className={`summary-workspace${listOpen ? "" : " summary-list-collapsed"}${sourceOpen ? " summary-source-open" : ""}`}>
      <header className="page-heading summary-action-heading">
        <div>
          <p className="summary-back"><button className="link-button" type="button" onClick={onClose}>← {translate("terms.summaryBack", language)}</button></p>
          <h1>{translate("terms.summary", language)}</h1>
          <p>{translate("terms.summaryHint", language)}</p>
        </div>
        <div className="button-group summary-heading-actions">
          <button className="quiet-button" type="button" onClick={() => setListOpen((value) => !value)}>{listOpen ? "←" : "→"} {translate(listOpen ? "terms.summaryHideList" : "terms.summaryShowList", language)}</button>
          <button className="quiet-button" type="button" disabled={busy || activeSummaryTask || participationSaving || !participation.size} onClick={() => void generateSummaries()}>{translate("terms.summaryGenerate", language)}</button>
          <button className="quiet-button" type="button" disabled={busy || activeSummaryTask} onClick={() => { setDialogSelection(new Set()); setDialog("aggregate"); }}>{translate("terms.summaryAggregate", language)}</button>
          <button className="quiet-button" type="button" disabled={busy} onClick={() => { setDialogSelection(new Set()); setDialog("export"); }}>{translate("terms.summaryExport", language)}</button>
        </div>
      </header>
      <div className="summary-layout">
        <aside className="summary-boundary-list">
          <div className="summary-participation-heading">
            <strong>{translate("terms.summarySelectedCount", language, { selected: summarySelected.selectedCount, total: boundaries.length })}</strong>
            <small>{translate("terms.summaryGenerationHint", language)}</small>
          </div>
          <SelectionControls
            boundaries={boundaries}
            selected={participation}
            search={search}
            onSearch={setSearch}
            onSelection={(next) => void saveParticipation(next)}
            names={names}
            language={language}
            showBoundaryRows={false}
            emptyMessage={translate("terms.summaryNoMatch", language)}
            disabled={participationSaving}
          />
          <div className="summary-boundary-list-rows">
            {visible.map((boundary) => {
              const key = boundaryKey(boundary.file_id, boundary.part_id);
              const fullArtifact = artifactFor(data?.artifacts ?? [], boundary, "full");
              const fragmentArtifact = artifactFor(data?.artifacts ?? [], boundary, "fragment");
              const latestArtifact = fullArtifact ?? fragmentArtifact;
              const done = hasUsableArtifact(data?.artifacts ?? [], boundary, "full");
              const failed = latestArtifact?.status === "failed";
              const stale = latestArtifact?.status === "stale" || Boolean(latestArtifact?.source_changed);
              return (
                <div className={`summary-boundary-row${key === focusedBoundary ? " focused" : ""}`} key={key}>
                  <button type="button" className="summary-boundary-row-main" aria-current={key === focusedBoundary ? "true" : undefined} onClick={() => focusBoundary(boundary)}>
                    <span className={`summary-boundary-status${done ? " done" : failed ? " failed" : stale ? " stale" : ""}`} />
                    <span><strong>{boundary.part_id}</strong><small>{translate("terms.summaryBoundaryStats", language, { file: names.get(boundary.file_id) ?? boundary.file_id, count: boundary.segment_count })}</small></span>
                  </button>
                  <input type="checkbox" aria-label={boundaryText(boundary, names, language)} checked={participation.has(key)} disabled={participationSaving} onChange={(event) => saveParticipation(toggleFilteredSelection(selectionBoundaries, participationStateRef.current.displayed, [asSelectionBoundary(boundary)], event.target.checked))} />
                </div>
              );
            })}
            {!visible.length && <p className="summary-empty">{translate("terms.summaryNoMatch", language)}</p>}
          </div>
        </aside>
        <main className="summary-content" ref={contentRef} onScroll={(event) => setScrollTop(event.currentTarget.scrollTop)}>
          {activeSummaryTask && <div className="summary-progress"><strong>{translate("terms.summaryTaskRunning", language)}</strong><span>{translate("terms.summaryProgress", language, summaryProgressValue)}</span></div>}
          {message && <p className="inline-message error-text">{message}</p>}
          {loading ? <p className="summary-empty">{translate("common.loading", language)}</p> : !focused ? <p className="summary-empty">{translate("terms.summaryNoMatch", language)}</p> : <>
            <div className="summary-tabs" role="tablist" aria-label={translate("terms.summaryTabs", language)}><button type="button" role="tab" id="summary-full-tab" aria-selected={tab === "full"} aria-controls="summary-full-panel" className={tab === "full" ? "active" : ""} onClick={() => setTab("full")}>{translate("terms.summaryFullTab", language)}</button><button type="button" role="tab" id="summary-fragment-tab" aria-selected={tab === "fragment"} aria-controls="summary-fragment-panel" className={tab === "fragment" ? "active" : ""} onClick={() => setTab("fragment")}>{translate("terms.summaryFragmentTab", language)} {fragments.length}</button></div>
            <div id={tab === "full" ? "summary-full-panel" : "summary-fragment-panel"} role="tabpanel" aria-labelledby={tab === "full" ? "summary-full-tab" : "summary-fragment-tab"}>
              {tab === "full" ? <SummaryArtifactCard artifact={full} boundary={focused} language={language} empty={translate("terms.summaryNoFull", language)} onSource={(segmentIds) => { setSourceSegmentIds(segmentIds); setSourceOpen(true); }} onRetry={() => void generateSummaries()} retryDisabled={participationSaving || busy || activeSummaryTask || !participation.has(boundaryKey(focused.file_id, focused.part_id))} /> : <div className="summary-fragment-list">{fragments.map((artifact) => <SummaryArtifactCard artifact={artifact} boundary={focused} language={language} key={artifact.record_id} empty={translate("terms.summaryNoFragments", language)} onSource={(segmentIds) => { setSourceSegmentIds(segmentIds); setSourceOpen(true); }} onRetry={() => void generateSummaries()} retryDisabled={participationSaving || busy || activeSummaryTask || !participation.has(boundaryKey(focused.file_id, focused.part_id))} />)}{!fragments.length && <p className="summary-empty">{translate("terms.summaryNoFragments", language)}</p>}</div>}
            </div>
          </>}
        </main>
        {sourceOpen && <SourcePanel project={project} boundary={focused} language={language} focusSegmentIds={sourceSegmentIds} onClose={() => setSourceOpen(false)} />}
      </div>
      {dialog === "aggregate" && <SelectionDialog mode="aggregate" boundaries={boundaries} selected={dialogSelection} names={names} language={language} onSelection={setDialogSelection} onClose={() => setDialog(null)} onConfirm={() => void aggregate()} />}
      {dialog === "export" && <SelectionDialog mode="export" boundaries={boundaries} selected={dialogSelection} names={names} language={language} onSelection={setDialogSelection} onClose={() => setDialog(null)} onConfirm={() => void exportMarkdown()} />}
      {conflict && (
        <Modal ariaLabel={translate("terms.summaryConflictTitle", language)}>
          <div className="summary-dialog-heading">
            <div><h2>{translate("terms.summaryConflictTitle", language)}</h2><p>{translate(conflict === "unfinished_run" ? "terms.summaryConflictUnfinished" : "terms.summaryConflictFingerprint", language)}</p></div>
            <button className="quiet-button" type="button" onClick={() => setConflict(null)}>×</button>
          </div>
          {conflict === "unfinished_run" && (
            <label className="radio-option decision-option">
              <input type="radio" checked={conflictResume} onChange={() => { setConflictResume(true); setConflictForce(false); }} />
              <span><strong>{translate("terms.summaryConflictResume", language)}</strong><small>{translate("terms.summaryConflictResumeHint", language)}</small></span>
            </label>
          )}
          {conflict === "mismatched_fingerprint" && (
            <label className="radio-option decision-option">
              <input type="radio" checked={conflictReuse} onChange={() => { setConflictReuse(true); setConflictForce(false); }} />
              <span><strong>{translate("terms.summaryConflictReuse", language)}</strong><small>{translate("terms.summaryConflictReuseHint", language)}</small></span>
            </label>
          )}
          <label className="radio-option decision-option">
            <input type="radio" checked={conflictForce} onChange={() => { setConflictForce(true); setConflictResume(false); setConflictReuse(false); }} />
            <span><strong>{translate("terms.summaryConflictForce", language)}</strong><small>{translate("terms.summaryConflictForceHint", language)}</small></span>
          </label>
          <div className="button-group summary-dialog-actions">
            <button className="quiet-button" type="button" disabled={busy} onClick={() => setConflict(null)}>{translate("common.cancel", language)}</button>
            <button className="primary-button" type="button" disabled={busy || (!conflictResume && !conflictReuse && !conflictForce)} onClick={() => void startSummaries({ resume: conflictResume, reuse: conflictReuse, force: conflictForce })}>{translate("terms.summaryConflictRun", language)}</button>
          </div>
        </Modal>
      )}
    </section>
  );
}

function SummaryArtifactCard({ artifact, boundary, language, empty, onSource, onRetry, retryDisabled }: {
  artifact: SummaryArtifact | null;
  boundary: SummaryBoundary;
  language: Language;
  empty: string;
  onSource: (segmentIds: string[]) => void;
  onRetry: () => void;
  retryDisabled?: boolean;
}) {
  const retryable = Boolean(artifact && (artifact.status !== "completed" || artifact.source_changed
    || (artifact.kind === "full" && !summaryArtifactHasCompleteCoverage(artifact, boundary.segment_count))));
  if (!artifact || retryable) {
    const detail = !artifact
      ? empty
      : artifact.source_changed
        ? translate("terms.summarySourceChanged", language)
        : artifact.status === "failed"
          ? translate("terms.summaryArtifactReason", language, { status: translate("terms.summaryArtifactFailed", language), reason: artifact.error_message || artifact.error || artifact.error_class || "" })
          : artifact.status === "stale"
            ? translate("terms.summaryArtifactReason", language, { status: translate("terms.summaryArtifactStale", language), reason: artifact.error_message || artifact.error || artifact.error_class || "" })
            : artifact.kind === "full"
              ? translate("terms.summaryArtifactIncomplete", language)
              : empty;
    return <div className={`summary-empty-card${retryable ? " summary-artifact-failure" : ""}`}>
      <p>{detail}</p>
      {retryable && retryDisabled && <small>{translate("terms.summaryRetrySelect", language)}</small>}
      <div className="summary-artifact-actions">
        <button className="quiet-button" type="button" onClick={() => onSource(artifact ? summaryArtifactSegmentIds(artifact) : [])}>{translate("terms.summarySource", language)}</button>
        {retryable && <button className="quiet-button" type="button" disabled={retryDisabled} onClick={onRetry}>{translate("terms.summaryRetry", language)}</button>}
      </div>
    </div>;
  }
  const origin = artifact.provenance?.origin === "adopted" || artifact.provenance?.origin === "adopted_fragment" ? "terms.summaryOriginAdopted" : "terms.summaryOriginLlm";
  const refs = summaryArtifactSegmentIds(artifact);
  const labels = refs.length
    ? refs.map((segmentId) => translate("terms.summaryReferenceLabel", language, { file: artifact.file_id, part: artifact.part_id, segment: segmentId }))
    : (artifact.refs ?? []).map((ref) => translate("terms.summaryReferenceLabel", language, { file: artifact.file_id, part: artifact.part_id, segment: ref }));
  return <article className="summary-artifact-card"><div className="summary-artifact-meta"><span>{translate(origin, language)}</span><small>{artifact.model}</small></div><p className="summary-artifact-text">{artifact.text || empty}</p><div className="summary-artifact-footer"><span>{labels.length ? translate("terms.summaryReferenceList", language, { refs: labels.join(translate("terms.summaryReferenceSeparator", language)) }) : ""}</span><button className="quiet-button" type="button" onClick={() => onSource(refs)}>{translate("terms.summarySource", language)}</button></div></article>;
}
