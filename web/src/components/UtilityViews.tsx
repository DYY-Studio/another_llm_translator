import { useEffect, useRef, useState } from "react";
import { api } from "../api";
import { useClassicSelection } from "../useClassicSelection";
import type { ProjectOverview } from "../types";

export function Overview({
  project,
  value,
  onFilesChanged,
}: {
  project: string;
  value: ProjectOverview;
  onFilesChanged: () => Promise<void>;
}) {
  const completed = value.segments.filter((item) => item.translation).length;
  const selection = useClassicSelection();
  const uploadRef = useRef<HTMLInputElement>(null);
  const [removing, setRemoving] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const fileIds = value.files.map((item) => item.file_id);
  const canAdd = value.document_adapter_id === "txt" || value.files.length === 0;

  async function upload(files: FileList | null) {
    if (!files?.length) return;
    setBusy(true);
    setError("");
    try {
      const body = new FormData();
      Array.from(files).forEach((file) => body.append("files", file));
      await api(`/api/v1/projects/${project}/files`, {
        method: "POST",
        body,
      });
      selection.reset();
      await onFilesChanged();
    } catch (value) {
      setError(String(value));
    } finally {
      setBusy(false);
      if (uploadRef.current) uploadRef.current.value = "";
    }
  }

  async function removeSelected() {
    setBusy(true);
    setError("");
    try {
      await api(`/api/v1/projects/${project}/files/remove`, {
        method: "POST",
        body: JSON.stringify({ file_ids: [...selection.selectedKeys] }),
      });
      selection.reset();
      setRemoving(false);
      await onFilesChanged();
    } catch (value) {
      setError(String(value));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="page">
      <div className="page-heading"><div><h1>{value.name}</h1><p>{value.path}</p></div></div>
      <div className="summary-strip">
        <div><strong>{value.files.length}</strong><span>文件</span></div>
        <div><strong>{value.nonempty_segment_count}</strong><span>非空 Segment</span></div>
        <div><strong>{completed}</strong><span>已有译文</span></div>
      </div>
      <div className="section-heading">
        <div><h2>文件</h2><p>{value.document_adapter_id.toUpperCase()} 项目</p></div>
        <div className="section-actions">
          <input
            ref={uploadRef}
            className="visually-hidden"
            type="file"
            accept={value.document_adapter_id === "epub" ? ".epub,application/epub+zip" : ".txt,text/plain"}
            multiple={value.document_adapter_id === "txt"}
            onChange={(event) => void upload(event.target.files)}
          />
          <button className="quiet-button" disabled={busy || !canAdd} onClick={() => uploadRef.current?.click()}>
            添加文件
          </button>
          <button className="danger-button" disabled={busy || selection.selectedKeys.size === 0} onClick={() => setRemoving(true)}>
            移除所选
          </button>
        </div>
      </div>
      {error && <button className="error-banner" onClick={() => setError("")}>{error}</button>}
      <div className="file-list">
        {value.files.length === 0 && (
          <div className="empty-file-state">
            <strong>项目还没有源文件</strong>
            <span>添加包含非空文本的 {value.document_adapter_id.toUpperCase()} 文件后即可运行阶段。</span>
          </div>
        )}
        {value.files.map((item) => (
          <button
            type="button"
            key={item.file_id}
            className={`file-row${selection.selectedKeys.has(item.file_id) ? " selected" : ""}`}
            onClick={(event) => selection.select(item.file_id, fileIds, event)}
          >
            <span>{item.file_id}</span><strong>{item.name}</strong>
          </button>
        ))}
      </div>
      {removing && (
        <div className="modal-backdrop" onMouseDown={() => setRemoving(false)}>
          <div className="modal" onMouseDown={(event) => event.stopPropagation()}>
            <h2>移除 {selection.selectedKeys.size} 个文件？</h2>
            <p>项目内源文件副本和活动 Segment 将被删除；历史阶段结果与既有输出文件会保留。以后重新添加会分配新的 File 与 Segment ID。</p>
            <div className="modal-actions">
              <button className="quiet-button" onClick={() => setRemoving(false)}>取消</button>
              <button className="danger-button" disabled={busy} onClick={() => void removeSelected()}>确认移除</button>
            </div>
          </div>
        </div>
      )}
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
  const [empty, setEmpty] = useState(false);
  useEffect(() => {
    void api<{ adapters: Array<{ adapter_id: string; capabilities: string[] }> }>("/api/v1/document-adapters")
      .then((value) => setAdapters(value.adapters));
  }, []);
  async function submit() {
    const body = new FormData();
    body.append("name", name);
    body.append("document_adapter", adapter);
    body.append("empty", String(empty));
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
        <label className="check-row">
          <input type="checkbox" checked={empty} onChange={(event) => { setEmpty(event.target.checked); if (event.target.checked) setFiles(null); }} />
          创建空项目，稍后添加文件
        </label>
        <label>输入文件
          <input
            key={adapter}
            type="file"
            disabled={empty}
            accept={adapter === "epub" ? ".epub,application/epub+zip" : ".txt,text/plain"}
            multiple={adapter === "txt"}
            onChange={(event) => setFiles(event.target.files)}
          />
        </label>
        <div className="modal-actions"><button className="quiet-button" onClick={onClose}>取消</button><button className="primary-button" disabled={!name || (!empty && !files?.length)} onClick={submit}>创建项目</button></div>
      </div>
    </div>
  );
}
