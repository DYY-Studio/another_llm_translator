export interface SummaryBoundary {
  key: string;
  fileId: string;
  partId: string;
}

export type SelectionState = "checked" | "unchecked" | "partial";

export interface BoundarySelectionState {
  selectedCount: number;
  visibleCount: number;
  hiddenSelectedCount: number;
  allSelected: boolean;
  visibleAllSelected: boolean;
}

export function toggleFilteredSelection(
  allBoundaries: SummaryBoundary[],
  selected: Set<string>,
  visible: SummaryBoundary[] | null,
  checked: boolean,
): Set<string> {
  const next = new Set(selected);
  const targets = visible ?? allBoundaries;
  for (const boundary of targets) {
    if (checked) next.add(boundary.key);
    else next.delete(boundary.key);
  }
  return next;
}

export function boundarySelectionState(
  allBoundaries: SummaryBoundary[],
  selected: Set<string>,
  visible: SummaryBoundary[],
): BoundarySelectionState {
  const visibleKeys = new Set(visible.map((item) => item.key));
  const selectedCount = allBoundaries.reduce((count, item) => count + (selected.has(item.key) ? 1 : 0), 0);
  const visibleSelectedCount = visible.reduce((count, item) => count + (selected.has(item.key) ? 1 : 0), 0);
  return {
    selectedCount,
    visibleCount: visible.length,
    hiddenSelectedCount: [...selected].filter((key) => !visibleKeys.has(key)).length,
    allSelected: allBoundaries.length > 0 && selectedCount === allBoundaries.length,
    visibleAllSelected: visible.length > 0 && visibleSelectedCount === visible.length,
  };
}

export function fileSelectionState(
  allBoundaries: SummaryBoundary[],
  selected: Set<string>,
  fileId: string,
): SelectionState {
  const boundaries = allBoundaries.filter((item) => item.fileId === fileId);
  const selectedCount = boundaries.filter((item) => selected.has(item.key)).length;
  if (selectedCount === 0) return "unchecked";
  if (selectedCount === boundaries.length) return "checked";
  return "partial";
}
