import { useEffect, useMemo, useState } from "react";
import { api } from "../api";
import type { Term, TermsResponse } from "../types";

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
  const [selectedKey, setSelectedKey] = useState<string | null>(null);
  const [form, setForm] = useState<TermForm>(emptyForm);
  const [search, setSearch] = useState("");
  const [onlyConflicts, setOnlyConflicts] = useState(false);
  const [showDisabled, setShowDisabled] = useState(false);
  const [message, setMessage] = useState("");
  const [saving, setSaving] = useState(false);
  const selected = data?.terms.find((term) => term.normalized === selectedKey) ?? null;

  useEffect(() => {
    setData(null);
    setSelectedKey(null);
    setForm(emptyForm);
    setMessage("");
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

  function selectTerm(term: Term | null) {
    setSelectedKey(term?.normalized ?? null);
    setForm(term ? formFor(term) : emptyForm);
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
          old_normalized: selectedKey,
          source: form.source,
          preferred_translation: form.preferredTranslation,
          category: form.category,
          description: form.description,
          aliases: form.aliases.split("\n").map((item) => item.trim()).filter(Boolean),
          disabled,
        }),
      });
      setData(value);
      const saved = value.terms.find((term) => term.source === form.source && term.disabled === disabled) ?? null;
      selectTerm(saved);
      setMessage(disabled ? "术语已移除" : selected?.disabled ? "术语已恢复" : "术语已保存");
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
            <input value={search} onChange={(event) => setSearch(event.target.value)} placeholder="搜索术语" />
            <button className="quiet-button" onClick={() => selectTerm(null)}>新增</button>
          </div>
          <div className="term-secondary">
            <div className="term-filters">
              <label><input type="checkbox" checked={onlyConflicts} onChange={(event) => setOnlyConflicts(event.target.checked)} />只看冲突</label>
              <label><input type="checkbox" checked={showDisabled} onChange={(event) => setShowDisabled(event.target.checked)} />显示已移除</label>
            </div>
            <div className="term-stats">
              <span>revision {data?.terms_revision ?? "无"}</span>
              <span>待裁决 {data?.conflict_count ?? 0}</span>
            </div>
          </div>
        </div>
        <div className="term-list">
          {visible.map((term) => (
            <button
              key={term.normalized}
              className={term.normalized === selectedKey ? "term-row selected" : "term-row"}
              onClick={() => selectTerm(term)}
            >
              <span className={term.has_conflicts ? "term-state conflict" : term.disabled ? "term-state disabled" : "term-state"} />
              <span><strong>{term.source}</strong><small>{term.preferred_translation || "尚无推荐译名"}</small></span>
              <em>{term.has_conflicts ? "待裁决" : term.disabled ? "已移除" : "有效"}</em>
            </button>
          ))}
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
