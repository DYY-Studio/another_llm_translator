import { useEffect, useMemo, useState } from "react";
import { api } from "../api";
import type { Language } from "../i18n";
import type { Term, TermsResponse } from "../types";
import { useClassicSelection } from "../useClassicSelection";

interface TermForm {
  source: string;
  preferredTranslation: string;
  category: string;
  description: string;
  aliases: string;
}

const emptyForm: TermForm = {
  source: "",
  preferredTranslation: "",
  category: "",
  description: "",
  aliases: "",
};

function formFor(term: Term): TermForm {
  return {
    source: term.source,
    preferredTranslation: term.preferred_translation ?? "",
    category: term.category ?? "",
    description: term.description ?? "",
    aliases: term.aliases.join("\n"),
  };
}

export function TermsView({
  project,
  focusFailures = false,
  language,
}: {
  project: string;
  focusFailures?: boolean;
  language: Language;
}) {
  const en = language === "en";
  const [data, setData] = useState<TermsResponse | null>(null);
  const [form, setForm] = useState<TermForm>(emptyForm);
  const [search, setSearch] = useState("");
  const [onlyConflicts, setOnlyConflicts] = useState(false);
  const [showDisabled, setShowDisabled] = useState(false);
  const [message, setMessage] = useState("");
  const [saving, setSaving] = useState(false);
  const [removeOpen, setRemoveOpen] = useState(false);
  const [deleteOpen, setDeleteOpen] = useState(false);
  const [importOpen, setImportOpen] = useState(false);
  const [exportOpen, setExportOpen] = useState(false);
  const [exportSource, setExportSource] = useState<"published" | "scanned">("published");
  const [partialOpen, setPartialOpen] = useState(false);
  const [showScanFailures, setShowScanFailures] = useState(false);
  const selection = useClassicSelection();
  const selected = data?.terms.find(
    (term) => term.normalized === selection.focusedKey,
  ) ?? null;

  useEffect(() => {
    setData(null);
    setForm(emptyForm);
    setMessage("");
    selection.reset();
    void api<TermsResponse>(`/api/v1/projects/${project}/terms`)
      .then(setData)
      .catch((error) => setMessage(String(error)));
  }, [project]);

  useEffect(() => {
    if (focusFailures) setShowScanFailures(true);
  }, [focusFailures]);

  const visible = useMemo(() => {
    const query = search.trim().toLocaleLowerCase();
    return (data?.terms ?? []).filter((term) => {
      const haystack = [
        term.source,
        term.preferred_translation,
        term.category,
        term.description,
        ...term.aliases,
      ].filter(Boolean).join("\n").toLocaleLowerCase();
      return (!query || haystack.includes(query))
        && (!onlyConflicts || term.has_conflicts)
        && (showDisabled || !term.disabled);
    });
  }, [data, onlyConflicts, search, showDisabled]);
  const visibleKeys = visible.map((term) => term.normalized);
  const selectedTerms = visible.filter((term) => selection.selectedKeys.has(term.normalized));
  const selectedActive = visible.filter(
    (term) => selection.selectedKeys.has(term.normalized) && !term.disabled,
  );

  function resetFilterSelection() {
    selection.reset();
    setForm(emptyForm);
    setMessage("");
  }

  function focusTerm(term: Term) {
    setForm(formFor(term));
    setMessage("");
  }

  async function save(disabled: boolean) {
    if (!form.source.trim()) {
      setMessage(en ? "Source term is required" : "术语原文不能为空");
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
          aliases: form.aliases.split("\n").map((item) => item.trim()).filter(Boolean),
          disabled,
        }),
      });
      setData(value);
      const saved = value.terms.find(
        (term) => term.source === form.source && term.disabled === disabled,
      ) ?? null;
      selection.reset(saved?.normalized ?? "");
      setForm(saved ? formFor(saved) : emptyForm);
      setMessage(disabled ? (en ? "Term removed" : "术语已移除") : selected?.disabled ? (en ? "Term restored" : "术语已恢复") : (en ? "Term saved" : "术语已保存"));
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
      setMessage(en ? `Removed ${value.removed} terms` : `已移除 ${value.removed} 条术语`);
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
      setMessage(en ? `Permanently deleted ${value.deleted} terms; future scans can discover them again` : `已彻底删除 ${value.deleted} 条术语；再次扫描可以重新发现`);
      setDeleteOpen(false);
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
                setSearch(event.target.value);
                resetFilterSelection();
              }}
              placeholder={en ? "Search terms" : "搜索术语"}
            />
            <button className="quiet-button" onClick={() => {
              selection.reset();
              setForm(emptyForm);
            }}>{en ? "New" : "新增"}</button>
          </div>
          <div className="term-secondary">
            <div className="term-filters">
              <label><input type="checkbox" checked={onlyConflicts} onChange={(event) => {
                setOnlyConflicts(event.target.checked);
                resetFilterSelection();
              }} />{en ? "Conflicts only" : "只看冲突"}</label>
              <label><input type="checkbox" checked={showDisabled} onChange={(event) => {
                setShowDisabled(event.target.checked);
                resetFilterSelection();
              }} />{en ? "Show removed" : "显示已移除"}</label>
            </div>
            <div className="term-stats">
              <span>revision {data?.terms_revision ?? (en ? "none" : "无")}</span>
              <span>{en ? "Conflicts" : "待裁决"} {data?.conflict_count ?? 0}</span>
            </div>
          </div>
          <div className="batch-toolbar segment-batch-toolbar">
            <span>{en ? `Selected ${selection.selectedKeys.size}` : `已选择 ${selection.selectedKeys.size} 条`}</span>
            <div className="segment-batch-actions">
              <button className="quiet-button" onClick={() => setImportOpen(true)}>{en ? "Import" : "导入"}</button>
              <button className="quiet-button" onClick={() => { setExportSource("published"); setExportOpen(true); }}>{en ? "Export" : "导出"}</button>
              <button
                className="danger-button"
                disabled={!selectedActive.length}
                onClick={() => setRemoveOpen(true)}
              >{en ? "Remove selected" : "移除所选"}</button>
              <button
                className="danger-button"
                disabled={!selectedTerms.length}
                onClick={() => setDeleteOpen(true)}
              >{en ? "Delete permanently" : "彻底删除所选"}</button>
            </div>
            <small className="term-removal-help">{en ? "Remove keeps scan ignore rules; permanent deletion allows rediscovery." : "移除会保留扫描忽略规则；彻底删除后可再次发现。"}</small>
          </div>
        </div>
        {data?.scan.active_task_id && (
          <div className="term-scan-status">
            <div>
              <strong>{en ? "Current scan" : "当前扫描"}</strong>
              <span>{en ? `Done ${data.scan.completed} · Failed ${data.scan.failed} · Pending ${data.scan.pending}` : `已完成 ${data.scan.completed} · 失败 ${data.scan.failed} · 待处理 ${data.scan.pending}`}</span>
              <span>{en ? `${data.scan.candidate_count} candidates available` : `可用候选 ${data.scan.candidate_count} 条`}</span>
              {Object.entries(data.scan.failure_counts).map(([key, count]) => <span key={key} className="scan-error-count">{key} {count}</span>)}
            </div>
            <div className="term-scan-actions">
              {data.scan.failed > 0 && <button className="quiet-button" onClick={() => setShowScanFailures((value) => !value)}>{showScanFailures ? (en ? "Hide failures" : "收起失败") : (en ? "View failures" : "查看失败")}</button>}
              {data.scan.candidate_count > 0 && <button className="quiet-button" onClick={() => { setExportSource("scanned"); setExportOpen(true); }}>{en ? "Export current scan" : "导出当前扫描结果"}</button>}
              {data.scan.candidate_count > 0 && <button className="primary-button" onClick={() => setPartialOpen(true)}>{en ? "Publish available results" : "发布现有结果"}</button>}
            </div>
            {showScanFailures && data.scan.failed_segments.length > 0 && (
              <div className="term-scan-failures">
                {data.scan.failed_segments.map((item) => (
                  <div key={item.segment_id}>
                    <code>{item.segment_id}</code><span>{item.error_class} · {item.error_message}</span>
                  </div>
                ))}
                {data.scan.failed_segments_truncated && <small>{en ? "Showing the first 200 failures." : "仅显示前 200 条失败记录。"}</small>}
              </div>
            )}
          </div>
        )}
        <div className="term-list">
          {visible.map((term) => {
            const selectedRow = selection.selectedKeys.has(term.normalized);
            const focused = selection.focusedKey === term.normalized;
            return (
              <button
                key={term.normalized}
                className={`term-row${selectedRow ? " selected" : ""}${focused ? " focused" : ""}`}
                onClick={(event) => {
                  selection.select(term.normalized, visibleKeys, event);
                  focusTerm(term);
                }}
              >
                <span className={term.has_conflicts ? "term-state conflict" : term.disabled ? "term-state disabled" : "term-state"} />
                <span><strong>{term.source}</strong><small>{term.preferred_translation || (en ? "No preferred translation" : "尚无推荐译名")}</small></span>
                <em>{term.has_conflicts ? (en ? "Conflict" : "待裁决") : term.disabled ? (en ? "Removed" : "已移除") : (en ? "Active" : "有效")}</em>
              </button>
            );
          })}
          {data && !visible.length && <div className="empty">{en ? "No terms match the current filters" : "当前筛选下没有术语"}</div>}
          {!data && <div className="empty">{en ? "Loading terms…" : "正在加载术语…"}</div>}
        </div>
      </section>
      <section className="term-editor">
        <div className="page-heading">
          <div><h1>{selected ? (en ? "Edit term" : "编辑术语") : (en ? "New term" : "新增术语")}</h1><p>{en ? "Saving creates a new term revision immediately." : "保存后立即生成新的术语 revision。"}</p></div>
        </div>
        <label>{en ? "Source term" : "术语原文"}<input value={form.source} disabled={selected?.disabled} onChange={(event) => setForm({ ...form, source: event.target.value })} /></label>
        <label>{en ? "Preferred translation" : "推荐译名"}<input value={form.preferredTranslation} disabled={selected?.disabled} onChange={(event) => setForm({ ...form, preferredTranslation: event.target.value })} /></label>
        {!!selected?.conflicts.preferred_translations.length && (
          <ConflictChoices
            label={en ? "Preferred translation conflicts; choose or enter your own" : "推荐译名存在冲突，请选择或自行填写"}
            values={selected.conflicts.preferred_translations}
            onChoose={(value) => setForm({ ...form, preferredTranslation: value })}
          />
        )}
        <label>{en ? "Category" : "类别"}<input value={form.category} disabled={selected?.disabled} onChange={(event) => setForm({ ...form, category: event.target.value })} /></label>
        {!!selected?.conflicts.categories.length && (
          <ConflictChoices
            label={en ? "Category conflicts; choose or enter your own" : "类别存在冲突，请选择或自行填写"}
            values={selected.conflicts.categories}
            onChoose={(value) => setForm({ ...form, category: value })}
          />
        )}
        {!!selected?.conflicts.alias_primaries.length && (
          <div className="conflict-box">
            <strong>{en ? "An alias is another term's primary entry; change it before saving" : "别名同时是其他术语的主条目，请修改别名后保存"}</strong>
            {selected.conflicts.alias_primaries.map((item) => (
              <p key={`${item.alias}-${item.primary_source}`}>
                {item.alias} → {item.primary_source}
              </p>
            ))}
          </div>
        )}
        <label>{en ? "Description" : "说明"}<textarea value={form.description} disabled={selected?.disabled} onChange={(event) => setForm({ ...form, description: event.target.value })} /></label>
        <label>{en ? "Aliases (one per line)" : "别名（每行一个）"}<textarea value={form.aliases} disabled={selected?.disabled} onChange={(event) => setForm({ ...form, aliases: event.target.value })} /></label>
        {message && <p className={message.startsWith("Error") ? "error-text" : "success-text"}>{message}</p>}
        <div className="editor-actions term-actions">
          {selected?.disabled ? (
            <button className="primary-button" disabled={saving} onClick={() => save(false)}>{en ? "Restore" : "恢复"}</button>
          ) : (
            <>
              <button className="primary-button" disabled={saving || !form.source.trim()} onClick={() => save(false)}>{en ? "Save" : "保存"}</button>
              {selected && <button className="danger-button" disabled={saving} onClick={() => save(true)}>{en ? "Remove" : "移除"}</button>}
            </>
          )}
        </div>
      </section>
      {removeOpen && (
        <ConfirmDialog
          language={language}
          title={en ? "Remove selected terms" : "移除所选术语"}
          text={en ? `Remove ${selectedActive.length} terms. Future scans will continue to ignore them.` : `将移除 ${selectedActive.length} 条术语。重新扫描不会自动恢复这些术语。`}
          confirming={saving}
          onCancel={() => setRemoveOpen(false)}
          onConfirm={removeSelected}
        />
      )}
      {deleteOpen && (
        <ConfirmDialog
          language={language}
          title={en ? "Permanently delete selected terms" : "彻底删除所选术语"}
          text={en ? `Delete ${selectedTerms.length} terms and their scan ignore rules. Future scans can rediscover them. This cannot be undone.` : `将删除 ${selectedTerms.length} 条术语及其扫描忽略规则；再次扫描可以重新发现。该操作不可撤销。`}
          confirmLabel={en ? "Delete permanently" : "确认彻底删除"}
          confirming={saving}
          onCancel={() => setDeleteOpen(false)}
          onConfirm={deleteSelected}
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
            setMessage(en ? "Term list imported" : "术语表已导入");
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
            setMessage(en ? "Available scan results published for later stages" : "现有扫描结果已发布并可用于后续阶段");
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
  const effectiveConfirmLabel = confirmLabel ?? (language === "en" ? "Confirm removal" : "确认移除");
  return (
    <div className="modal-backdrop">
      <div className="modal" role="dialog" aria-modal="true" aria-label={title}>
        <h2>{title}</h2>
        <p>{text}</p>
        <div className="modal-actions">
          <button className="quiet-button" disabled={confirming} onClick={onCancel}>{language === "en" ? "Cancel" : "取消"}</button>
          <button className="danger-button" disabled={confirming} onClick={onConfirm}>{effectiveConfirmLabel}</button>
        </div>
      </div>
    </div>
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
    <div className="modal-backdrop">
      <div className="modal" role="dialog" aria-modal="true" aria-label={language === "en" ? "Import term list" : "导入术语表"}>
        <h2>{language === "en" ? "Import term list" : "导入术语表"}</h2>
        <p>{language === "en" ? "JSON or CSV is merged into the scan baseline; absent terms are not deleted." : "JSON 或 CSV 将增量合并到扫描基线；未出现的术语不会删除。"}</p>
        <label>{language === "en" ? "Term file" : "术语文件"}<input type="file" accept=".json,.csv" onChange={(event) => setFile(event.target.files?.[0] ?? null)} /></label>
        {error && <p className="error-text">{error}</p>}
        <div className="modal-actions">
          <button className="quiet-button" disabled={saving} onClick={onClose}>{language === "en" ? "Cancel" : "取消"}</button>
          <button className="primary-button" disabled={saving || !file} onClick={submit}>{language === "en" ? "Import" : "导入"}</button>
        </div>
      </div>
    </div>
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
        const value = await response.json();
        throw new Error(value.error || `请求失败：${response.status}`);
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
    <div className="modal-backdrop">
      <div className="modal" role="dialog" aria-modal="true" aria-label={language === "en" ? "Export term list" : "导出术语表"}>
        <h2>{language === "en" ? "Export term list" : "导出术语表"}</h2>
        <label>{language === "en" ? "Source" : "来源"}<select value={source} onChange={(event) => setSource(event.target.value as "published" | "scanned")}><option value="published">{language === "en" ? "Published terms" : "已发布术语表"}</option>{hasScanned && <option value="scanned">{language === "en" ? "Current scan candidates" : "当前扫描候选"}</option>}</select></label>
        <label>{language === "en" ? "Format" : "格式"}<select value={format} onChange={(event) => setFormat(event.target.value as "json" | "csv")}><option value="json">JSON</option><option value="csv">CSV</option></select></label>
        <label className="check-row"><input type="checkbox" checked={includeDisabled} onChange={(event) => setIncludeDisabled(event.target.checked)} />{language === "en" ? "Include removed terms" : "包含已移除术语"}</label>
        {error && <p className="error-text">{error}</p>}
        <div className="modal-actions">
          <button className="quiet-button" onClick={onClose}>{language === "en" ? "Cancel" : "取消"}</button>
          <button className="primary-button" onClick={download}>{language === "en" ? "Download" : "下载"}</button>
        </div>
      </div>
    </div>
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
      <div className="modal" role="dialog" aria-modal="true" aria-label={language === "en" ? "Publish available scan results" : "发布现有扫描结果"}>
        <h2>{language === "en" ? "Publish available scan results" : "发布现有扫描结果"}</h2>
        <p>{language === "en" ? `Merge ${count} available candidate terms into the active term list. Incomplete segments remain unscanned and historical candidates are kept.` : `将合并当前可用的 ${count} 条候选术语，立即作为正式术语表使用。未完成 Segment 不会被标记为已扫描；历史候选仍会保留。`}</p>
        {error && <p className="error-text">{error}</p>}
        <div className="modal-actions">
          <button className="quiet-button" disabled={working} onClick={onClose}>{language === "en" ? "Cancel" : "取消"}</button>
          <button className="primary-button" disabled={working || !count} onClick={confirm}>{working ? (language === "en" ? "Publishing…" : "正在发布…") : (language === "en" ? "Publish" : "确认发布")}</button>
        </div>
      </div>
    </div>
  );
}
