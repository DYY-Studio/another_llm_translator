import { useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import { useVirtualizer } from "@tanstack/react-virtual";
import { api } from "../api";
import { translate, translateError, type Language } from "../i18n";
import type { RelatedTerm, RelatedTermsResponse, Term, TermHitsResponse, TermsResponse } from "../types";
import { useClassicSelection } from "../useClassicSelection";
import { Modal } from "./Modal";

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
  data: TermsResponse;
  search: string;
  onlyConflicts: boolean;
  showDisabled: boolean;
  focusedKey: string;
  scrollTop: number;
}

// Survives tab switches so returning renders the term list instantly. Keyed
// by project; cleared when the project changes so cached data never leaks
// across projects.
const termsCache = new Map<string, TermsCacheEntry>();
const termsProjectRef = { current: "" };

// Warms the cache when a project is opened so the first visit to the
// terminology page renders instantly. Best-effort: failures are left to the
// view, which fetches and surfaces them on visit; the write guard keeps a
// mounted view's fresher entry (with its filters and scroll state) intact.
export function prefetchTerms(project: string) {
  if (termsCache.has(project)) return;
  void api<TermsResponse>(`/api/v1/projects/${project}/terms`)
    .then((data) => {
      if (termsCache.has(project)) return;
      termsCache.set(project, {
        data,
        search: "",
        onlyConflicts: false,
        showDisabled: false,
        focusedKey: "",
        scrollTop: 0,
      });
    })
    .catch(() => {});
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
  focusFailures = false,
  language,
  onFindSegment,
}: {
  project: string;
  focusFailures?: boolean;
  language: Language;
  onFindSegment: (source: string, segmentId: string) => void;
}) {
  const [data, setData] = useState<TermsResponse | null>(null);
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
  const [exportSource, setExportSource] = useState<"published" | "scanned">("published");
  const [partialOpen, setPartialOpen] = useState(false);
  const [showScanFailures, setShowScanFailures] = useState(false);
  const [editorTab, setEditorTab] = useState<"edit" | "group" | "hits">("edit");
  const [pendingPrimary, setPendingPrimary] = useState<string | null>(null);
  const [hits, setHits] = useState<TermHitsResponse | null>(null);
  const [hitsLoading, setHitsLoading] = useState(false);
  const [hitsError, setHitsError] = useState("");
  const hitsRequestRef = useRef(0);
  const [related, setRelated] = useState<RelatedTermsResponse | null>(null);
  const [relatedLoading, setRelatedLoading] = useState(false);
  const [relatedError, setRelatedError] = useState("");
  const relatedRequestRef = useRef(0);
  const relatedCacheRef = useRef(new Map<string, RelatedTermsResponse>());
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

  // Restore a cached view synchronously during render so the browser never
  // paints an empty frame. This runs on the first mount too: prefetchTerms
  // warms the cache when the project is opened, so entering the terminology
  // page renders instantly. Switching projects drops every entry except the
  // current project's (including its prefetched entry), so cached data never
  // leaks across projects; the load effect refreshes in the background.
  if (termsProjectRef.current !== project) {
    termsProjectRef.current = project;
    for (const key of [...termsCache.keys()]) {
      if (key !== project) termsCache.delete(key);
    }
    setData(null);
    selection.reset();
    termsRestoredRef.current = false;
  }
  if (!termsRestoredRef.current) {
    termsRestoredRef.current = true;
    const cached = termsCache.get(project);
    if (cached) {
      setData(cached.data);
      setSearch(cached.search);
      setOnlyConflicts(cached.onlyConflicts);
      setShowDisabled(cached.showDisabled);
      selection.reset(cached.focusedKey);
      restoredScrollRef.current = cached.scrollTop;
    } else {
      setData(null);
      selection.reset();
    }
  }

  useEffect(() => {
    setForm(emptyForm);
    setMessage("");
    void api<TermsResponse>(`/api/v1/projects/${project}/terms`)
      .then(setData)
      .catch((error) => setMessage(String(error)));
  }, [project]);

  useEffect(() => {
    if (!data) return;
    termsCache.set(project, {
      data,
      search,
      onlyConflicts,
      showDisabled,
      focusedKey: selection.focusedKey,
      scrollTop: termListRef.current?.scrollTop ?? 0,
    });
  }, [project, data, search, onlyConflicts, showDisabled, selection.focusedKey]);

  useLayoutEffect(() => {
    if (restoredScrollRef.current === null) return;
    if (termListRef.current) termListRef.current.scrollTop = restoredScrollRef.current;
    restoredScrollRef.current = null;
  });

  useEffect(() => {
    if (focusFailures) setShowScanFailures(true);
  }, [focusFailures]);

  const hitsPageSize = 50;
  function hitsUrl(normalized: string, offset: number) {
    const params = new URLSearchParams({
      normalized,
      offset: String(offset),
      limit: String(hitsPageSize),
    });
    return `/api/v1/projects/${project}/terms/hits?${params}`;
  }

  // Hits are intentionally loaded only when the user opens the hits tab. A
  // normal term selection must not scan every Segment in the project.
  useEffect(() => {
    const normalized = selected?.normalized ?? "";
    const requestId = ++hitsRequestRef.current;
    if (editorTab !== "hits" || !normalized || selectedIsDisabled) {
      setHits(
        editorTab === "hits" && normalized && selectedIsDisabled
          ? { normalized, source: selected?.source ?? normalized, total: 0, offset: 0, limit: hitsPageSize, hits: [] }
          : null,
      );
      setHitsLoading(false);
      setHitsError("");
      return;
    }
    setHitsLoading(true);
    setHitsError("");
    setHits(null);
    void api<TermHitsResponse>(hitsUrl(normalized, 0))
      .then((value) => {
        if (requestId === hitsRequestRef.current) setHits(value);
      })
      .catch((error) => {
        if (requestId === hitsRequestRef.current) setHitsError(String(error));
      })
      .finally(() => {
        if (requestId === hitsRequestRef.current) setHitsLoading(false);
      });
  }, [editorTab, project, selectedIsDisabled, selectedMatchKey]);

  // Related terms are cheap to compute but only useful on the group tab. Keep
  // a small revision-aware cache so switching between tabs does not repeat
  // the same library scan, while a save/removal naturally invalidates it.
  useEffect(() => {
    const normalized = selected?.normalized ?? "";
    const requestId = ++relatedRequestRef.current;
    if (editorTab !== "group" || !normalized || selectedIsDisabled) {
      setRelated(null);
      setRelatedLoading(false);
      setRelatedError("");
      return;
    }
    const cacheKey = `${project}:${data?.terms_revision ?? "none"}:${selectedMatchKey}`;
    const cached = relatedCacheRef.current.get(cacheKey);
    if (cached) {
      setRelated(cached);
      setRelatedLoading(false);
      setRelatedError("");
      return;
    }
    setRelated(null);
    setRelatedLoading(true);
    setRelatedError("");
    const params = new URLSearchParams({ normalized, limit: "20" });
    void api<RelatedTermsResponse>(`/api/v1/projects/${project}/terms/related?${params}`)
      .then((value) => {
        if (requestId !== relatedRequestRef.current) return;
        if (relatedCacheRef.current.size >= 50) relatedCacheRef.current.clear();
        relatedCacheRef.current.set(cacheKey, value);
        setRelated(value);
      })
      .catch((error) => {
        if (requestId === relatedRequestRef.current) setRelatedError(String(error));
      })
      .finally(() => {
        if (requestId === relatedRequestRef.current) setRelatedLoading(false);
      });
  }, [data?.terms_revision, editorTab, project, selectedIsDisabled, selectedMatchKey]);

  function loadMoreHits() {
    if (!selected || !hits) return;
    const normalized = selected.normalized;
    const offset = hits.hits.length;
    setHitsLoading(true);
    void api<TermHitsResponse>(hitsUrl(normalized, offset))
      .then((value) => setHits((current) => (
        current && current.normalized === normalized
          ? { ...value, hits: [...current.hits, ...value.hits] }
          : current
      )))
      .catch((error) => setHitsError(String(error)))
      .finally(() => setHitsLoading(false));
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
      setData(value);
      selection.reset(saved?.normalized ?? "");
      setForm(saved ? formFor(saved) : emptyForm);
      setMessage(disabled ? translate("terms.termRemoved", language) : selected?.disabled ? translate("terms.termRestored", language) : translate("terms.termSaved", language));
    } catch (error) {
      setMessage(String(error));
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
      setData(value);
      selection.reset();
      setForm(emptyForm);
      setMessage(translate("terms.removedCount", language, { count: value.removed }));
      setRemoveOpen(false);
    } catch (error) {
      setMessage(String(error));
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
      setData(value);
      selection.reset();
      setForm(emptyForm);
      setMessage(translate("terms.deletedCount", language, { count: value.deleted }));
      setDeleteOpen(false);
    } catch (error) {
      setMessage(String(error));
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
      setData(value);
      selection.reset();
      setForm(emptyForm);
      setHits(null);
      setHitsLoading(false);
      setHitsError("");
      setRelated(null);
      setRelatedLoading(false);
      setRelatedError("");
      setPendingPrimary(null);
      setPendingRelatedGroup(null);
      setPendingRelatedAlias(null);
      setPendingGroupMemberAlias(null);
      setPendingGroupMemberLeave(null);
      setPendingRelatedRemoval(null);
      setRelatedPrimary("");
      setShowScanFailures(false);
      setPartialOpen(false);
      hitsRequestRef.current += 1;
      relatedRequestRef.current += 1;
      relatedCacheRef.current.clear();
      setClearOpen(false);
      setMessage(translate("terms.stageCleared", language));
    } catch (error) {
      setMessage(String(error));
    } finally {
      setSaving(false);
    }
  }

  async function materializeAlias(alias: string) {
    if (!selected) return;
    setSaving(true);
    setMessage("");
    try {
      const value = await api<TermsResponse & { materialized: string }>(
        `/api/v1/projects/${project}/terms/materialize`,
        { method: "POST", body: JSON.stringify({ normalized: selected.normalized, alias }) },
      );
      setData(value);
      const member = value.terms.find((term) => term.normalized === value.materialized) ?? null;
      const groupPrimary = termByKey.get(selected.group_primary ?? selected.normalized) ?? selected;
      selection.reset(member?.normalized ?? "");
      setForm(member ? {
        ...formFor(member),
        category: groupPrimary.category ?? "",
        description: groupPrimary.description ?? "",
      } : emptyForm);
      setMessage(translate("terms.materializedUnsaved", language));
    } catch (error) {
      setMessage(String(error));
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
      setData(value);
      selection.reset(pendingPrimary);
      const primary = value.terms.find((term) => term.normalized === pendingPrimary);
      setForm(primary ? formFor(primary) : emptyForm);
      setPendingPrimary(null);
      setMessage(translate("terms.primaryChanged", language));
    } catch (error) {
      setMessage(String(error));
    } finally {
      setSaving(false);
    }
  }

  async function copyRelatedSource(candidate: RelatedTerm) {
    try {
      await navigator.clipboard.writeText(candidate.source);
      setMessage(translate("terms.relatedCopied", language));
    } catch (error) {
      setMessage(`${translate("terms.relatedCopyFailed", language)}: ${String(error)}`);
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
      setData(value);
      const primary = value.terms.find((term) => term.normalized === relatedPrimary) ?? null;
      selection.reset(primary?.normalized ?? selectedNormalized);
      setForm(primary ? formFor(primary) : emptyForm);
      setPendingRelatedGroup(null);
      setMessage(translate("terms.relatedGrouped", language));
    } catch (error) {
      setMessage(String(error));
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
      setData(value);
      const target = value.terms.find((term) => term.normalized === selectedNormalized) ?? null;
      selection.reset(target?.normalized ?? "");
      setForm(target ? formFor(target) : emptyForm);
      setPendingRelatedAlias(null);
      setMessage(translate("terms.relatedConverted", language, { count: value.aliases_added.length }));
    } catch (error) {
      setMessage(String(error));
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
      setData(value);
      const primary = value.terms.find((term) => term.normalized === primaryNormalized) ?? null;
      selection.reset(primary?.normalized ?? primaryNormalized);
      setForm(primary ? formFor(primary) : emptyForm);
      setPendingGroupMemberAlias(null);
      setMessage(translate("terms.groupMemberConverted", language, { count: value.aliases_added.length }));
    } catch (error) {
      setMessage(String(error));
    } finally {
      setSaving(false);
    }
  }

  async function leaveGroup() {
    if (!pendingGroupMemberLeave) return;
    const normalized = pendingGroupMemberLeave.normalized;
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
      setData(value);
      const left = value.terms.find((term) => term.normalized === normalized) ?? null;
      selection.reset(left?.normalized ?? "");
      setForm(left ? formFor(left) : emptyForm);
      setPendingGroupMemberLeave(null);
      setMessage(translate("terms.groupMemberLeft", language));
    } catch (error) {
      setMessage(String(error));
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
      setData(value);
      setPendingRelatedRemoval(null);
      setMessage(translate("terms.relatedRemoved", language));
    } catch (error) {
      setMessage(String(error));
    } finally {
      setSaving(false);
    }
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
          <div className="batch-toolbar segment-batch-toolbar">
            <span>{translate("terms.selected", language, { count: selection.selectedKeys.size })}</span>
            <div className="segment-batch-actions">
              <button className="quiet-button" onClick={() => setImportOpen(true)}>{translate("terms.import", language)}</button>
              <button className="quiet-button" onClick={() => { setExportSource("published"); setExportOpen(true); }}>{translate("terms.export", language)}</button>
              <button
                className="danger-button"
                disabled={!selectedActive.length}
                onClick={() => setRemoveOpen(true)}
              >{translate("terms.removeSelected", language)}</button>
              <button
                className="danger-button"
                disabled={!selectedTerms.length}
                onClick={() => setDeleteOpen(true)}
              >{translate("terms.deletePermanently", language)}</button>
              <button
                className="danger-button"
                disabled={saving || !canClearStage}
                onClick={() => setClearOpen(true)}
              >{translate("terms.clearStage", language)}</button>
            </div>
            <small className="term-removal-help">{translate("terms.removalHelp", language)}</small>
          </div>
        </div>
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
          const cached = termsCache.get(project);
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
          {data && !visible.length && <div className="empty">{translate("terms.noMatch", language)}</div>}
          {!data && <div className="empty">{translate("terms.loading", language)}</div>}
        </div>
      </section>
      <section className="term-editor">
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
                {hits.hits.length < hits.total && (
                  <button className="quiet-button term-hits-more" disabled={hitsLoading} onClick={loadMoreHits}>{translate("terms.hitsLoadMore", language)}</button>
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
          selected={selected}
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
          onImported={(value) => {
            setData(value);
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
            setData(await api<TermsResponse>(`/api/v1/projects/${project}/terms`));
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

function ConfirmDialog({
  title,
  text,
  confirmLabel,
  language,
  confirming,
  onCancel,
  onConfirm,
}: {
  title: string;
  text: string;
  confirmLabel?: string;
  language: Language;
  confirming: boolean;
  onCancel: () => void;
  onConfirm: () => void;
}) {
  const effectiveConfirmLabel = confirmLabel ?? translate("terms.confirmRemoval", language);
  return (
    <Modal ariaLabel={title}>
      <h2>{title}</h2>
      <p>{text}</p>
      <div className="modal-actions">
        <button className="quiet-button" disabled={confirming} onClick={onCancel}>{translate("common.cancel", language)}</button>
        <button className="danger-button" disabled={confirming} onClick={onConfirm}>{effectiveConfirmLabel}</button>
      </div>
    </Modal>
  );
}

function RelatedGroupDialog({
  language,
  selected,
  candidate,
  primary,
  selectedRoot,
  selectedRootSource,
  confirming,
  onPrimaryChange,
  onCancel,
  onConfirm,
}: {
  language: Language;
  selected: Term;
  candidate: RelatedTerm;
  primary: string;
  selectedRoot: string;
  selectedRootSource: string;
  confirming: boolean;
  onPrimaryChange: (value: string) => void;
  onCancel: () => void;
  onConfirm: () => void;
}) {
  const options = [
    {
      normalized: selectedRoot,
      source: selectedRootSource,
    },
    {
      normalized: candidate.group_root_normalized,
      source: candidate.group_root_source,
    },
  ].filter(
    (option, index, all) =>
      all.findIndex((item) => item.normalized === option.normalized) === index,
  );
  return (
    <Modal ariaLabel={translate("terms.relatedGroupTitle", language)}>
      <h2>{translate("terms.relatedGroupTitle", language)}</h2>
      <p>{translate("terms.relatedGroupText", language, { source: candidate.source })}</p>
      <div className="related-primary-options">
        {options.map((option) => (
          <label key={option.normalized} className="radio-option">
            <input
              type="radio"
              name="related-primary"
              checked={primary === option.normalized}
              onChange={() => onPrimaryChange(option.normalized)}
            />
            <span>{option.source}</span>
          </label>
        ))}
      </div>
      <div className="modal-actions">
        <button className="quiet-button" disabled={confirming} onClick={onCancel}>{translate("common.cancel", language)}</button>
        <button className="primary-button" disabled={confirming} onClick={onConfirm}>{translate("terms.relatedGroup", language)}</button>
      </div>
    </Modal>
  );
}

function TermImportDialog({
  project,
  language,
  onClose,
  onImported,
}: {
  project: string;
  language: Language;
  onClose: () => void;
  onImported: (value: TermsResponse) => void;
}) {
  const [file, setFile] = useState<File | null>(null);
  const [error, setError] = useState("");
  const [saving, setSaving] = useState(false);

  async function submit() {
    if (!file) return;
    const body = new FormData();
    body.append("file", file);
    setSaving(true);
    try {
      await api(`/api/v1/projects/${project}/terms/import`, {
        method: "POST",
        body,
      });
      onImported(await api<TermsResponse>(`/api/v1/projects/${project}/terms`));
    } catch (value) {
      setError(String(value));
    } finally {
      setSaving(false);
    }
  }

  return (
    <Modal ariaLabel={translate("terms.importDialogTitle", language)}>
      <h2>{translate("terms.importDialogTitle", language)}</h2>
      <p>{translate("terms.importHint", language)}</p>
      <label>{translate("terms.termFile", language)}<input type="file" accept=".json,.csv" onChange={(event) => setFile(event.target.files?.[0] ?? null)} /></label>
      {error && <p className="error-text">{error}</p>}
      <div className="modal-actions">
        <button className="quiet-button" disabled={saving} onClick={onClose}>{translate("common.cancel", language)}</button>
        <button className="primary-button" disabled={saving || !file} onClick={submit}>{translate("terms.import", language)}</button>
      </div>
    </Modal>
  );
}

function TermExportDialog({
  project,
  language,
  hasScanned,
  defaultSource,
  onClose,
}: {
  project: string;
  language: Language;
  hasScanned: boolean;
  defaultSource: "published" | "scanned";
  onClose: () => void;
}) {
  const [format, setFormat] = useState<"json" | "csv">("json");
  const [source, setSource] = useState<"published" | "scanned">(defaultSource);
  const [includeDisabled, setIncludeDisabled] = useState(false);
  const [error, setError] = useState("");

  async function download() {
    try {
      const response = await fetch(
        `/api/v1/projects/${project}/terms/export?format=${format}&include_disabled=${includeDisabled}&source=${source}`,
      );
      if (!response.ok) {
        const value = await response.json().catch(() => null);
        const code: unknown = value?.code;
        const localized = typeof code === "string"
          ? translateError(code, value?.params ?? {})
          : null;
        throw new Error(localized || value?.error || translate("export.requestFailedStatus", language, { status: response.status }));
      }
      const url = URL.createObjectURL(await response.blob());
      const link = document.createElement("a");
      link.href = url;
      link.download = `${project}-terms.${format}`;
      link.click();
      URL.revokeObjectURL(url);
      onClose();
    } catch (value) {
      setError(String(value));
    }
  }

  return (
    <Modal ariaLabel={translate("terms.exportDialogTitle", language)}>
      <h2>{translate("terms.exportDialogTitle", language)}</h2>
      <label>{translate("terms.source", language)}<select value={source} onChange={(event) => setSource(event.target.value as "published" | "scanned")}><option value="published">{translate("terms.publishedTerms", language)}</option>{hasScanned && <option value="scanned">{translate("terms.scanCandidatesOption", language)}</option>}</select></label>
      <label>{translate("terms.format", language)}<select value={format} onChange={(event) => setFormat(event.target.value as "json" | "csv")}><option value="json">JSON</option><option value="csv">CSV</option></select></label>
      <label className="check-row"><input type="checkbox" checked={includeDisabled} onChange={(event) => setIncludeDisabled(event.target.checked)} />{translate("terms.includeRemoved", language)}</label>
      {error && <p className="error-text">{error}</p>}
      <div className="modal-actions">
        <button className="quiet-button" onClick={onClose}>{translate("common.cancel", language)}</button>
        <button className="primary-button" onClick={download}>{translate("terms.download", language)}</button>
      </div>
    </Modal>
  );
}

function PartialPublishDialog({
  project,
  language,
  count,
  onClose,
  onPublished,
}: {
  project: string;
  language: Language;
  count: number;
  onClose: () => void;
  onPublished: () => Promise<void>;
}) {
  const [working, setWorking] = useState(false);
  const [error, setError] = useState("");

  async function confirm() {
    setWorking(true);
    setError("");
    try {
      await api(`/api/v1/projects/${project}/terms/publish-partial`, { method: "POST", body: JSON.stringify({ confirm: true }) });
      await onPublished();
    } catch (value) {
      setError(String(value));
    } finally {
      setWorking(false);
    }
  }

  return (
    <div className="modal-backdrop">
      <div className="modal" role="dialog" aria-modal="true" aria-label={translate("terms.publishTitle", language)}>
        <h2>{translate("terms.publishTitle", language)}</h2>
        <p>{translate("terms.publishText", language, { count })}</p>
        {error && <p className="error-text">{error}</p>}
        <div className="modal-actions">
          <button className="quiet-button" disabled={working} onClick={onClose}>{translate("common.cancel", language)}</button>
          <button className="primary-button" disabled={working || !count} onClick={confirm}>{working ? translate("terms.publishing", language) : translate("terms.publish", language)}</button>
        </div>
      </div>
    </div>
  );
}
