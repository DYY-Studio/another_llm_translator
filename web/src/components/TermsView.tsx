import { useEffect, useMemo, useState } from "react";
import { api } from "../api";
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

export function TermsView({ project }: { project: string }) {
  const [data, setData] = useState<TermsResponse | null>(null);
  const [form, setForm] = useState<TermForm>(emptyForm);
  const [search, setSearch] = useState("");
  const [onlyConflicts, setOnlyConflicts] = useState(false);
  const [showDisabled, setShowDisabled] = useState(false);
  const [message, setMessage] = useState("");
  const [saving, setSaving] = useState(false);
  const [removeOpen, setRemoveOpen] = useState(false);
  const [importOpen, setImportOpen] = useState(false);
  const [exportOpen, setExportOpen] = useState(false);
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
      setMessage("术语原文不能为空");
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
      setMessage(disabled ? "术语已移除" : selected?.disabled ? "术语已恢复" : "术语已保存");
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
      setMessage(`已移除 ${value.removed} 条术语`);
      setRemoveOpen(false);
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
              placeholder="搜索术语"
            />
            <button className="quiet-button" onClick={() => {
              selection.reset();
              setForm(emptyForm);
            }}>新增</button>
          </div>
          <div className="term-secondary">
            <div className="term-filters">
              <label><input type="checkbox" checked={onlyConflicts} onChange={(event) => {
                setOnlyConflicts(event.target.checked);
                resetFilterSelection();
              }} />只看冲突</label>
              <label><input type="checkbox" checked={showDisabled} onChange={(event) => {
                setShowDisabled(event.target.checked);
                resetFilterSelection();
              }} />显示已移除</label>
            </div>
            <div className="term-stats">
              <span>revision {data?.terms_revision ?? "无"}</span>
              <span>待裁决 {data?.conflict_count ?? 0}</span>
            </div>
          </div>
          <div className="batch-toolbar">
            <span>已选择 {selection.selectedKeys.size} 条</span>
            <button className="quiet-button" onClick={() => setImportOpen(true)}>导入</button>
            <button className="quiet-button" onClick={() => setExportOpen(true)}>导出</button>
            <button
              className="danger-button"
              disabled={!selectedActive.length}
              onClick={() => setRemoveOpen(true)}
            >移除所选</button>
          </div>
        </div>
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
                <span><strong>{term.source}</strong><small>{term.preferred_translation || "尚无推荐译名"}</small></span>
                <em>{term.has_conflicts ? "待裁决" : term.disabled ? "已移除" : "有效"}</em>
              </button>
            );
          })}
          {data && !visible.length && <div className="empty">当前筛选下没有术语</div>}
          {!data && <div className="empty">正在加载术语…</div>}
        </div>
      </section>
      <section className="term-editor">
        <div className="page-heading">
          <div><h1>{selected ? "编辑术语" : "新增术语"}</h1><p>保存后立即生成新的术语 revision。</p></div>
        </div>
        <label>术语原文<input value={form.source} disabled={selected?.disabled} onChange={(event) => setForm({ ...form, source: event.target.value })} /></label>
        <label>推荐译名<input value={form.preferredTranslation} disabled={selected?.disabled} onChange={(event) => setForm({ ...form, preferredTranslation: event.target.value })} /></label>
        {!!selected?.conflicts.preferred_translations.length && (
          <ConflictChoices
            label="推荐译名存在冲突，请选择或自行填写"
            values={selected.conflicts.preferred_translations}
            onChoose={(value) => setForm({ ...form, preferredTranslation: value })}
          />
        )}
        <label>类别<input value={form.category} disabled={selected?.disabled} onChange={(event) => setForm({ ...form, category: event.target.value })} /></label>
        {!!selected?.conflicts.categories.length && (
          <ConflictChoices
            label="类别存在冲突，请选择或自行填写"
            values={selected.conflicts.categories}
            onChoose={(value) => setForm({ ...form, category: value })}
          />
        )}
        {!!selected?.conflicts.alias_primaries.length && (
          <div className="conflict-box">
            <strong>别名同时是其他术语的主条目，请修改别名后保存</strong>
            {selected.conflicts.alias_primaries.map((item) => (
              <p key={`${item.alias}-${item.primary_source}`}>
                {item.alias} → {item.primary_source}
              </p>
            ))}
          </div>
        )}
        <label>说明<textarea value={form.description} disabled={selected?.disabled} onChange={(event) => setForm({ ...form, description: event.target.value })} /></label>
        <label>别名（每行一个）<textarea value={form.aliases} disabled={selected?.disabled} onChange={(event) => setForm({ ...form, aliases: event.target.value })} /></label>
        {message && <p className={message.startsWith("Error") ? "error-text" : "success-text"}>{message}</p>}
        <div className="editor-actions term-actions">
          {selected?.disabled ? (
            <button className="primary-button" disabled={saving} onClick={() => save(false)}>恢复</button>
          ) : (
            <>
              <button className="primary-button" disabled={saving || !form.source.trim()} onClick={() => save(false)}>保存</button>
              {selected && <button className="danger-button" disabled={saving} onClick={() => save(true)}>移除</button>}
            </>
          )}
        </div>
      </section>
      {removeOpen && (
        <ConfirmDialog
          title="移除所选术语"
          text={`将移除 ${selectedActive.length} 条术语。重新扫描不会自动恢复这些术语。`}
          confirming={saving}
          onCancel={() => setRemoveOpen(false)}
          onConfirm={removeSelected}
        />
      )}
      {importOpen && (
        <TermImportDialog
          project={project}
          onClose={() => setImportOpen(false)}
          onImported={(value) => {
            setData(value);
            selection.reset();
            setForm(emptyForm);
            setImportOpen(false);
            setMessage("术语表已导入");
          }}
        />
      )}
      {exportOpen && (
        <TermExportDialog
          project={project}
          onClose={() => setExportOpen(false)}
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
  confirming,
  onCancel,
  onConfirm,
}: {
  title: string;
  text: string;
  confirming: boolean;
  onCancel: () => void;
  onConfirm: () => void;
}) {
  return (
    <div className="modal-backdrop">
      <div className="modal" role="dialog" aria-modal="true" aria-label={title}>
        <h2>{title}</h2>
        <p>{text}</p>
        <div className="modal-actions">
          <button className="quiet-button" disabled={confirming} onClick={onCancel}>取消</button>
          <button className="danger-button" disabled={confirming} onClick={onConfirm}>确认移除</button>
        </div>
      </div>
    </div>
  );
}

function TermImportDialog({
  project,
  onClose,
  onImported,
}: {
  project: string;
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
      <div className="modal" role="dialog" aria-modal="true" aria-label="导入术语表">
        <h2>导入术语表</h2>
        <p>JSON 或 CSV 将增量合并到扫描基线；未出现的术语不会删除。</p>
        <label>术语文件<input type="file" accept=".json,.csv" onChange={(event) => setFile(event.target.files?.[0] ?? null)} /></label>
        {error && <p className="error-text">{error}</p>}
        <div className="modal-actions">
          <button className="quiet-button" disabled={saving} onClick={onClose}>取消</button>
          <button className="primary-button" disabled={saving || !file} onClick={submit}>导入</button>
        </div>
      </div>
    </div>
  );
}

function TermExportDialog({
  project,
  onClose,
}: {
  project: string;
  onClose: () => void;
}) {
  const [format, setFormat] = useState<"json" | "csv">("json");
  const [includeDisabled, setIncludeDisabled] = useState(false);
  const [error, setError] = useState("");

  async function download() {
    try {
      const response = await fetch(
        `/api/v1/projects/${project}/terms/export?format=${format}&include_disabled=${includeDisabled}`,
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
      <div className="modal" role="dialog" aria-modal="true" aria-label="导出术语表">
        <h2>导出术语表</h2>
        <label>格式<select value={format} onChange={(event) => setFormat(event.target.value as "json" | "csv")}><option value="json">JSON</option><option value="csv">CSV</option></select></label>
        <label className="check-row"><input type="checkbox" checked={includeDisabled} onChange={(event) => setIncludeDisabled(event.target.checked)} />包含已移除术语</label>
        {error && <p className="error-text">{error}</p>}
        <div className="modal-actions">
          <button className="quiet-button" onClick={onClose}>取消</button>
          <button className="primary-button" onClick={download}>下载</button>
        </div>
      </div>
    </div>
  );
}
