import type { ReactNode } from "react";
import type { Stage, TaskState, ThemeMode } from "../types";
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
  project,
  stage,
  task,
  onStage,
  onShowFailures,
  onRun,
  onCancel,
  canRun,
  runLoading,
  starting,
  themeMode,
  onTheme,
  language,
  onLanguage,
  children,
}: {
  project: string;
  stage: Stage;
  task: TaskState | null;
  onStage: (value: Stage) => void;
  onShowFailures: () => void;
  onRun: () => void;
  onCancel: () => void;
  canRun: boolean;
  runLoading: boolean;
  starting: boolean;
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
  const themeTitle = translate("theme.current", language, {
    current: themeLabels[themeMode],
    next: themeLabels[nextTheme[themeMode]],
  });
  return (
    <div className={`app${task ? " has-run-status" : ""}`}>
      <header className="topbar">
        <div className="brand">{translate("brand", language)}</div>
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
        <section className="global-run-status" aria-label={translate("shell.globalTaskStatus", language)}>
          <div className="run-identity">
            <strong>{statusLabels[task.status] ?? task.status}</strong>
            <span>{task.project} · {task.stage}</span>
          </div>
          <div className="run-progress">
            <span>{translate("run.completedCount", language, { completed, failed, pending, total })}</span>
            <div className="progress-track" role="progressbar" aria-label={translate("shell.taskProgress", language)} aria-valuemin={0} aria-valuemax={total} aria-valuenow={processed}>
              <span className="progress-completed" style={{ width: `${total ? completed / total * 100 : 0}%` }} />
              <span className="progress-failed" style={{ width: `${total ? failed / total * 100 : 0}%` }} />
            </div>
          </div>
          <div className="run-tokens">
            {task.usage.available ? (
              <><span>{translate("run.tokensInput", language)} {task.usage.input_tokens} Tokens</span><span>{translate("run.tokensOutput", language)} {task.usage.output_tokens} Tokens</span></>
            ) : <span>{translate("run.tokensUnavailable", language)}</span>}
          </div>
          {failed > 0 && <button className="run-failure-link" onClick={onShowFailures}>{translate("run.failedSegments", language, { count: failed })}</button>}
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
            <button className="primary-button run-button" disabled={!project || running || starting || runLoading} onClick={onRun}>
              <span className="run-label-full">{running ? translate("run.buttonRunning", language) : starting ? translate("run.buttonStarting", language) : runLoading ? translate("run.buttonChecking", language) : translate("run.buttonStart", language)}</span>
              <span className="run-label-short">{running ? translate("run.buttonRunning", language) : starting ? translate("run.buttonStartingShort", language) : runLoading ? translate("run.buttonChecking", language) : translate("run.buttonStartShort", language)}</span>
            </button>
          )}
        </div>}
      </aside>
      <main className="main">
        {canRun && (
          <div className="mobile-run-bar">
            {canRun && (
              <button className="primary-button" disabled={!project || running || starting || runLoading} onClick={onRun}>
                {running ? translate("run.buttonRunning", language) : starting ? translate("run.buttonStarting", language) : runLoading ? translate("run.buttonChecking", language) : translate("run.buttonStartMobile", language)}
              </button>
            )}
          </div>
        )}
        {children}
      </main>
    </div>
  );
}
