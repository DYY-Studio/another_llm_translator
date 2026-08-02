import type { ReactNode } from "react";
import type { ProjectSummary, Stage, TaskState, ThemeMode } from "../types";
import { icons } from "./Icons";

const items: Array<{ id: Stage; label: string }> = [
  { id: "overview", label: "项目概览" },
  { id: "diagnostics", label: "仪表盘" },
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
  onShowFailures,
  onCreate,
  onRun,
  onCancel,
  canRun,
  runLoading,
  themeMode,
  onTheme,
  children,
}: {
  projects: ProjectSummary[];
  project: string;
  stage: Stage;
  task: TaskState | null;
  onProject: (value: string) => void;
  onStage: (value: Stage) => void;
  onShowFailures: () => void;
  onCreate: () => void;
  onRun: () => void;
  onCancel: () => void;
  canRun: boolean;
  runLoading: boolean;
  themeMode: ThemeMode;
  onTheme: () => void;
  children: ReactNode;
}) {
  const running = Boolean(
    task && ["queued", "running", "cancelling"].includes(task.status),
  );
  const completed = task?.completed_segments ?? 0;
  const failed = task?.failed_segments ?? 0;
  const pending = task?.pending_segments ?? 0;
  const total = task?.total_segments ?? 0;
  const processed = completed + failed;
  const statusLabels: Record<string, string> = {
    queued: "等待中",
    running: "运行中",
    cancelling: "正在取消",
    completed: "已完成",
    failed: "失败",
    cancelled: "已取消",
  };
  const themeLabels: Record<ThemeMode, string> = {
    system: "跟随系统",
    light: "浅色",
    dark: "深色",
  };
  const nextTheme: Record<ThemeMode, ThemeMode> = {
    system: "light",
    light: "dark",
    dark: "system",
  };
  const themeIcon = themeMode === "system"
    ? icons.themeSystem
    : themeMode === "light"
      ? icons.themeLight
      : icons.themeDark;
  const themeTitle = `当前外观：${themeLabels[themeMode]}；切换为${themeLabels[nextTheme[themeMode]]}`;
  return (
    <div className={`app${task ? " has-run-status" : ""}`}>
      <header className="topbar">
        <div className="brand">译工坊</div>
        <select value={project} onChange={(event) => onProject(event.target.value)}>
          <option value="">选择项目</option>
          {projects.map((item) => <option key={item.selector} value={item.selector}>{item.external ? `${item.name} · ${item.path}` : item.name}</option>)}
        </select>
        <button className="quiet-button create-button" onClick={onCreate}>新建 / 打开</button>
        <div className="topbar-spacer" />
        <button className="icon-button" aria-label={themeTitle} title={themeTitle} onClick={onTheme}>
          {themeIcon}
        </button>
        <button className="icon-button" aria-label="设置" onClick={() => onStage("settings")}>
          {icons.settings}
        </button>
      </header>
      {task && (
        <section className="global-run-status" aria-label="全局任务状态">
          <div className="run-identity">
            <strong>{statusLabels[task.status] ?? task.status}</strong>
            <span>{task.project} · {task.stage}</span>
          </div>
          <div className="run-progress">
            <span>已完成 {completed} · 失败 {failed} · 待处理 {pending} / {total}</span>
            <div className="progress-track" role="progressbar" aria-label="任务进度" aria-valuemin={0} aria-valuemax={total} aria-valuenow={processed}>
              <span className="progress-completed" style={{ width: `${total ? completed / total * 100 : 0}%` }} />
              <span className="progress-failed" style={{ width: `${total ? failed / total * 100 : 0}%` }} />
            </div>
          </div>
          <div className="run-tokens">
            {task.usage.available ? (
              <><span>输入 {task.usage.input_tokens} Tokens</span><span>输出 {task.usage.output_tokens} Tokens</span></>
            ) : <span>精确 Tokens 不可用</span>}
          </div>
          {failed > 0 && <button className="run-failure-link" onClick={onShowFailures}>查看 {failed} 个失败 Segment</button>}
          {task.error && <span className="error-text run-error">{task.error}</span>}
          {running && <button className="danger-link" onClick={onCancel}>取消任务</button>}
        </section>
      )}
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
        {canRun && <div className="run-panel">
          {canRun && (
            <button className="primary-button run-button" disabled={!project || running || runLoading} onClick={onRun}>
              {running ? "正在执行" : runLoading ? "正在检查" : "开始当前阶段"}
            </button>
          )}
        </div>}
      </aside>
      <main className="main">
        {canRun && (
          <div className="mobile-run-bar">
            {canRun && (
              <button className="primary-button" disabled={!project || running || runLoading} onClick={onRun}>
                {running ? "正在执行" : runLoading ? "正在检查" : "运行当前阶段"}
              </button>
            )}
          </div>
        )}
        {children}
      </main>
    </div>
  );
}
