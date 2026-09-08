import { useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import { useInfiniteQuery, useQuery, useQueryClient, type QueryClient } from "@tanstack/react-query";
import { useVirtualizer } from "@tanstack/react-virtual";
import { api, apiErrorFromResponse } from "../api";
import { errorMessage, translate, type Language } from "../i18n";
import { isCurrentProjectRequest } from "../requestState";
import type { ProjectOverview, RelatedTerm, TaskState, Term, TermDecisionManualReviewItem, TermDecisionReviewState, TermHitsResponse, TermsResponse } from "../types";
import { fetchRelatedTerms, fetchTermHits, fetchTerms, queryKeys } from "../queries";
import { useClassicSelection } from "../useClassicSelection";
import { Modal } from "./Modal";
import { TermDecisionWorkspace } from "./TermDecisionWorkspace";
import { SummaryWorkspace } from "./SummaryWorkspace";
import { ConfirmDialog, RelatedGroupDialog, TermImportDialog, TermExportDialog, PartialPublishDialog } from "./TermDialogs";

interface TermForm {
  source: string;
  preferredTranslation: string;
  category: string;
  description: string;
  aliases: string[];
}

const emptyForm: TermForm = {
  source: "",
  preferredTranslation: "",
  category: "",
  description: "",
  aliases: [],
};

interface TermsCacheEntry {
  search: string;
  onlyConflicts: boolean;
  showDisabled: boolean;
  focusedKey: string;
  scrollTop: number;
}

const emptyManualReview: TermDecisionReviewState["manual_review"] = {
  items: [],
  total: 0,
  resolved: 0,
  remaining: 0,
};

// Survives tab switches so returning restores the view state instantly. The
// server data itself is owned by the React Query cache.
const termsCache = new Map<string, TermsCacheEntry>();
const termsProjectRef = { current: "" };

export type TermsSubpage = "library" | "decision" | "summary";
const termsSubpageCache = new Map<string, TermsSubpage>();
const termsSubpageListeners = new Set<(project: string, subpage: TermsSubpage) => void>();

export function openTermsSubpage(projectId: string, subpage: TermsSubpage) {
  termsSubpageCache.set(projectId, subpage);
  for (const listener of termsSubpageListeners) listener(projectId, subpage);
}

// Warms the query cache when a project is opened so the first visit to the
// terminology page renders instantly. A failed prefetch remains an error in
// the query cache and is surfaced when the view mounts.
export function prefetchTerms(project: string, projectId: string, queryClient: QueryClient) {
  void queryClient.prefetchQuery({
    queryKey: queryKeys.terms({ projectId }),
    queryFn: ({ signal }) => fetchTerms(project, signal),
  }).catch(() => {});
}

function formFor(term: Term): TermForm {
  return {
    source: term.source,
    preferredTranslation: term.preferred_translation ?? "",
    category: term.category ?? "",
    description: term.description ?? "",
    aliases: [...term.aliases],
  };
}

function matchesFilters(term: Term, primarySource: string, query: string, onlyConflicts: boolean, showDisabled: boolean) {
  const normalized = query.trim().toLocaleLowerCase();
  const haystack = [
    term.source,
    term.preferred_translation,
    term.category,
    term.description,
    ...term.aliases,
    primarySource,
  ].filter(Boolean).join("\n").toLocaleLowerCase();
  return (!normalized || haystack.includes(normalized))
    && (!onlyConflicts || term.has_conflicts)
    && (showDisabled || !term.disabled);
}

export function TermsView({
  project,
  projectId,
  overview,
  focusFailures = false,
  language,
  onFindSegment,
  task,
  onTask,
  onSubpageChange,
}: {
  project: string;
  projectId: string;
  overview: ProjectOverview;
  focusFailures?: boolean;
  language: Language;
  onFindSegment: (source: string, segmentId: string) => void;
  task: TaskState | null;
  onTask: (task: TaskState) => void;
  onSubpageChange?: (subpage: TermsSubpage) => void;
}) {
  const [form, setForm] = useState<TermForm>(emptyForm);
  const [search, setSearch] = useState("");
  const [onlyConflicts, setOnlyConflicts] = useState(false);
  const [showDisabled, setShowDisabled] = useState(false);
  const [message, setMessage] = useState("");
  const [saving, setSaving] = useState(false);
  const [removeOpen, setRemoveOpen] = useState(false);
  const [deleteOpen, setDeleteOpen] = useState(false);
  const [clearOpen, setClearOpen] = useState(false);
  const [importOpen, setImportOpen] = useState(false);
  const [exportOpen, setExportOpen] = useState(false);
  const [decisionOpen, setDecisionOpen] = useState(false);
  const [summaryOpen, setSummaryOpen] = useState(() => termsSubpageCache.get(projectId) === "summary");
  const [decisionInitialTab, setDecisionInitialTab] = useState<"proposals" | "manual">("proposals");
  const [manualReview, setManualReview] = useState(emptyManualReview);
  const [decisionDraftPending, setDecisionDraftPending] = useState(false);
  const [decisionPrefetchError, setDecisionPrefetchError] = useState("");
  const [decisionPrefetchAttempt, setDecisionPrefetchAttempt] = useState(0);
  const [manualFocusId, setManualFocusId] = useState<string | null>(null);
  const [termActionsOpen, setTermActionsOpen] = useState(false);
  const [exportSource, setExportSource] = useState<"published" | "scanned">("published");
  const [partialOpen, setPartialOpen] = useState(false);
  const [showScanFailures, setShowScanFailures] = useState(false);
  const [editorTab, setEditorTab] = useState<"edit" | "group" | "hits">("edit");
  const [pendingPrimary, setPendingPrimary] = useState<string | null>(null);
  const decisionPrefetchRequestRef = useRef(0);
  const activeProjectRef = useRef(projectId);
  activeProjectRef.current = projectId;
  const [pendingRelatedGroup, setPendingRelatedGroup] = useState<RelatedTerm | null>(null);
  const [pendingRelatedAlias, setPendingRelatedAlias] = useState<RelatedTerm | null>(null);
  const [pendingGroupMemberAlias, setPendingGroupMemberAlias] = useState<Term | null>(null);
  const [pendingGroupMemberLeave, setPendingGroupMemberLeave] = useState<Term | null>(null);
  const [pendingRelatedRemoval, setPendingRelatedRemoval] = useState<RelatedTerm | null>(null);
  const [relatedPrimary, setRelatedPrimary] = useState<string>("");
  const termListRef = useRef<HTMLDivElement>(null);
  const restoredScrollRef = useRef<number | null>(null);
  const suppressFocusScrollForDataRef = useRef<TermsResponse | null>(null);
  const termsRestoredRef = useRef(false);
  const selection = useClassicSelection();
  const queryClient = useQueryClient();
  const termsQuery = useQuery({
    queryKey: queryKeys.terms({ projectId }),
    queryFn: ({ signal }) => fetchTerms(project, signal),
    enabled: Boolean(project),
  });
  const data = termsQuery.data ?? null;
  const selected = data?.terms.find(
    (term) => term.normalized === selection.focusedKey,
  ) ?? null;
  const termByKey = useMemo(
    () => new Map((data?.terms ?? []).map((term) => [term.normalized, term])),
    [data],
  );
  const membersByPrimary = useMemo(() => {
    const value = new Map<string, Term[]>();
    for (const term of data?.terms ?? []) {
      if (!term.group_primary) continue;
      const members = value.get(term.group_primary) ?? [];
      members.push(term);
      value.set(term.group_primary, members);
    }
    return value;
  }, [data]);
  const selectedMatchKey = selected
    ? JSON.stringify([selected.normalized, selected.source, selected.aliases])
    : "";
  const selectedIsDisabled = Boolean(selected?.disabled);

  async function setCurrentData(value: TermsResponse) {
    if (activeProjectRef.current !== projectId) return;
    await queryClient.cancelQueries({ queryKey: queryKeys.terms({ projectId }) });
    if (activeProjectRef.current !== projectId) return;
    queryClient.setQueryData(queryKeys.terms({ projectId }), value);
    void queryClient.invalidateQueries({ queryKey: ["term-hits", projectId] }).catch(() => {});
    void queryClient.invalidateQueries({ queryKey: ["related-terms", projectId] }).catch(() => {});
  }

  useEffect(() => {
    const listener = (targetProject: string, subpage: TermsSubpage) => {
      if (targetProject !== projectId) return;
      setDecisionOpen(subpage === "decision");
      setSummaryOpen(subpage === "summary");
      onSubpageChange?.(subpage);
    };
    termsSubpageListeners.add(listener);
    const subpage = termsSubpageCache.get(projectId) ?? "library";
    setDecisionOpen(subpage === "decision");
    setSummaryOpen(subpage === "summary");
    onSubpageChange?.(subpage);
    return () => { termsSubpageListeners.delete(listener); };
  }, [onSubpageChange, projectId]);

  // Restore view state synchronously during render so switching projects does
  // not briefly reuse the previous project's filters or focus.
  if (termsProjectRef.current !== projectId) {
    termsProjectRef.current = projectId;
    selection.reset();
    setManualFocusId(null);
    termsRestoredRef.current = false;
  }
  if (!termsRestoredRef.current) {
    termsRestoredRef.current = true;
    const cached = termsCache.get(projectId);
    if (cached) {
      setSearch(cached.search);
      setOnlyConflicts(cached.onlyConflicts);
      setShowDisabled(cached.showDisabled);
      selection.reset(cached.focusedKey);
      restoredScrollRef.current = cached.scrollTop;
    } else {
      selection.reset();
    }
  }

  useEffect(() => {
    setForm(emptyForm);
    setMessage("");
  }, [projectId]);

  useEffect(() => {
    setManualReview(emptyManualReview);
    setDecisionDraftPending(false);
  }, [projectId]);

  useEffect(() => {
    const requestId = ++decisionPrefetchRequestRef.current;
    const targetProject = projectId;
    setDecisionPrefetchError("");
    void api<TermDecisionReviewState>(`/api/v1/projects/${project}/terms/decision`)
      .then((value) => {
        if (!isCurrentProjectRequest(requestId, decisionPrefetchRequestRef.current, targetProject, activeProjectRef.current)) return;
        setManualReview(value.manual_review);
        setDecisionDraftPending(Boolean(value.draft));
      })
      .catch((error) => {
        if (isCurrentProjectRequest(requestId, decisionPrefetchRequestRef.current, targetProject, activeProjectRef.current)) setDecisionPrefetchError(errorMessage(error, language));
      });
    return () => { decisionPrefetchRequestRef.current += 1; };
  }, [decisionPrefetchAttempt, language, project, projectId]);

  useEffect(() => {
    if (!data) return;
    termsCache.set(projectId, {
      search,
      onlyConflicts,
      showDisabled,
      focusedKey: selection.focusedKey,
      scrollTop: termListRef.current?.scrollTop ?? 0,
    });
  }, [projectId, data, search, onlyConflicts, showDisabled, selection.focusedKey]);

  useLayoutEffect(() => {
    if (restoredScrollRef.current === null) return;
    if (termListRef.current) termListRef.current.scrollTop = restoredScrollRef.current;
    restoredScrollRef.current = null;
  });

  useEffect(() => {
    if (focusFailures) setShowScanFailures(true);
  }, [focusFailures]);

  const hitsPageSize = 50;
  const hitsEnabled = editorTab === "hits" && Boolean(selected) && !selectedIsDisabled;
  const hitsQuery = useInfiniteQuery({
    queryKey: queryKeys.termHits({ projectId }, selected?.normalized ?? ""),
    queryFn: ({ pageParam, signal }) => fetchTermHits(project, selected!.normalized, pageParam, signal, hitsPageSize),
    initialPageParam: 0,
    getNextPageParam: (lastPage) => {
      const nextOffset = lastPage.offset + lastPage.hits.length;
      return nextOffset < lastPage.total ? nextOffset : undefined;
    },
    enabled: hitsEnabled,
  });
  const hits = useMemo<TermHitsResponse | null>(() => {
    if (editorTab === "hits" && selected?.normalized && selectedIsDisabled) {
      return { normalized: selected.normalized, source: selected.source, total: 0, offset: 0, limit: hitsPageSize, hits: [] };
    }
    if (!hitsEnabled || !hitsQuery.data?.pages.length) return null;
    const [firstPage] = hitsQuery.data.pages;
    return { ...firstPage, hits: hitsQuery.data.pages.flatMap((page) => page.hits) };
  }, [editorTab, hitsEnabled, hitsQuery.data, selected, selectedIsDisabled]);
  const hitsLoading = hitsEnabled && (hitsQuery.isPending || hitsQuery.isFetchingNextPage);
  const hitsError = hitsEnabled && hitsQuery.error ? errorMessage(hitsQuery.error, language) : "";

  const relatedEnabled = editorTab === "group" && Boolean(selected) && !selectedIsDisabled;
  const relatedQuery = useQuery({
    queryKey: queryKeys.relatedTerms({ projectId }, data?.terms_revision ?? null, selectedMatchKey),
    queryFn: ({ signal }) => fetchRelatedTerms(project, selected!.normalized, signal),
    enabled: relatedEnabled,
  });
  const related = relatedEnabled ? relatedQuery.data ?? null : null;
  const relatedLoading = relatedEnabled && (relatedQuery.isPending || relatedQuery.isFetching);
  const relatedError = relatedEnabled && relatedQuery.error ? errorMessage(relatedQuery.error, language) : "";

  function loadMoreHits() {
    if (!hitsEnabled || !hitsQuery.hasNextPage || hitsQuery.isFetchingNextPage) return;
    void hitsQuery.fetchNextPage();
  }

  const visible = useMemo(() => {
    return (data?.terms ?? []).filter(
      (term) => matchesFilters(
        term,
        term.group_primary ? termByKey.get(term.group_primary)?.source ?? "" : "",
        search,
        onlyConflicts,
        showDisabled,
      ),
    );
  }, [data, onlyConflicts, search, showDisabled, termByKey]);
  const visibleKeys = visible.map((term) => term.normalized);
  const selectedTerms = visible.filter((term) => selection.selectedKeys.has(term.normalized));
  const selectedActive = visible.filter(
    (term) => selection.selectedKeys.has(term.normalized) && !term.disabled,
  );
  const canClearStage = Boolean(
    data && (
      data.terms_revision !== null
      || data.terms.length > 0
      || data.scan.status !== "none"
      || data.scan.candidate_count > 0
    ),
  );

  const termVirtualizer = useVirtualizer({
    count: visible.length,
    getScrollElement: () => termListRef.current,
    getItemKey: (index) => visible[index]?.normalized ?? index,
    estimateSize: () => 72,
    overscan: 10,
  });
  const virtualTerms = termVirtualizer.getVirtualItems();

  // After a filter change keeps the focused term visible, make sure its row
  // stays in view: clearing a filter can move it far down the full list.
  useEffect(() => {
    if (!selection.focusedKey) return;
    if (suppressFocusScrollForDataRef.current === data) return;
    const index = visible.findIndex(
      (term) => term.normalized === selection.focusedKey,
    );
    if (index >= 0) termVirtualizer.scrollToIndex(index, { align: "auto" });
  }, [data, onlyConflicts, search, showDisabled, selection.focusedKey, termVirtualizer, visible]);

  function resetFilterSelection() {
    suppressFocusScrollForDataRef.current = null;
    selection.reset();
    setForm(emptyForm);
    setMessage("");
  }

  // Filter changes keep the focused term when it still matches the next
  // conditions, so clearing a filter returns to the term just selected.
  function clearSelectionIfFilteredOut(
    nextSearch: string,
    nextConflicts: boolean,
    nextDisabled: boolean,
  ) {
    suppressFocusScrollForDataRef.current = null;
    const focused = data?.terms.find(
      (term) => term.normalized === selection.focusedKey,
    ) ?? null;
    if (!focused || !matchesFilters(
      focused,
      focused.group_primary ? termByKey.get(focused.group_primary)?.source ?? "" : "",
      nextSearch,
      nextConflicts,
      nextDisabled,
    )) {
      resetFilterSelection();
    }
  }

  function focusTerm(term: Term) {
    suppressFocusScrollForDataRef.current = null;
    setForm(formFor(term));
    setMessage("");
  }

  function manualItemId(item: TermDecisionManualReviewItem) {
    return `${item.run_id}:${item.normalized}`;
  }

  const focusedManual = manualFocusId
    ? manualReview.items.find((item) => manualItemId(item) === manualFocusId) ?? null
    : null;
  const openManualItems = manualReview.items.filter((item) => !item.resolved);

  function openDecision(tab: "proposals" | "manual" = "proposals") {
    if (tab === "manual" && decisionDraftPending) return;
    setDecisionInitialTab(tab);
    setDecisionOpen(true);
    setSummaryOpen(false);
    openTermsSubpage(projectId, "decision");
  }

  function openSummary() {
    setDecisionOpen(false);
    setSummaryOpen(true);
    openTermsSubpage(projectId, "summary");
  }

  function updateDecisionReview(value: TermDecisionReviewState) {
    setManualReview(value.manual_review);
    setDecisionDraftPending(Boolean(value.draft));
  }

  function openManualEditor(item: TermDecisionManualReviewItem, tab: "edit" | "group") {
    const term = data?.terms.find((value) => value.normalized === item.normalized) ?? null;
    setDecisionOpen(false);
    openTermsSubpage(projectId, "library");
    setManualFocusId(manualItemId(item));
    setSearch("");
    setOnlyConflicts(false);
    setShowDisabled(Boolean(term?.disabled));
    setEditorTab(tab);
    if (term) {
      selection.reset(term.normalized);
      focusTerm(term);
    } else {
      selection.reset();
      setForm(emptyForm);
      setMessage(translate("terms.decisionManualTermMissing", language));
    }
  }

  async function setManualResolved(item: TermDecisionManualReviewItem, resolved: boolean) {
    try {
      const result = await api<{ manual_review: TermDecisionReviewState["manual_review"] }>(
        `/api/v1/projects/${project}/terms/decision/manual-review`,
        { method: "PUT", body: JSON.stringify({ run_id: item.run_id, normalized: item.normalized, resolved }) },
      );
      setManualReview(result.manual_review);
    } catch (error) {
      setMessage(errorMessage(error, language));
    }
  }

  function navigateManual(offset: number) {
    if (!focusedManual) return;
    const index = openManualItems.findIndex((item) => manualItemId(item) === manualItemId(focusedManual));
    const next = openManualItems[index + offset] ?? openManualItems[0];
    if (next) openManualEditor(next, editorTab === "group" ? "group" : "edit");
  }

  async function save(disabled: boolean) {
    if (!form.source.trim()) {
      setMessage(translate("terms.sourceRequired", language));
      return;
    }
    setSaving(true);
    setMessage("");
    try {
      const value = await api<TermsResponse>(`/api/v1/projects/${project}/terms`, {
        method: "POST",
        body: JSON.stringify({
          old_normalized: selection.focusedKey || null,
          source: form.source,
          preferred_translation: form.preferredTranslation,
          category: form.category,
          description: form.description,
          aliases: form.aliases.map((item) => item.trim()).filter(Boolean),
          disabled,
        }),
      });
      const saved = value.terms.find(
        (term) => term.source === form.source && term.disabled === disabled,
      ) ?? null;
      if (selected?.has_conflicts && saved && !saved.has_conflicts) {
        suppressFocusScrollForDataRef.current = value;
      } else {
        suppressFocusScrollForDataRef.current = null;
      }
      await setCurrentData(value);
      selection.reset(saved?.normalized ?? "");
      setForm(saved ? formFor(saved) : emptyForm);
      setMessage(disabled ? translate("terms.termRemoved", language) : selected?.disabled ? translate("terms.termRestored", language) : translate("terms.termSaved", language));
    } catch (error) {
      setMessage(errorMessage(error, language));
    } finally {
      setSaving(false);
    }
  }

  async function removeSelected() {
    setSaving(true);
    try {
      const value = await api<TermsResponse & { removed: number }>(
        `/api/v1/projects/${project}/terms/remove`,
        {
          method: "POST",
          body: JSON.stringify({
            normalized: selectedActive.map((term) => term.normalized),
          }),
        },
      );
      await setCurrentData(value);
      selection.reset();
      setForm(emptyForm);
      setMessage(translate("terms.removedCount", language, { count: value.removed }));
      setRemoveOpen(false);
    } catch (error) {
      setMessage(errorMessage(error, language));
    } finally {
      setSaving(false);
    }
  }

  async function deleteSelected() {
    setSaving(true);
    try {
      const value = await api<TermsResponse & { deleted: number }>(
        `/api/v1/projects/${project}/terms/delete`,
        {
          method: "POST",
          body: JSON.stringify({
            normalized: selectedTerms.map((term) => term.normalized),
          }),
        },
      );
      await setCurrentData(value);
      selection.reset();
      setForm(emptyForm);
      setMessage(translate("terms.deletedCount", language, { count: value.deleted }));
      setDeleteOpen(false);
    } catch (error) {
      setMessage(errorMessage(error, language));
    } finally {
      setSaving(false);
    }
  }

  async function clearTerms() {
    setSaving(true);
    setMessage("");
    try {
      const value = await api<TermsResponse>(
        `/api/v1/projects/${project}/terms/clear`,
        { method: "POST", body: JSON.stringify({ confirm: true }) },
      );
      await setCurrentData(value);
      selection.reset();
      setForm(emptyForm);
      setPendingPrimary(null);
      setPendingRelatedGroup(null);
      setPendingRelatedAlias(null);
      setPendingGroupMemberAlias(null);
      setPendingGroupMemberLeave(null);
      setPendingRelatedRemoval(null);
      setRelatedPrimary("");
      setShowScanFailures(false);
      setPartialOpen(false);
      setClearOpen(false);
      setMessage(translate("terms.stageCleared", language));
    } catch (error) {
      setMessage(errorMessage(error, language));
    } finally {
      setSaving(false);
    }
  }

  async function materializeAlias(alias: string) {
    if (!selected) return;
    const selectedNormalized = selected.normalized;
    setSaving(true);
    setMessage("");
    try {
      const value = await api<TermsResponse & { materialized: string }>(
        `/api/v1/projects/${project}/terms/materialize`,
        { method: "POST", body: JSON.stringify({ normalized: selectedNormalized, alias }) },
      );
      await setCurrentData(value);
      const member = value.terms.find((term) => term.normalized === value.materialized) ?? null;
      const restored = Boolean(data?.terms.find((term) => term.normalized === value.materialized)?.disabled);
      const groupPrimary = termByKey.get(selected.group_primary ?? selectedNormalized) ?? selected;
      selection.reset(member?.normalized ?? "");
      setForm(member ? restored ? formFor(member) : {
        ...formFor(member),
        category: groupPrimary.category ?? "",
        description: groupPrimary.description ?? "",
      } : emptyForm);
      setMessage(translate(restored ? "terms.materializedRestored" : "terms.materializedUnsaved", language));
    } catch (error) {
      setMessage(errorMessage(error, language));
    } finally {
      setSaving(false);
    }
  }

  async function setPrimary() {
    if (!pendingPrimary) return;
    setSaving(true);
    try {
      const value = await api<TermsResponse>(
        `/api/v1/projects/${project}/terms/set-primary`,
        { method: "POST", body: JSON.stringify({ normalized: pendingPrimary, confirm: true }) },
      );
      await setCurrentData(value);
      selection.reset(pendingPrimary);
      const primary = value.terms.find((term) => term.normalized === pendingPrimary);
      setForm(primary ? formFor(primary) : emptyForm);
      setPendingPrimary(null);
      setMessage(translate("terms.primaryChanged", language));
    } catch (error) {
      setMessage(errorMessage(error, language));
    } finally {
      setSaving(false);
    }
  }

  async function copyRelatedSource(candidate: RelatedTerm) {
    try {
      await navigator.clipboard.writeText(candidate.source);
      setMessage(translate("terms.relatedCopied", language));
    } catch (error) {
      setMessage(`${translate("terms.relatedCopyFailed", language)}: ${errorMessage(error, language)}`);
    }
  }

  function locateRelated(candidate: RelatedTerm) {
    const term = termByKey.get(candidate.normalized);
    if (!term) return;
    selection.reset(term.normalized);
    focusTerm(term);
  }

  function openRelatedGroup(candidate: RelatedTerm) {
    if (!selected) return;
    const selectedRoot = selected.group_primary ?? selected.normalized;
    const selectedSize =
      (membersByPrimary.get(selectedRoot)?.length ?? 0) + 1;
    const primary =
      candidate.group_size > 1
        ? candidate.group_root_normalized
        : selectedSize > 1
          ? selectedRoot
          : candidate.relation === "contains_selected"
            ? candidate.normalized
            : selected.normalized;
    setRelatedPrimary(primary);
    setPendingRelatedGroup(candidate);
  }

  async function groupRelated() {
    if (!selected || !pendingRelatedGroup || !relatedPrimary) return;
    setSaving(true);
    setMessage("");
    const selectedNormalized = selected.normalized;
    const candidateNormalized = pendingRelatedGroup.normalized;
    try {
      const value = await api<TermsResponse>(
        `/api/v1/projects/${project}/terms/group-related`,
        {
          method: "POST",
          body: JSON.stringify({
            normalized: selectedNormalized,
            related_normalized: candidateNormalized,
            primary_normalized: relatedPrimary,
            confirm: true,
          }),
        },
      );
      await setCurrentData(value);
      const primary = value.terms.find((term) => term.normalized === relatedPrimary) ?? null;
      selection.reset(primary?.normalized ?? selectedNormalized);
      setForm(primary ? formFor(primary) : emptyForm);
      setPendingRelatedGroup(null);
      setMessage(translate("terms.relatedGrouped", language));
    } catch (error) {
      setMessage(errorMessage(error, language));
    } finally {
      setSaving(false);
    }
  }

  async function convertRelatedToAlias() {
    if (!selected || !pendingRelatedAlias) return;
    setSaving(true);
    setMessage("");
    const selectedNormalized = selected.normalized;
    const candidateNormalized = pendingRelatedAlias.normalized;
    try {
      const value = await api<TermsResponse & { aliases_added: string[] }>(
        `/api/v1/projects/${project}/terms/convert-to-alias`,
        {
          method: "POST",
          body: JSON.stringify({
            normalized: selectedNormalized,
            related_normalized: candidateNormalized,
            confirm: true,
          }),
        },
      );
      await setCurrentData(value);
      const target = value.terms.find((term) => term.normalized === selectedNormalized) ?? null;
      selection.reset(target?.normalized ?? "");
      setForm(target ? formFor(target) : emptyForm);
      setPendingRelatedAlias(null);
      setMessage(translate("terms.relatedConverted", language, { count: value.aliases_added.length }));
    } catch (error) {
      setMessage(errorMessage(error, language));
    } finally {
      setSaving(false);
    }
  }

  async function convertGroupMemberToAlias() {
    if (!selected || !pendingGroupMemberAlias) return;
    const primaryNormalized = selected.group_primary ?? selected.normalized;
    setSaving(true);
    setMessage("");
    try {
      const value = await api<TermsResponse & { aliases_added: string[] }>(
        `/api/v1/projects/${project}/terms/convert-to-alias`,
        {
          method: "POST",
          body: JSON.stringify({
            normalized: primaryNormalized,
            related_normalized: pendingGroupMemberAlias.normalized,
            confirm: true,
          }),
        },
      );
      await setCurrentData(value);
      const primary = value.terms.find((term) => term.normalized === primaryNormalized) ?? null;
      selection.reset(primary?.normalized ?? primaryNormalized);
      setForm(primary ? formFor(primary) : emptyForm);
      setPendingGroupMemberAlias(null);
      setMessage(translate("terms.groupMemberConverted", language, { count: value.aliases_added.length }));
    } catch (error) {
      setMessage(errorMessage(error, language));
    } finally {
      setSaving(false);
    }
  }

  async function leaveGroup() {
    if (!pendingGroupMemberLeave) return;
    const normalized = pendingGroupMemberLeave.normalized;
    const focusedNormalized = selected?.normalized ?? "";
    setSaving(true);
    setMessage("");
    try {
      const value = await api<TermsResponse>(
        `/api/v1/projects/${project}/terms/leave-group`,
        {
          method: "POST",
          body: JSON.stringify({ normalized, confirm: true }),
        },
      );
      await setCurrentData(value);
      const focused = value.terms.find((term) => term.normalized === focusedNormalized) ?? null;
      selection.reset(focused?.normalized ?? focusedNormalized);
      setForm(focused ? formFor(focused) : emptyForm);
      setPendingGroupMemberLeave(null);
      setMessage(translate("terms.groupMemberLeft", language));
    } catch (error) {
      setMessage(errorMessage(error, language));
    } finally {
      setSaving(false);
    }
  }

  async function removeRelated() {
    if (!pendingRelatedRemoval) return;
    setSaving(true);
    setMessage("");
    try {
      const value = await api<TermsResponse & { removed: number }>(
        `/api/v1/projects/${project}/terms/remove`,
        {
          method: "POST",
          body: JSON.stringify({ normalized: [pendingRelatedRemoval.normalized] }),
        },
      );
      await setCurrentData(value);
      setPendingRelatedRemoval(null);
      setMessage(translate("terms.relatedRemoved", language));
    } catch (error) {
      setMessage(errorMessage(error, language));
    } finally {
      setSaving(false);
    }
  }

  if (summaryOpen) {
    return <SummaryWorkspace
      key={`summary:${projectId}`}
      project={project}
      projectId={projectId}
      overview={overview}
      language={language}
      task={task}
      onTask={onTask}
      onClose={() => { setSummaryOpen(false); openTermsSubpage(projectId, "library"); }}
    />;
  }

  if (decisionOpen) {
    return <TermDecisionWorkspace
      key={`decision:${projectId}`}
      project={project}
      projectId={projectId}
      language={language}
      task={task}
      onTask={onTask}
      onTerms={setCurrentData}
      onClose={() => { setDecisionOpen(false); openTermsSubpage(projectId, "library"); }}
      initialTab={decisionInitialTab}
      onReviewState={updateDecisionReview}
      onNavigateToEditor={openManualEditor}
    />;
  }

  return (
    <div className="terms-workspace">
      <section className="terms-browser">
        <div className="term-toolbar">
          <div className="term-primary">
            <input
              value={search}
              onChange={(event) => {
                const next = event.target.value;
                setSearch(next);
                clearSelectionIfFilteredOut(next, onlyConflicts, showDisabled);
              }}
              placeholder={translate("terms.search", language)}
            />
            <button className="quiet-button" onClick={resetFilterSelection}>{translate("terms.new", language)}</button>
          </div>
          <div className="term-secondary">
            <div className="term-filters">
              <label><input type="checkbox" checked={onlyConflicts} onChange={(event) => {
                const next = event.target.checked;
                setOnlyConflicts(next);
                clearSelectionIfFilteredOut(search, next, showDisabled);
              }} />{translate("terms.conflictsOnly", language)}</label>
              <label><input type="checkbox" checked={showDisabled} onChange={(event) => {
                const next = event.target.checked;
                setShowDisabled(next);
                clearSelectionIfFilteredOut(search, onlyConflicts, next);
              }} />{translate("terms.showRemoved", language)}</label>
            </div>
            <div className="term-stats">
              <span>revision {data?.terms_revision ?? translate("terms.revisionNone", language)}</span>
              <span>{translate("terms.conflicts", language)} {data?.conflict_count ?? 0}</span>
            </div>
          </div>
          <div className="batch-toolbar segment-batch-toolbar term-actions-toolbar">
            <span>{translate("terms.selected", language, { count: selection.selectedKeys.size })}</span>
            <div className="segment-batch-actions">
              <button className="quiet-button" onClick={() => setImportOpen(true)}>{translate("terms.import", language)}</button>
              <button className="quiet-button" onClick={() => { setExportSource("published"); setExportOpen(true); }}>{translate("terms.export", language)}</button>
              {selectedActive.length > 0 && <button
                className="danger-button"
                onClick={() => { setTermActionsOpen(false); setRemoveOpen(true); }}
              >{translate("terms.removeSelected", language)}</button>}
              <details
                className="term-actions-menu"
                open={termActionsOpen}
                onToggle={(event) => setTermActionsOpen(event.currentTarget.open)}
              >
                <summary className="quiet-button">{translate("terms.moreActions", language)}</summary>
                <div className="term-actions-popover">
                  <div className="term-actions-group">
                    <strong>{translate("terms.experiments", language)}</strong>
                    <div className="term-actions-experiment-entry">
                      <button className="quiet-button" disabled={!data?.terms_revision || Boolean(task && ["queued", "running", "cancelling"].includes(task.status))} onClick={() => { setTermActionsOpen(false); openDecision("proposals"); }}>{translate("terms.autoDecision", language)}</button>
                      <small>{translate("terms.autoDecisionDescription", language)}</small>
                    </div>
                    {!decisionDraftPending && manualReview.remaining > 0 && <button className="quiet-button term-manual-queue-button" onClick={() => { setTermActionsOpen(false); openDecision("manual"); }}>{translate("terms.manualReviewQueueProgress", language, { remaining: manualReview.remaining, total: manualReview.total })}</button>}
                    <div className="term-actions-experiment-entry">
                      <button className="quiet-button" onClick={() => { setTermActionsOpen(false); openSummary(); }}>{translate("terms.summary", language)}</button>
                      <small>{translate("terms.summaryDescription", language)}</small>
                    </div>
                  </div>
                  <button
                    className="danger-button"
                    disabled={!selectedTerms.length}
                    onClick={() => { setTermActionsOpen(false); setDeleteOpen(true); }}
                  >{translate("terms.deletePermanently", language)}</button>
                  <button
                    className="danger-button"
                    disabled={saving || !canClearStage}
                    onClick={() => { setTermActionsOpen(false); setClearOpen(true); }}
                  >{translate("terms.clearStage", language)}</button>
                </div>
              </details>
            </div>
            <small className="term-removal-help">{translate("terms.removalHelp", language)}</small>
          </div>
        </div>
        {decisionPrefetchError && <div className="inline-message error-text"><span>{translate("terms.decisionPrefetchError", language, { status: translate("terms.decisionPrefetchFailed", language), detail: decisionPrefetchError })}</span><button className="quiet-button" type="button" onClick={() => setDecisionPrefetchAttempt((value) => value + 1)}>{translate("terms.decisionRetryLoad", language)}</button></div>}
        {data?.scan.active_task_id && (
          <div className="term-scan-status">
            <div>
              <strong>{translate("terms.currentScan", language)}</strong>
              <span>{translate("terms.scanStatus", language, { done: data.scan.completed, failed: data.scan.failed, pending: data.scan.pending })}</span>
              <span>{translate("terms.scanCandidates", language, { count: data.scan.candidate_count })}</span>
              {Object.entries(data.scan.failure_counts).map(([key, count]) => <span key={key} className="scan-error-count">{key} {count}</span>)}
            </div>
            <div className="term-scan-actions">
              {data.scan.failed > 0 && <button className="quiet-button" onClick={() => setShowScanFailures((value) => !value)}>{showScanFailures ? translate("terms.hideFailures", language) : translate("terms.viewFailures", language)}</button>}
              {data.scan.candidate_count > 0 && <button className="quiet-button" onClick={() => { setExportSource("scanned"); setExportOpen(true); }}>{translate("terms.exportCurrentScan", language)}</button>}
              {data.scan.candidate_count > 0 && <button className="primary-button" onClick={() => setPartialOpen(true)}>{translate("terms.publishAvailable", language)}</button>}
            </div>
            {showScanFailures && data.scan.failed_segments.length > 0 && (
              <div className="term-scan-failures">
                {data.scan.failed_segments.map((item) => (
                  <div key={item.segment_id}>
                    <code>{item.segment_id}</code><span>{item.error_class} · {item.error_message}</span>
                  </div>
                ))}
                {data.scan.failed_segments_truncated && <small>{translate("terms.first200Failures", language)}</small>}
              </div>
            )}
          </div>
        )}
        <div className="term-list" ref={termListRef} onScroll={(event) => {
          const cached = termsCache.get(projectId);
          if (cached) cached.scrollTop = event.currentTarget.scrollTop;
        }}>
          <div className="term-row-stack" style={{ height: termVirtualizer.getTotalSize(), position: "relative" }}>
            {virtualTerms.map((virtualTerm) => {
              const term = visible[virtualTerm.index];
              if (!term) return null;
              const category = term.category || term.conflicts.categories.join(" / ");
              const categoryHasConflict = !term.category && term.conflicts.categories.length > 0;
              const selectedRow = selection.selectedKeys.has(term.normalized);
              const focused = selection.focusedKey === term.normalized;
              return (
                <button
                  key={virtualTerm.key}
                  ref={termVirtualizer.measureElement}
                  data-index={virtualTerm.index}
                  style={{
                    position: "absolute",
                    top: 0,
                    left: 0,
                    width: "100%",
                    transform: `translateY(${virtualTerm.start}px)`,
                  }}
                  className={`term-row${term.group_primary ? " term-member" : ""}${selectedRow ? " selected" : ""}${focused ? " focused" : ""}`}
                  onClick={(event) => {
                    selection.select(term.normalized, visibleKeys, event);
                    focusTerm(term);
                  }}
                >
                  <span className={term.has_conflicts ? "term-state conflict" : term.disabled ? "term-state disabled" : "term-state"} />
                  <span>
                    <strong>{term.source}</strong>
                    <span className="term-row-summary">
                      <small>{term.preferred_translation || translate("terms.noPreferredTranslation", language)}</small>
                      {category ? (
                        <small
                          className={`term-row-category${categoryHasConflict ? " conflict" : ""}`}
                          title={`${translate("terms.category", language)}: ${category}`}
                        >{category}</small>
                      ) : null}
                    </span>
                    {term.group_primary ? (
                      <small>{translate("terms.groupPrimaryBadge", language, { source: termByKey.get(term.group_primary)?.source ?? term.group_primary })}</small>
                    ) : (membersByPrimary.get(term.normalized)?.length ?? 0) > 0 ? (
                      <small>{translate("terms.groupCountBadge", language, { count: membersByPrimary.get(term.normalized)?.length ?? 0 })}</small>
                    ) : null}
                  </span>
                  <em>{term.has_conflicts ? translate("terms.conflict", language) : term.disabled ? translate("terms.removed", language) : translate("terms.active", language)}</em>
                </button>
              );
            })}
          </div>
          {termsQuery.error && <div className="empty error-text">{errorMessage(termsQuery.error, language)}</div>}
          {!termsQuery.error && data && !visible.length && <div className="empty">{translate("terms.noMatch", language)}</div>}
          {!termsQuery.error && !data && <div className="empty">{translate("terms.loading", language)}</div>}
        </div>
      </section>
      <section className="term-editor">
        {focusedManual && <div className="manual-review-editor-bar">
          <div><strong>{translate("terms.decisionManualEditorTitle", language)}</strong><span>{translate("terms.decisionManualProgress", language, { remaining: manualReview.remaining, total: manualReview.total, resolved: manualReview.resolved })}</span></div>
          <div className="manual-review-editor-actions">
            <button className="quiet-button" onClick={() => openDecision("manual")}>{translate("terms.decisionBackToQueue", language)}</button>
            <button className="quiet-button" disabled={openManualItems.length < 2} onClick={() => navigateManual(-1)}>‹</button>
            <button className="quiet-button" disabled={openManualItems.length < 2} onClick={() => navigateManual(1)}>›</button>
            <button className="primary-button" onClick={() => void setManualResolved(focusedManual, !focusedManual.resolved)}>{focusedManual.resolved ? translate("terms.decisionRestoreManual", language) : translate("terms.decisionMarkHandled", language)}</button>
          </div>
          {!data?.terms.some((term) => term.normalized === focusedManual.normalized) && <small className="error-text">{translate("terms.decisionManualTermMissing", language)}</small>}
        </div>}
        <div className="page-heading">
          <div><h1>{selected ? translate("terms.editTitle", language) : translate("terms.newTitle", language)}</h1><p>{translate("terms.saveRevisionHint", language)}</p></div>
        </div>
        {selected && (
          <div className="term-tabs">
            <button className={editorTab === "edit" ? "active" : ""} onClick={() => setEditorTab("edit")}>{translate("terms.tabEdit", language)}</button>
            <button className={editorTab === "group" ? "active" : ""} onClick={() => setEditorTab("group")}>{translate("terms.tabGroup", language)}</button>
            <button className={editorTab === "hits" ? "active" : ""} onClick={() => setEditorTab("hits")}>{translate("terms.tabHits", language, { count: hits ? hits.total : "…" })}</button>
          </div>
        )}
        {selected && editorTab === "hits" ? (
          <div className="term-tab-panel term-hits-panel">
            {hitsLoading && !hits ? (
              <div className="term-hits-state">{translate("terms.hitsLoading", language)}</div>
            ) : hitsError ? (
              <div className="term-hits-state error-text">{hitsError}</div>
            ) : hits && hits.total === 0 ? (
              <div className="term-hits-state">{translate("terms.hitsEmpty", language)}</div>
            ) : hits && (
              <>
                <div className="term-hits-list">
                  {hits.hits.map((item) => (
                    <button
                      key={item.segment_id}
                      className="term-hit-row"
                      title={translate("terms.hitsJump", language)}
                      onClick={() => onFindSegment(selected.source, item.segment_id)}
                    >
                      <code>{item.segment_id}</code>
                      <span>{item.source}</span>
                    </button>
                  ))}
                </div>
                {hitsQuery.hasNextPage && (
                  <button className="quiet-button term-hits-more" disabled={hitsQuery.isFetchingNextPage} onClick={loadMoreHits}>{translate("terms.hitsLoadMore", language)}</button>
                )}
              </>
            )}
          </div>
        ) : selected && editorTab === "group" ? (
          <div className="term-tab-panel term-group-panel">
            {(() => {
              const primaryKey = selected.group_primary ?? selected.normalized;
              const primary = termByKey.get(primaryKey) ?? selected;
              const members = membersByPrimary.get(primaryKey) ?? [];
              const grouped = members.length > 0 || selected.group_primary !== null || selected.conflicts.group_claims.length > 0;
              return (
                <>
                  {grouped ? (
                    <>
                      <div className="term-group-row primary">
                        <button className="link-button" onClick={() => { selection.reset(primary.normalized); focusTerm(primary); }}>{primary.source}</button>
                        <span>{primary.preferred_translation || translate("terms.noPreferredTranslation", language)}</span>
                        <em>{translate("terms.groupPrimary", language)}</em>
                      </div>
                      {members.map((member) => (
                        <div className="term-group-row" key={member.normalized}>
                          <button className="link-button" onClick={() => { selection.reset(member.normalized); focusTerm(member); }}>{member.source}</button>
                          <span>{member.preferred_translation || translate("terms.noPreferredTranslation", language)}</span>
                          <div className="term-group-actions">
                            <button className="quiet-button" disabled={member.disabled || saving} onClick={() => setPendingPrimary(member.normalized)}>{translate("terms.setPrimary", language)}</button>
                            <button className="danger-button" disabled={member.disabled || saving} onClick={() => setPendingGroupMemberAlias(member)}>{translate("terms.relatedConvert", language)}</button>
                            <button className="danger-button" disabled={member.disabled || saving} onClick={() => setPendingGroupMemberLeave(member)}>{translate("terms.leaveGroup", language)}</button>
                          </div>
                        </div>
                      ))}
                      {!!selected.conflicts.group_claims.length && (
                        <div className="conflict-box">
                          <strong>{translate("terms.groupClaims", language)}</strong>
                          {selected.conflicts.group_claims.map((claim) => (
                            <p key={`${claim.entry}-${claim.claimed_by}-${claim.alias}`}>{claim.alias} · {claim.claimed_by} → {claim.entry} · {claim.reason}</p>
                          ))}
                          <button className="quiet-button" onClick={() => setPendingPrimary(selected.normalized)}>{translate("terms.resolveAsPrimary", language)}</button>
                        </div>
                      )}
                    </>
                  ) : (
                    <div className="term-hits-state">{translate("terms.groupEmpty", language)}</div>
                  )}
                  <div className="term-related-panel">
                    <strong>{translate("terms.relatedTitle", language)}</strong>
                    <p className="term-related-help">{translate("terms.relatedHelp", language)}</p>
                    {relatedLoading && <div className="term-hits-state">{translate("terms.relatedLoading", language)}</div>}
                    {relatedError && <div className="term-hits-state error-text">{relatedError}</div>}
                    {!relatedLoading && !relatedError && related && !related.related.length && (
                      <div className="term-hits-state">{translate("terms.relatedEmpty", language)}</div>
                    )}
                    {!!related?.related.length && (
                      <div className="term-related-list">
                        {related.related.map((candidate) => (
                          <div className="term-related-row" key={candidate.normalized}>
                            <div className="term-related-main">
                              <strong>{candidate.source}</strong>
                              <span>{candidate.preferred_translation || translate("terms.noPreferredTranslation", language)}</span>
                              <small>
                                {translate(
                                  candidate.relation === "contains_selected" ? "terms.relatedContains" : "terms.relatedContainedBy",
                                  language,
                                  { value: candidate.selected_match },
                                )}
                                {" · "}
                                {translate(
                                  candidate.related_match_type === "alias" ? "terms.relatedAliasMatch" : "terms.relatedSourceMatch",
                                  language,
                                  { value: candidate.related_match },
                                )}
                              </small>
                              {candidate.group_size > 1 && (
                                <small>
                                  {translate("terms.relatedGroupStatus", language, {
                                    source: candidate.group_root_source,
                                    count: candidate.group_size,
                                  })}
                                </small>
                              )}
                            </div>
                            <div className="term-related-actions">
                              <button className="quiet-button" disabled={saving} onClick={() => void copyRelatedSource(candidate)}>{translate("terms.relatedCopy", language)}</button>
                              <button className="quiet-button" disabled={saving} onClick={() => locateRelated(candidate)}>{translate("terms.relatedLocate", language)}</button>
                              {candidate.can_group && <button className="quiet-button" disabled={saving} onClick={() => openRelatedGroup(candidate)}>{translate("terms.relatedGroup", language)}</button>}
                              {candidate.can_convert_alias && <button className="danger-button" disabled={saving} onClick={() => setPendingRelatedAlias(candidate)}>{translate("terms.relatedConvert", language)}</button>}
                              {candidate.can_remove && <button className="danger-button" disabled={saving} onClick={() => setPendingRelatedRemoval(candidate)}>{translate("terms.relatedRemove", language)}</button>}
                            </div>
                            {candidate.blocked_reason && <small className="term-related-blocked">{translate(`terms.relatedBlocked.${candidate.blocked_reason}`, language)}</small>}
                          </div>
                        ))}
                      </div>
                    )}
                  </div>
                </>
              );
            })()}
          </div>
        ) : (
          <div className="term-tab-panel term-edit-panel">
            <label>{translate("terms.sourceTerm", language)}<input value={form.source} disabled={selected?.disabled} onChange={(event) => setForm({ ...form, source: event.target.value })} /></label>
            <label>{translate("terms.preferredTranslation", language)}<input value={form.preferredTranslation} disabled={selected?.disabled} onChange={(event) => setForm({ ...form, preferredTranslation: event.target.value })} /></label>
            {!!selected?.conflicts.preferred_translations.length && (
              <ConflictChoices
                label={translate("terms.conflictTranslations", language)}
                values={selected.conflicts.preferred_translations}
                onChoose={(value) => setForm({ ...form, preferredTranslation: value })}
              />
            )}
            <label>{translate("terms.category", language)}<input value={form.category} disabled={selected?.disabled} onChange={(event) => setForm({ ...form, category: event.target.value })} /></label>
            {!!selected?.conflicts.categories.length && (
              <ConflictChoices
                label={translate("terms.conflictCategories", language)}
                values={selected.conflicts.categories}
                onChoose={(value) => setForm({ ...form, category: value })}
              />
            )}
            {!!selected?.conflicts.alias_primaries.length && (
              <div className="conflict-box">
                <strong>{translate("terms.aliasPrimaryConflict", language)}</strong>
                {selected.conflicts.alias_primaries.map((item) => (
                  <p key={`${item.alias}-${item.primary_source}`}>
                    {item.alias} → {item.primary_source}
                  </p>
                ))}
              </div>
            )}
            <label>{translate("terms.description", language)}<textarea value={form.description} disabled={selected?.disabled} onChange={(event) => setForm({ ...form, description: event.target.value })} /></label>
            <div className="term-alias-editor">
              <strong>{translate("terms.aliases", language)}</strong>
              {form.aliases.map((alias, index) => (
                <div className="term-alias-row" key={index}>
                  <input value={alias} disabled={selected?.disabled} onChange={(event) => setForm({ ...form, aliases: form.aliases.map((value, aliasIndex) => aliasIndex === index ? event.target.value : value) })} />
                  {selected && <button className="quiet-button" disabled={selected.disabled || saving || !alias.trim()} onClick={() => materializeAlias(alias)}>{translate("terms.materialize", language)}</button>}
                  <button className="quiet-button" disabled={selected?.disabled} aria-label={translate("terms.removeAlias", language)} onClick={() => setForm({ ...form, aliases: form.aliases.filter((_, aliasIndex) => aliasIndex !== index) })}>×</button>
                </div>
              ))}
              <button className="quiet-button" disabled={selected?.disabled} onClick={() => setForm({ ...form, aliases: [...form.aliases, ""] })}>{translate("terms.addAlias", language)}</button>
            </div>
            {message && <p className={message.startsWith("Error") ? "error-text" : "success-text"}>{message}</p>}
            <div className="editor-actions term-actions">
              {selected?.disabled ? (
                <button className="primary-button" disabled={saving} onClick={() => save(false)}>{translate("terms.restore", language)}</button>
              ) : (
                <>
                  <button className="primary-button" disabled={saving || !form.source.trim()} onClick={() => save(false)}>{translate("common.save", language)}</button>
                  {selected && <button className="danger-button" disabled={saving} onClick={() => save(true)}>{translate("common.remove", language)}</button>}
                </>
              )}
            </div>
          </div>
        )}
      </section>
      {removeOpen && (
        <ConfirmDialog
          language={language}
          title={translate("terms.removeTitle", language)}
          text={translate("terms.removeText", language, { count: selectedActive.length })}
          confirming={saving}
          onCancel={() => setRemoveOpen(false)}
          onConfirm={removeSelected}
        />
      )}
      {deleteOpen && (
        <ConfirmDialog
          language={language}
          title={translate("terms.deleteTitle", language)}
          text={translate("terms.deleteText", language, { count: selectedTerms.length })}
          confirmLabel={translate("terms.confirmDelete", language)}
          confirming={saving}
          onCancel={() => setDeleteOpen(false)}
          onConfirm={deleteSelected}
        />
      )}
      {clearOpen && (
        <ConfirmDialog
          language={language}
          title={translate("terms.clearStageTitle", language)}
          text={translate("terms.clearStageText", language)}
          confirmLabel={translate("terms.clearStageConfirm", language)}
          confirming={saving}
          onCancel={() => setClearOpen(false)}
          onConfirm={clearTerms}
        />
      )}
      {pendingPrimary && (
        <ConfirmDialog
          language={language}
          title={translate("terms.setPrimaryTitle", language)}
          text={translate("terms.setPrimaryText", language, { source: termByKey.get(pendingPrimary)?.source ?? pendingPrimary })}
          confirmLabel={translate("terms.setPrimary", language)}
          confirming={saving}
          onCancel={() => setPendingPrimary(null)}
          onConfirm={setPrimary}
        />
      )}
      {pendingRelatedGroup && selected && (
        <RelatedGroupDialog
          language={language}
          candidate={pendingRelatedGroup}
          primary={relatedPrimary}
          selectedRoot={selected.group_primary ?? selected.normalized}
          selectedRootSource={termByKey.get(selected.group_primary ?? selected.normalized)?.source ?? selected.source}
          confirming={saving}
          onPrimaryChange={setRelatedPrimary}
          onCancel={() => setPendingRelatedGroup(null)}
          onConfirm={groupRelated}
        />
      )}
      {pendingRelatedAlias && selected && (
        <ConfirmDialog
          language={language}
          title={translate("terms.relatedConvertTitle", language)}
          text={translate("terms.relatedConvertText", language, {
            source: pendingRelatedAlias.source,
            aliases: termByKey.get(pendingRelatedAlias.normalized)?.aliases.join("、") || translate("terms.relatedNoAliases", language),
          })}
          confirmLabel={translate("terms.relatedConvert", language)}
          confirming={saving}
          onCancel={() => setPendingRelatedAlias(null)}
          onConfirm={convertRelatedToAlias}
        />
      )}
      {pendingGroupMemberAlias && selected && (
        <ConfirmDialog
          language={language}
          title={translate("terms.groupMemberAliasTitle", language)}
          text={translate("terms.groupMemberAliasText", language, {
            source: pendingGroupMemberAlias.source,
            aliases: pendingGroupMemberAlias.aliases.join("、") || translate("terms.relatedNoAliases", language),
          })}
          confirmLabel={translate("terms.relatedConvert", language)}
          confirming={saving}
          onCancel={() => setPendingGroupMemberAlias(null)}
          onConfirm={convertGroupMemberToAlias}
        />
      )}
      {pendingGroupMemberLeave && (
        <ConfirmDialog
          language={language}
          title={translate("terms.leaveGroupTitle", language)}
          text={translate("terms.leaveGroupText", language, {
            source: pendingGroupMemberLeave.source,
          })}
          confirmLabel={translate("terms.leaveGroup", language)}
          confirming={saving}
          onCancel={() => setPendingGroupMemberLeave(null)}
          onConfirm={leaveGroup}
        />
      )}
      {pendingRelatedRemoval && (
        <ConfirmDialog
          language={language}
          title={translate("terms.relatedRemoveTitle", language)}
          text={translate("terms.relatedRemoveText", language, { source: pendingRelatedRemoval.source })}
          confirmLabel={translate("terms.relatedRemove", language)}
          confirming={saving}
          onCancel={() => setPendingRelatedRemoval(null)}
          onConfirm={removeRelated}
        />
      )}
      {importOpen && (
        <TermImportDialog
          project={project}
          language={language}
          onClose={() => setImportOpen(false)}
          onImported={async (value) => {
            await setCurrentData(value);
            selection.reset();
            setForm(emptyForm);
            setImportOpen(false);
            setMessage(translate("terms.imported", language));
          }}
        />
      )}
      {exportOpen && (
        <TermExportDialog
          project={project}
          language={language}
          hasScanned={Boolean(data?.scan.candidate_count)}
          defaultSource={exportSource}
          onClose={() => setExportOpen(false)}
        />
      )}
      {partialOpen && (
        <PartialPublishDialog
          project={project}
          language={language}
          count={data?.scan.candidate_count ?? 0}
          onClose={() => setPartialOpen(false)}
          onPublished={async () => {
            setPartialOpen(false);
            await setCurrentData(await api<TermsResponse>(`/api/v1/projects/${project}/terms`));
            setMessage(translate("terms.published", language));
          }}
        />
      )}
    </div>
  );
}

function ConflictChoices({
  label,
  values,
  onChoose,
}: {
  label: string;
  values: string[];
  onChoose: (value: string) => void;
}) {
  return (
    <div className="conflict-box">
      <strong>{label}</strong>
      <div className="choice-buttons">
        {values.map((value) => <button key={value} type="button" onClick={() => onChoose(value)}>{value}</button>)}
      </div>
    </div>
  );
}
