import { useEffect, useState } from "react";
import { api } from "../api";
import type { ProjectOverview } from "../types";

export function Overview({ value }: { value: ProjectOverview }) {
  const completed = value.segments.filter((item) => item.translation).length;
  return (
    <div className="page">
      <div className="page-heading"><div><h1>{value.name}</h1><p>{value.path}</p></div></div>
      <div className="summary-strip">
        <div><strong>{value.files.length}</strong><span>文件</span></div>
        <div><strong>{value.segments.length}</strong><span>非空 Segment</span></div>
        <div><strong>{completed}</strong><span>已有译文</span></div>
      </div>
      <h2>文件</h2>
      <div className="file-list">
        {value.files.map((item) => <div key={item.file_id}><span>{item.file_id}</span><strong>{item.name}</strong></div>)}
      </div>
    </div>
  );
}

export function TermsView({ project }: { project: string }) {
  const [data, setData] = useState<{ terms_revision: number | null; conflict_count: number; terms: Array<Record<string, unknown>> } | null>(null);
  useEffect(() => { void api<typeof data>(`/api/v1/projects/${project}/terms`).then(setData); }, [project]);
  return (
    <div className="page">
      <div className="page-heading"><div><h1>术语</h1><p>优先处理冲突，再检查已发布术语。</p></div></div>
      <div className="term-table">
        <div className="table-head"><span>原文</span><span>类别</span><span>推荐译名</span><span>状态</span></div>
        {data?.terms.map((item) => <div className="table-row" key={String(item.normalized)}>
          <strong>{String(item.source)}</strong>
          <span>{String(item.category ?? "—")}</span>
          <span>{String(item.preferred_translation ?? "—")}</span>
          <span>{item.has_conflicts ? "待裁决" : item.disabled ? "已移除" : "有效"}</span>
        </div>)}
        {!data?.terms.length && <div className="empty">尚无已发布术语</div>}
      </div>
    </div>
  );
}

export function ExportView({ project }: { project: string }) {
  const [stage, setStage] = useState("translated");
  const [bilingual, setBilingual] = useState(false);
  const [result, setResult] = useState("");
  async function run() {
    const value = await api<Record<string, unknown>>(`/api/v1/projects/${project}/export`, {
      method: "POST",
      body: JSON.stringify({ stage, bilingual, allow_missing: false }),
    });
    setResult(JSON.stringify(value, null, 2));
  }
  return (
    <div className="page narrow-page">
      <div className="page-heading"><div><h1>导出</h1><p>从已持久化结果生成输出文件。</p></div></div>
      <label>结果阶段<select value={stage} onChange={(event) => setStage(event.target.value)}><option value="translated">翻译</option><option value="proofread">已应用校对</option><option value="polished">已应用润色</option></select></label>
      <label className="check-row"><input type="checkbox" checked={bilingual} onChange={(event) => setBilingual(event.target.checked)} /> 生成双语对照</label>
      <button className="primary-button" onClick={run}>生成输出</button>
      {result && <pre className="result-box">{result}</pre>}
    </div>
  );
}

export function CreateProjectDialog({ onClose, onCreated }: { onClose: () => void; onCreated: (name: string) => void }) {
  const [name, setName] = useState("");
  const [adapter, setAdapter] = useState("txt");
  const [adapters, setAdapters] = useState<Array<{ adapter_id: string; capabilities: string[] }>>([]);
  const [files, setFiles] = useState<FileList | null>(null);
  useEffect(() => {
    void api<{ adapters: Array<{ adapter_id: string; capabilities: string[] }> }>("/api/v1/document-adapters")
      .then((value) => setAdapters(value.adapters));
  }, []);
  async function submit() {
    const body = new FormData();
    body.append("name", name);
    body.append("document_adapter", adapter);
    Array.from(files ?? []).forEach((file) => body.append("files", file));
    await api("/api/v1/projects", { method: "POST", body });
    onCreated(name);
  }
  return (
    <div className="modal-backdrop" onMouseDown={onClose}>
      <div className="modal" onMouseDown={(event) => event.stopPropagation()}>
        <h2>新建项目</h2>
        <label>项目名<input value={name} onChange={(event) => setName(event.target.value)} /></label>
        <label>文档格式
          <select value={adapter} onChange={(event) => { setAdapter(event.target.value); setFiles(null); }}>
            {adapters.map((item) => <option key={item.adapter_id} value={item.adapter_id}>{item.adapter_id.toUpperCase()}</option>)}
          </select>
        </label>
        <label>输入文件
          <input
            key={adapter}
            type="file"
            accept={adapter === "epub" ? ".epub,application/epub+zip" : ".txt,text/plain"}
            multiple={adapter === "txt"}
            onChange={(event) => setFiles(event.target.files)}
          />
        </label>
        <div className="modal-actions"><button className="quiet-button" onClick={onClose}>取消</button><button className="primary-button" disabled={!name || !files?.length} onClick={submit}>创建项目</button></div>
      </div>
    </div>
  );
}
