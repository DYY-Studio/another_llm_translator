export type SummaryTab = "full" | "fragment";
export type TermsSubpage = "library" | "decision" | "summary";

export interface SummaryParticipationItem {
  file_id: string;
  part_id: string;
  selected: boolean;
}

export interface SummaryParticipationState {
  confirmed: Set<string>;
  displayed: Set<string>;
  version: number;
  dirty: boolean;
}

function participationKey(item: Pick<SummaryParticipationItem, "file_id" | "part_id">): string {
  return JSON.stringify([item.file_id, item.part_id]);
}

export function selectedSummaryParticipation(items: SummaryParticipationItem[]): Set<string> {
  return new Set(items.filter((item) => item.selected).map(participationKey));
}

export function createSummaryParticipationState(initial = new Set<string>()): SummaryParticipationState {
  const snapshot = new Set(initial);
  return { confirmed: snapshot, displayed: new Set(snapshot), version: 0, dirty: false };
}

export function beginSummaryParticipation(
  current: SummaryParticipationState,
  selected: Set<string>,
): { state: SummaryParticipationState; version: number } {
  const version = current.version + 1;
  return {
    version,
    state: {
      confirmed: new Set(current.confirmed),
      displayed: new Set(selected),
      version,
      dirty: true,
    },
  };
}

export function applySummaryParticipationFetch(
  current: SummaryParticipationState,
  requestVersion: number,
  requestWasDirty: boolean,
  items: SummaryParticipationItem[],
): SummaryParticipationState {
  if (requestWasDirty || current.dirty || current.version !== requestVersion) return current;
  const confirmed = selectedSummaryParticipation(items);
  return { confirmed, displayed: new Set(confirmed), version: current.version, dirty: false };
}

export function resolveSummaryParticipationPut(
  current: SummaryParticipationState,
  version: number,
  items: SummaryParticipationItem[],
): SummaryParticipationState {
  const confirmed = selectedSummaryParticipation(items);
  if (version !== current.version) return { ...current, confirmed };
  return { confirmed, displayed: new Set(confirmed), version, dirty: false };
}

export function rejectSummaryParticipationPut(
  current: SummaryParticipationState,
  version: number,
): SummaryParticipationState {
  if (version !== current.version) return current;
  return { confirmed: new Set(current.confirmed), displayed: new Set(current.confirmed), version, dirty: false };
}

interface SummaryArtifactLike {
  kind?: string;
  status?: string;
  text?: string | null;
  expired?: boolean;
  expiry_reason?: string | null;
  source_changed?: boolean;
  refs?: string[];
  source_range?: unknown;
  file_id?: string;
  part_id?: string;
}

export interface SummaryProgressBoundary {
  key: string;
  fileId: string;
  partId: string;
  segmentCount: number;
}

export function termsSubpageForTask(stage: string, includeSummaries = false): TermsSubpage {
  if (stage === "terminology_decision") return "decision";
  if (stage === "content_summary" || (stage === "terminology" && includeSummaries)) return "summary";
  return "library";
}

function rangeSegments(artifact: SummaryArtifactLike): Array<Record<string, unknown>> {
  if (!artifact.source_range || typeof artifact.source_range !== "object") return [];
  const values = (artifact.source_range as { segments?: unknown }).segments;
  return Array.isArray(values)
    ? values.filter((value): value is Record<string, unknown> => typeof value === "object" && value !== null)
    : [];
}

export function summaryArtifactSegmentIds(artifact: SummaryArtifactLike): string[] {
  const segments = rangeSegments(artifact);
  const ids = segments.map((segment) => String(segment.original_segment_id ?? segment.segment_id ?? "")).filter(Boolean);
  const byId = new Set(ids);
  const result: string[] = [];
  for (const ref of artifact.refs ?? []) {
    const direct = byId.has(ref) ? ref : /^\d+$/.test(ref) ? ids[Number(ref) - 1] : undefined;
    if (direct && !result.includes(direct)) result.push(direct);
  }
  return result;
}

export function summaryArtifactHasCompleteCoverage(artifact: SummaryArtifactLike, segmentCount: number): boolean {
  if (artifact.kind !== "full" || artifact.status !== "completed" || artifact.source_changed) return false;
  const ids = new Set(rangeSegments(artifact).map((segment) => String(segment.original_segment_id ?? segment.segment_id ?? "")).filter(Boolean));
  return segmentCount > 0 && ids.size === segmentCount;
}

export function summaryArtifactIsExpired(artifact: SummaryArtifactLike): boolean {
  return Boolean(artifact.expired || artifact.source_changed || artifact.status === "stale");
}

export function summaryArtifactExpiryReason(artifact: SummaryArtifactLike): string | null {
  if (!summaryArtifactIsExpired(artifact)) return null;
  if (artifact.expiry_reason) return artifact.expiry_reason;
  if (artifact.source_changed) return "source_changed";
  if (artifact.status === "stale") return "stale_status";
  return "dependency_changed";
}

export function summaryArtifactHasUsableFull(artifact: SummaryArtifactLike, segmentCount: number): boolean {
  if (artifact.kind !== "full" || artifact.text == null || artifact.status === "failed") return false;
  return summaryArtifactIsExpired(artifact)
    || summaryArtifactHasCompleteCoverage(artifact, segmentCount);
}

export function summaryProgress(
  boundaries: SummaryProgressBoundary[],
  selected: Set<string>,
  artifacts: SummaryArtifactLike[],
): { done: number; total: number } {
  const selectedBoundaries = boundaries.filter((boundary) => selected.has(boundary.key));
  const done = selectedBoundaries.filter((boundary) => artifacts.some((artifact) => (
    artifact.file_id === boundary.fileId
    && artifact.part_id === boundary.partId
    && summaryArtifactHasUsableFull(artifact, boundary.segmentCount)
  ))).length;
  return { done, total: selectedBoundaries.length };
}

export interface SummaryWorkspaceState {
  search: string;
  focusedBoundary: string;
  tab: SummaryTab;
  sourceOpen: boolean;
  scrollTop: number;
}

export function createSummaryWorkspaceState(): SummaryWorkspaceState {
  return {
    search: "",
    focusedBoundary: "",
    tab: "full",
    sourceOpen: false,
    scrollTop: 0,
  };
}

export function updateSummaryWorkspaceState(
  current: SummaryWorkspaceState,
  patch: Partial<SummaryWorkspaceState>,
): SummaryWorkspaceState {
  return { ...current, ...patch };
}

export function restoreSummaryWorkspaceState(
  cache: Map<string, SummaryWorkspaceState>,
  project: string,
): SummaryWorkspaceState {
  const value = cache.get(project);
  return value ? { ...value } : createSummaryWorkspaceState();
}
