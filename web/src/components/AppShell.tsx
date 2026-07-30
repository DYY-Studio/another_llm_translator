import type { ReactNode } from "react";
import type { ProjectSummary, Stage, TaskState } from "../types";
import { icons } from "./Icons";

const items: Array<{ id: Stage; label: string }> = [
  { id: "overview", label: "项目概览" },
  { id: "terminology", label: "术语" },
  { id: "translation", label: "翻译" },
  { id: "proofreading", label: "校对" },
  { id: "polishing", label: "润色" },
  { id: "export", label: "导出" },
];

export function AppShell({
  projects,
  project,
  stage,
  task,
  onProject,
  onStage,
  onCreate,
  onRun,
  onCancel,
  children,
}: {
  projects: ProjectSummary[];
  project: string;
  stage: Stage;
  task: TaskState | null;
  onProject: (value: string) => void;
  onStage: (value: Stage) => void;
  onCreate: () => void;
  onRun: () => void;
  onCancel: () => void;
  children: ReactNode;
}) {
  const running = task && ["queued", "running", "cancelling"].includes(task.status);
  return (
    <div className="app">
      <header className="topbar">
        <div className="brand">译工坊</div>
        <select value={project} onChange={(event) => onProject(event.target.value)}>
          <option value="">选择项目</option>
          {projects.map((item) => <option key={item.name}>{item.name}</option>)}
        </select>
        <button className="quiet-button create-button" onClick={onCreate}>新建项目</button>
        <div className="topbar-spacer" />
        <button className="icon-button" aria-label="设置" onClick={() => onStage("settings")}>
          {icons.settings}
        </button>
      </header>
      <aside className="sidebar">
        <nav>
          {items.map((item) => (
            <button
              className={stage === item.id ? "nav-item active" : "nav-item"}
              key={item.id}
              aria-label={item.label}
              onClick={() => onStage(item.id)}
            >
              {icons[item.id]}<span>{item.label}</span>
            </button>
          ))}
        </nav>
        <div className="run-panel">
          <button className="primary-button run-button" disabled={!project || !!running} onClick={onRun}>
            {running ? "正在执行" : "开始当前阶段"}
          </button>
          {task && (
            <div className="task-summary">
              <strong>{task.status === "running" ? "运行中" : task.status}</strong>
              <span>{task.stage}</span>
              {task.error && <span className="error-text">{task.error}</span>}
              {running && <button className="danger-link" onClick={onCancel}>取消任务</button>}
            </div>
          )}
        </div>
      </aside>
      <main className="main">{children}</main>
    </div>
  );
}
