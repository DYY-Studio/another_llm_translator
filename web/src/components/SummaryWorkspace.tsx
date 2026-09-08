import { useEffect, useMemo, useRef, useState } from "react";
import { api, apiErrorFromResponse } from "../api";
import { errorMessage, translate, type Language } from "../i18n";
import { nativeBridgeAvailable, saveExport } from "../native";
import { useClassicSelection } from "../useClassicSelection";
import type { ProjectOverview, RunDecision, Segment, SummaryArtifact, SummaryBoundary, SummariesResponse, TaskOptions, TaskState } from "../types";
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
  summaryArtifactHasUsableFull,
  summaryArtifactExpiryReason,
  summaryArtifactIsExpired,
  summaryArtifactSegmentIds,
  summaryProgress,
  updateSummaryWorkspaceState,
  type SummaryTab,
  type SummaryWorkspaceState,
} from "../summaryWorkspaceState";
import { Modal } from "./Modal";
import { RunDialog } from "./RunDialog";

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
  if (kind === "full") {
    const current = values.filter((item) => (
      !summaryArtifactIsExpired(item)
      && summaryArtifactHasUsableFull(item, boundary.segment_count)
    ));
    return current[current.length - 1] ?? values[values.length - 1] ?? null;
  }
  return values[values.length - 1] ?? null;
}

function hasUsableArtifact(artifacts: SummaryArtifact[], boundary: SummaryBoundary, kind: SummaryArtifact["kind"]): boolean {
  const artifact = artifactFor(artifacts, boundary, kind);
  if (!artifact) return false;
  if (kind === "full") return summaryArtifactHasUsableFull(artifact, boundary.segment_count);
  return artifact.status === "completed" && !artifact.source_changed;
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
  const hasFilter = normalized.length > 0;
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

  function changeScope(checked: boolean) {
    onSelection(toggleFilteredSelection(selectionBoundaries, selected, hasFilter ? visibleSelection : null, checked));
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
        <button className="quiet-button" type="button" onClick={() => changeScope(true)} disabled={disabled || (hasFilter && !visible.length)}>{translate(hasFilter ? "terms.summarySelectFiltered" : "terms.summarySelectAll", language)}</button>
        <button className="quiet-button" type="button" onClick={() => changeScope(false)} disabled={disabled || (hasFilter && !visible.length)}>{translate(hasFilter ? "terms.summaryDeselectFiltered" : "terms.summaryDeselectAll", language)}</button>
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
  boundaries,
  selected,
  names,
  language,
  path,
  error,
  onPath,
  onSelection,
  onClose,
  onConfirm,
}: {
  boundaries: SummaryBoundary[];
  selected: Set<string>;
  names: Map<string, string>;
  language: Language;
  path?: string;
  error?: string;
  onPath?: (next: string) => void;
  onSelection: (next: Set<string>) => void;
  onClose: () => void;
  onConfirm: () => void;
}) {
  const [search, setSearch] = useState("");
  const hasSelection = selected.size > 0;
  return (
    <Modal ariaLabel={translate("terms.summaryChooseExport", language)}>
      <div className="summary-dialog-heading">
        <div><h2>{translate("terms.summaryChooseExport", language)}</h2><p>{translate("terms.summaryExportHint", language)}</p></div>
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
      {onPath !== undefined && (
        <label>
          {translate("terms.summaryExportPath", language)}
          <input value={path ?? ""} onChange={(event) => onPath(event.target.value)} />
        </label>
      )}
      {!hasSelection && <p className="error-text summary-message">{translate("terms.summaryExportEmpty", language)}</p>}
      {error && <p className="error-text summary-message">{error}</p>}
      <div className="button-group summary-dialog-actions">
        <button className="quiet-button" type="button" onClick={onClose}>{translate("common.cancel", language)}</button>
        <button className="primary-button" type="button" disabled={!hasSelection} onClick={onConfirm}>{translate("terms.summaryExport", language)}</button>
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
  const [dialog, setDialog] = useState<"export" | null>(null);
  const [dialogSelection, setDialogSelection] = useState<Set<string>>(new Set());
  const [runOptions, setRunOptions] = useState<TaskOptions | null>(null);
  const [runKind, setRunKind] = useState<"fragment" | "full" | null>(null);
  const [exportPath, setExportPath] = useState("summary.md");
  const [message, setMessage] = useState<{ text: string; type: "error" | "success" } | null>(null);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [listOpen, setListOpen] = useState(true);
  const contentRef = useRef<HTMLDivElement>(null);
  const loadRequestRef = useRef(0);
  const workspaceProjectRef = useRef(project);
  const participationStateRef = useRef(createSummaryParticipationState());
  const participationQueueRef = useRef(Promise.resolve());
  const [participationSaving, setParticipationSaving] = useState(false);
  const [dialogError, setDialogError] = useState("");
  const partSelection = useClassicSelection();
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
  const visibleBoundaryKeys = visible.map((item) => boundaryKey(item.file_id, item.part_id));
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
      setMessage({ text: errorMessage(error, language), type: "error" });
    }
  }

  useEffect(() => {
    workspaceProjectRef.current = project;
    participationQueueRef.current = Promise.resolve();
    partSelection.reset();
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
    setMessage(null);
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
    setMessage(null);
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
          setMessage({ text: errorMessage(error, language), type: "error" });
        }
      } finally {
        if (workspaceProjectRef.current === targetProject
          && version === participationStateRef.current.version
          && !participationStateRef.current.dirty) setParticipationSaving(false);
      }
    });
  }

  function participationTargets(key: string): SelectionBoundary[] {
    const selectedVisibleKeys = visibleBoundaryKeys.filter((value) => partSelection.selectedKeys.has(value));
    const targetKeys = partSelection.selectedKeys.has(key) && selectedVisibleKeys.length > 1
      ? new Set(selectedVisibleKeys)
      : new Set([key]);
    return selectionBoundaries.filter((item) => targetKeys.has(item.key));
  }

  async function openSummaryRun(kind: "fragment" | "full") {
    if (!participation.size) {
      setMessage({ text: translate("terms.summarySelectionEmpty", language), type: "error" });
      return;
    }
    setBusy(true);
    setMessage(null);
    try {
      const options = await api<TaskOptions>(
        kind === "fragment"
          ? `/api/v1/projects/${project}/task-options/terminology?include_summaries=true&language=${encodeURIComponent(language)}`
          : `/api/v1/projects/${project}/task-options/content_summary?language=${encodeURIComponent(language)}`,
      );
      if (kind === "full") {
        const selectedBoundaries = boundaries.filter((item) => participation.has(boundaryKey(item.file_id, item.part_id)));
        const artifacts = data?.artifacts ?? [];
        const completed = selectedBoundaries.filter((item) => hasUsableArtifact(artifacts, item, "full")).length;
        const failed = selectedBoundaries.filter((item) => {
          const latest = artifactFor(artifacts, item, "full") ?? artifactFor(artifacts, item, "fragment");
          return latest?.status === "failed";
        }).length;
        setRunOptions({
          ...options,
          selected: selectedBoundaries.length,
          completed,
          failed,
          pending: selectedBoundaries.length - completed - failed,
        });
      } else {
        setRunOptions(options);
      }
      setRunKind(kind);
    } catch (error) {
      setMessage({ text: errorMessage(error, language), type: "error" });
    } finally {
      setBusy(false);
    }
  }

  async function startSummaryRun(decision: RunDecision) {
    if (!runKind || !runOptions) return;
    const kind = runKind;
    const selected = selectionItems(participation, boundaries);
    if (!selected.length) {
      setRunOptions(null);
      setRunKind(null);
      setMessage({ text: translate("terms.summarySelectionEmpty", language), type: "error" });
      return;
    }
    setRunOptions(null);
    setRunKind(null);
    setBusy(true);
    setMessage(null);
    try {
      const next = await api<TaskState>(`/api/v1/projects/${project}/tasks`, {
        method: "POST",
        body: JSON.stringify({
          stage: kind === "fragment" ? "terminology" : "content_summary",
          language,
          ...(kind === "fragment"
            ? { include_summaries: true }
            : { summary_selection: selected }),
          ...decision,
        }),
      });
      onTask(next);
    } catch (error) {
      setMessage({ text: errorMessage(error, language), type: "error" });
    } finally {
      setBusy(false);
    }
  }

  async function exportMarkdown() {
    if (!dialogSelection.size) return;
    const trimmed = exportPath.trim() || "summary.md";
    setBusy(true);
    setMessage(null);
    setDialogError("");
    try {
      const result = await api<{ path: string }>(`/api/v1/projects/${project}/summaries/export`, {
        method: "POST",
        body: JSON.stringify({ boundaries: selectionItems(dialogSelection, boundaries), path: trimmed }),
      });
      setDialog(null);
      setMessage({ text: translate("terms.summaryExportDone", language, { path: result.path }), type: "success" });
      downloadExportMarkdown(result.path);
    } catch (error) {
      const errText = errorMessage(error, language);
      setDialogError(errText);
      setMessage({ text: errText, type: "error" });
    } finally {
      setBusy(false);
    }
  }

  function downloadExportMarkdown(path: string) {
    const filename = path.split("/").pop() || "summary.md";
    const url = `/api/v1/projects/${project}/exports/download`;
    if (nativeBridgeAvailable()) {
      void saveExport(url, filename, JSON.stringify({ file: path }))
        .then((saved) => { if (saved) setMessage({ text: translate("terms.summaryExportSaved", language, { path: saved }), type: "success" }); })
        .catch((reason) => setMessage({ text: errorMessage(reason, language), type: "error" }));
      return;
    }
    void fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ file: path }),
    }).then(async (response) => {
      if (!response.ok) throw await apiErrorFromResponse(response);
      const objectUrl = URL.createObjectURL(await response.blob());
      const anchor = document.createElement("a");
      anchor.href = objectUrl;
      anchor.download = filename;
      document.body.appendChild(anchor);
      anchor.click();
      anchor.remove();
      URL.revokeObjectURL(objectUrl);
    }).catch((reason) => setMessage({ text: errorMessage(reason, language), type: "error" }));
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
          <button className="quiet-button" type="button" disabled={busy || activeSummaryTask || participationSaving || !participation.size} onClick={() => void openSummaryRun("fragment")}>{translate("terms.summaryGenerate", language)}</button>
          <button className="quiet-button" type="button" disabled={busy || activeSummaryTask || participationSaving || !participation.size} onClick={() => void openSummaryRun("full")}>{translate("terms.summaryAggregate", language)}</button>
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
              const stale = Boolean(latestArtifact && summaryArtifactIsExpired(latestArtifact));
              const rowSelected = partSelection.selectedKeys.has(key);
              return (
                <div className={`summary-boundary-row${rowSelected ? " selected" : ""}${key === focusedBoundary ? " focused" : ""}`} key={key}>
                  <button type="button" className="summary-boundary-row-main" aria-current={key === focusedBoundary ? "true" : undefined} onClick={(event) => { partSelection.select(key, visibleBoundaryKeys, event); focusBoundary(boundary); }}>
                    <span className={`summary-boundary-status${done ? " done" : failed ? " failed" : ""}${stale ? " stale" : ""}`} />
                    <span><strong>{boundary.part_id}</strong><small>{translate("terms.summaryBoundaryStats", language, { file: names.get(boundary.file_id) ?? boundary.file_id, count: boundary.segment_count })}</small></span>
                  </button>
                  <input type="checkbox" aria-label={boundaryText(boundary, names, language)} checked={participation.has(key)} disabled={participationSaving} onChange={(event) => saveParticipation(toggleFilteredSelection(selectionBoundaries, participationStateRef.current.displayed, participationTargets(key), event.target.checked))} />
                </div>
              );
            })}
            {!visible.length && <p className="summary-empty">{translate("terms.summaryNoMatch", language)}</p>}
          </div>
        </aside>
        <main className="summary-content" ref={contentRef} onScroll={(event) => setScrollTop(event.currentTarget.scrollTop)}>
          {activeSummaryTask && <div className="summary-progress"><strong>{translate("terms.summaryTaskRunning", language)}</strong><span>{task?.summary_selection_progress ? translate("terms.summaryTaskBoundaryProgress", language, { done: task.summary_selection_progress.completed, total: task.summary_selection_progress.total }) : translate("terms.summaryProgress", language, summaryProgressValue)}</span></div>}
          {message && <p className={`inline-message ${message.type === "success" ? "success-text" : "error-text"}`}>{message.text}</p>}
          {loading ? <p className="summary-empty">{translate("common.loading", language)}</p> : !focused ? <p className="summary-empty">{translate("terms.summaryNoMatch", language)}</p> : <>
            <div className="summary-tabs" role="tablist" aria-label={translate("terms.summaryTabs", language)}><button type="button" role="tab" id="summary-full-tab" aria-selected={tab === "full"} aria-controls="summary-full-panel" className={tab === "full" ? "active" : ""} onClick={() => setTab("full")}>{translate("terms.summaryFullTab", language)}</button><button type="button" role="tab" id="summary-fragment-tab" aria-selected={tab === "fragment"} aria-controls="summary-fragment-panel" className={tab === "fragment" ? "active" : ""} onClick={() => setTab("fragment")}>{translate("terms.summaryFragmentTab", language)} {fragments.length}</button></div>
            <div id={tab === "full" ? "summary-full-panel" : "summary-fragment-panel"} role="tabpanel" aria-labelledby={tab === "full" ? "summary-full-tab" : "summary-fragment-tab"}>
              {tab === "full" ? <SummaryArtifactCard artifact={full} boundary={focused} language={language} empty={translate("terms.summaryNoFull", language)} onSource={(segmentIds) => { setSourceSegmentIds(segmentIds); setSourceOpen(true); }} onRetry={() => void openSummaryRun("fragment")} retryDisabled={participationSaving || busy || activeSummaryTask || !participation.has(boundaryKey(focused.file_id, focused.part_id))} /> : <div className="summary-fragment-list">{fragments.map((artifact) => <SummaryArtifactCard artifact={artifact} boundary={focused} language={language} key={artifact.record_id} empty={translate("terms.summaryNoFragments", language)} onSource={(segmentIds) => { setSourceSegmentIds(segmentIds); setSourceOpen(true); }} onRetry={() => void openSummaryRun("fragment")} retryDisabled={participationSaving || busy || activeSummaryTask || !participation.has(boundaryKey(focused.file_id, focused.part_id))} />)}{!fragments.length && <p className="summary-empty">{translate("terms.summaryNoFragments", language)}</p>}</div>}
            </div>
          </>}
        </main>
        {sourceOpen && <SourcePanel project={project} boundary={focused} language={language} focusSegmentIds={sourceSegmentIds} onClose={() => setSourceOpen(false)} />}
      </div>
      {dialog === "export" && <SelectionDialog boundaries={boundaries} selected={dialogSelection} names={names} language={language} path={exportPath} error={dialogError} onPath={(next) => { setExportPath(next); setDialogError(""); }} onSelection={(next) => { setDialogSelection(next); setDialogError(""); }} onClose={() => { setDialog(null); setDialogError(""); }} onConfirm={() => void exportMarkdown()} />}
      {runOptions && runKind && <RunDialog
        key={`${runKind}-${runOptions.stage}-${runOptions.running_run?.run_id ?? "new"}-${runOptions.mismatched_fingerprint_completed}`}
        options={runOptions}
        language={language}
        onClose={() => { setRunOptions(null); setRunKind(null); }}
        onStart={(decision) => { void startSummaryRun(decision); }}
      />}
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
  const expired = Boolean(artifact && summaryArtifactIsExpired(artifact));
  const failed = Boolean(artifact && artifact.status === "failed");
  const incomplete = Boolean(artifact && artifact.kind === "full" && !expired
    && !summaryArtifactHasCompleteCoverage(artifact, boundary.segment_count));
  const retryable = Boolean(artifact && (expired || failed || incomplete));
  if (!artifact || failed || incomplete) {
    const detail = !artifact
      ? empty
      : artifact.status === "failed"
        ? translate("terms.summaryArtifactReason", language, { status: translate("terms.summaryArtifactFailed", language), reason: artifact.error_message || artifact.error || artifact.error_class || "" })
        : artifact.kind === "full"
          ? translate("terms.summaryArtifactIncomplete", language)
          : empty;
    return <div className={`summary-empty-card${retryable ? " summary-artifact-failure" : ""}`}>
      <p>{detail}</p>
      <div className="summary-artifact-actions">
        <button className="quiet-button" type="button" onClick={() => onSource(artifact ? summaryArtifactSegmentIds(artifact) : [])}>{translate("terms.summarySource", language)}</button>
        {retryable && <button className="quiet-button" type="button" disabled={retryDisabled} onClick={onRetry}>{translate("terms.summaryRetry", language)}</button>}
      </div>
      {retryable && retryDisabled && <small>{translate("terms.summaryRetrySelect", language)}</small>}
    </div>;
  }
  const origin = artifact.provenance?.origin === "adopted" || artifact.provenance?.origin === "adopted_fragment" ? "terms.summaryOriginAdopted" : "terms.summaryOriginLlm";
  const expiryReason = artifact ? summaryArtifactExpiryReason(artifact) : null;
  const warning = expiryReason === "source_changed"
    ? translate("terms.summarySourceChanged", language)
    : expiryReason === "dependency_changed"
      ? translate("terms.summaryDependencyChanged", language)
      : expiryReason === "provenance_unavailable"
        ? translate("terms.summaryProvenanceUnavailable", language)
        : expiryReason === "stale_status"
          ? translate("terms.summaryArtifactStale", language)
          : translate("terms.summaryArtifactExpired", language);
  const refs = summaryArtifactSegmentIds(artifact);
  const labels = refs.length
    ? refs.map((segmentId) => translate("terms.summaryReferenceLabel", language, { segment: segmentId }))
    : (artifact.refs ?? []).map((ref) => translate("terms.summaryReferenceLabel", language, { segment: ref }));
  return <article className={`summary-artifact-card${expired ? " summary-artifact-warning" : ""}`}>
    {expired && <p className="summary-artifact-warning-message">{warning}</p>}
    <div className="summary-artifact-meta"><span>{translate(origin, language)}</span><small>{artifact.model}</small></div>
    <p className="summary-artifact-text">{artifact.text || empty}</p>
    <div className="summary-artifact-footer">
      <span>{labels.length ? translate("terms.summaryReferenceList", language, { refs: labels.join(translate("terms.summaryReferenceSeparator", language)) }) : ""}</span>
      <div className="summary-artifact-actions">
        <button className="quiet-button" type="button" onClick={() => onSource(refs)}>{translate("terms.summarySource", language)}</button>
        {expired && <button className="quiet-button" type="button" disabled={retryDisabled} onClick={onRetry}>{translate("terms.summaryRetry", language)}</button>}
      </div>
    </div>
    {expired && retryDisabled && <small>{translate("terms.summaryRetrySelect", language)}</small>}
  </article>;
}
