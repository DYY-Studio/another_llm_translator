import { useEffect, useState } from "react";
import { api } from "../api";
import { AdapterSettings } from "./AdapterSettings";

type SettingsTab = "adapter" | "config" | "prompts";

export function SettingsView({ project }: { project: string }) {
  const [tab, setTab] = useState<SettingsTab>("adapter");
  return (
    <div className="settings-page">
      <div className="settings-tabs" aria-label="项目设置">
        <button className={tab === "adapter" ? "active" : ""} onClick={() => setTab("adapter")}>LLM Adapter</button>
        <button className={tab === "config" ? "active" : ""} onClick={() => setTab("config")}>项目配置</button>
        <button className={tab === "prompts" ? "active" : ""} onClick={() => setTab("prompts")}>Prompt</button>
      </div>
      {tab === "adapter" && <AdapterSettings project={project} />}
      {tab === "config" && <TextSettings project={project} kind="config" />}
      {tab === "prompts" && <TextSettings project={project} kind="prompt" />}
    </div>
  );
}

function TextSettings({ project, kind }: { project: string; kind: "config" | "prompt" }) {
  const [stage, setStage] = useState("translation");
  const [content, setContent] = useState("");
  const [message, setMessage] = useState("");
  const path = kind === "config"
    ? `/api/v1/projects/${project}/config`
    : `/api/v1/projects/${project}/prompts/${stage}`;

  useEffect(() => {
    setMessage("");
    void api<{ content: string }>(path).then((value) => setContent(value.content));
  }, [path]);

  async function save() {
    await api(path, {
      method: "PUT",
      body: JSON.stringify({ content }),
    });
    setMessage("已验证并保存");
  }

  return (
    <section className="text-settings">
      <div className="page-heading">
        <div>
          <h1>{kind === "config" ? "项目配置" : "Prompt"}</h1>
          <p>{kind === "config" ? "保存前会严格验证完整 TOML。" : "编辑项目内的阶段 Prompt 副本。"}</p>
        </div>
        <button className="primary-button" onClick={save}>验证并保存</button>
      </div>
      {kind === "prompt" && (
        <label className="stage-select">阶段
          <select value={stage} onChange={(event) => setStage(event.target.value)}>
            <option value="terminology">术语</option>
            <option value="translation">翻译</option>
            <option value="proofreading">校对</option>
            <option value="polishing">润色</option>
          </select>
        </label>
      )}
      <span className="success-text">{message}</span>
      <textarea
        className="settings-editor"
        spellCheck={false}
        value={content}
        onChange={(event) => setContent(event.target.value)}
      />
    </section>
  );
}
