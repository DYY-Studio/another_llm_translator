import type { ReactNode } from "react";
import type { ProjectSummary, Stage, TaskState, ThemeMode } from "../types";
import { icons } from "./Icons";
import type { Language } from "../i18n";
import { translate } from "../i18n";

const items: Array<{ id: Stage; key: string }> = [
  { id: "overview", key: "nav.overview" },
  { id: "diagnostics", key: "nav.diagnostics" },
  { id: "terminology", key: "nav.terminology" },
  { id: "translation", key: "nav.translation" },
  { id: "proofreading", key: "nav.proofreading" },
  { id: "polishing", key: "nav.polishing" },
  { id: "export", key: "nav.export" },
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
  language,
  onLanguage,
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
  language: Language;
  onLanguage: () => void;
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
  const statusLabels: Record<string, string> = Object.fromEntries(
    ["queued", "running", "cancelling", "completed", "failed", "cancelled"]
      .map((key) => [key, translate(`run.${key}`, language)]),
  );
  const themeLabels: Record<ThemeMode, string> = {
    system: translate("theme.system", language),
    light: translate("theme.light", language),
    dark: translate("theme.dark", language),
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
        <div className="brand">{translate("brand", language)}</div>
        <select value={project} onChange={(event) => onProject(event.target.value)}>
          <option value="">{translate("project.select", language)}</option>
          {projects.map((item) => <option key={item.selector} value={item.selector}>{item.external ? `${item.name} · ${item.path}` : item.name}</option>)}
        </select>
        <button className="quiet-button create-button" onClick={onCreate}>{translate("project.create", language)}</button>
        <div className="topbar-spacer" />
        <button className="icon-button" aria-label={themeTitle} title={themeTitle} onClick={onTheme}>
          {themeIcon}
        </button>
        <button className="icon-button" aria-label={translate("nav.settings", language)} onClick={() => onStage("settings")}>
          {icons.settings}
        </button>
        <button className="language-button" onClick={onLanguage}>{translate("language.switch", language)}</button>
      </header>
      {task && (
        <section className="global-run-status" aria-label="全局任务状态">
          <div className="run-identity">
            <strong>{statusLabels[task.status] ?? task.status}</strong>
            <span>{task.project} · {task.stage}</span>
          </div>
          <div className="run-progress">
            <span>{translate("run.completedCount", language, { completed, failed, pending, total })}</span>
            <div className="progress-track" role="progressbar" aria-label="任务进度" aria-valuemin={0} aria-valuemax={total} aria-valuenow={processed}>
              <span className="progress-completed" style={{ width: `${total ? completed / total * 100 : 0}%` }} />
              <span className="progress-failed" style={{ width: `${total ? failed / total * 100 : 0}%` }} />
            </div>
          </div>
          <div className="run-tokens">
            {task.usage.available ? (
              <><span>输入 {task.usage.input_tokens} Tokens</span><span>输出 {task.usage.output_tokens} Tokens</span></>
            ) : <span>{translate("run.tokensUnavailable", language)}</span>}
          </div>
          {failed > 0 && <button className="run-failure-link" onClick={onShowFailures}>{language === "en" ? `View ${failed} failed segments` : `查看 ${failed} 个失败 Segment`}</button>}
          {task.error && <span className="error-text run-error">{task.error}</span>}
          {running && <button className="danger-link" onClick={onCancel}>{translate("run.cancel", language)}</button>}
        </section>
      )}
      <aside className="sidebar">
        <nav>
          {items.map((item) => (
            <button
              className={stage === item.id ? "nav-item active" : "nav-item"}
              key={item.id}
              aria-label={translate(item.key, language)}
              onClick={() => onStage(item.id)}
            >
              {icons[item.id]}<span>{translate(item.key, language)}</span>
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
