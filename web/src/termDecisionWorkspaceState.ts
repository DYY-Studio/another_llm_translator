export type DecisionTab = "proposals" | "manual";
export type DecisionStatus = "all" | "accepted" | "rejected";
export type ManualStatus = "all" | "open" | "resolved";

export interface TermDecisionWorkspaceState {
  tab: DecisionTab;
  search: string;
  kind: string;
  status: DecisionStatus;
  manualStatus: ManualStatus;
  page: number;
  scrollTop: number;
}

export function createTermDecisionWorkspaceState(tab: DecisionTab = "proposals"): TermDecisionWorkspaceState {
  return {
    tab,
    search: "",
    kind: "",
    status: "all",
    manualStatus: "open",
    page: 0,
    scrollTop: 0,
  };
}

export function updateTermDecisionWorkspaceState(
  current: TermDecisionWorkspaceState,
  patch: Partial<TermDecisionWorkspaceState>,
): TermDecisionWorkspaceState {
  return { ...current, ...patch };
}

export function restoreTermDecisionWorkspaceState(
  cache: Map<string, TermDecisionWorkspaceState>,
  project: string,
  fallbackTab: DecisionTab = "proposals",
): TermDecisionWorkspaceState {
  const value = cache.get(project);
  return value ? { ...value } : createTermDecisionWorkspaceState(fallbackTab);
}
