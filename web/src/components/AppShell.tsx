import { useLayoutEffect, useRef, useState, type CSSProperties, type ReactNode } from "react";
import type { Stage, TaskState, ThemeMode } from "../types";
import { icons } from "./Icons";
import type { Language } from "../i18n";
import { errorMessage, translate } from "../i18n";
import { canCancelTaskStatus } from "../taskState";

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
  tasks,
  onOpenTaskProject,
  onCancelTask,
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
  tasks: TaskState[];
  onOpenTaskProject: (task: TaskState) => void | Promise<void>;
  onCancelTask: (taskId: string) => void;
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
  const runStatusRef = useRef<HTMLElement>(null);
  const [runStatusHeight, setRunStatusHeight] = useState(0);
  const [taskPanelOpen, setTaskPanelOpen] = useState(false);
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
  useLayoutEffect(() => {
    const element = runStatusRef.current;
    if (!element) {
      setRunStatusHeight(0);
      return;
    }
    const update = () => setRunStatusHeight(element.getBoundingClientRect().height);
    update();
    if (typeof ResizeObserver === "undefined") return;
    const observer = new ResizeObserver(update);
    observer.observe(element);
    return () => observer.disconnect();
  }, [Boolean(task)]);
  const appStyle = {
    "--mobile-run-status-height": `${runStatusHeight}px`,
  } as CSSProperties;
  return (
    <div className={`app${task ? " has-run-status" : ""}`} style={appStyle}>
      <header className="topbar">
        <div className="brand">{translate("brand", language)}</div>
        <div className="topbar-spacer" />
        <div className="task-panel-wrap">
          <button
            className={`task-panel-trigger${tasks.length ? " has-tasks" : ""}`}
            aria-expanded={taskPanelOpen}
            aria-label={translate("run.taskPanel", language, { count: tasks.length })}
            onClick={() => setTaskPanelOpen((current) => !current)}
          >
            {translate("run.taskPanel", language, { count: tasks.length })}
          </button>
          {taskPanelOpen && (
            <div className="task-panel-popover" role="dialog" aria-label={translate("run.taskPanelTitle", language)}>
              <div className="task-panel-heading">
                <strong>{translate("run.taskPanelTitle", language)}</strong>
                <span>{tasks.length}</span>
              </div>
              {tasks.length === 0 ? (
                <p className="task-panel-empty">{translate("run.taskPanelEmpty", language)}</p>
              ) : (
                <div className="task-panel-list">
                  {tasks.map((next) => {
                    const nextCompleted = next.completed_segments;
                    const nextFailed = next.failed_segments;
                    const nextPending = next.pending_segments;
                    const nextTotal = next.total_segments;
                    const nextProcessed = nextCompleted + nextFailed;
                    const nextStage = next.stage === "terminology_decision"
                      ? "stage.terminologyDecision"
                      : `stage.${next.stage}`;
                    return (
                      <article className="task-panel-item" key={next.task_id}>
                        <div className="task-panel-identity">
                          <strong>{next.project}</strong>
                          <span>{translate(nextStage, language)} · {statusLabels[next.status] ?? next.status}</span>
                        </div>
                        <div className="task-panel-progress">
                          <span>{translate("run.completedCount", language, {
                            completed: nextCompleted,
                            failed: nextFailed,
                            pending: nextPending,
                            total: nextTotal,
                          })}</span>
                          <div className="progress-track" role="progressbar" aria-label={translate("shell.taskProgress", language)} aria-valuemin={0} aria-valuemax={nextTotal} aria-valuenow={nextProcessed}>
                            <span className="progress-completed" style={{ width: `${nextTotal ? nextCompleted / nextTotal * 100 : 0}%` }} />
                            <span className="progress-failed" style={{ width: `${nextTotal ? nextFailed / nextTotal * 100 : 0}%` }} />
                          </div>
                        </div>
                        <div className="task-panel-tokens">
                          {next.usage.available ? (
                            <><span>{translate("run.tokensInput", language)} {next.usage.input_tokens}</span><span>{translate("run.tokensOutput", language)} {next.usage.output_tokens}</span></>
                          ) : next.usage.partial ? (
                            <span>{translate("run.tokensPartial", language, { input: next.usage.input_tokens, output: next.usage.output_tokens })}</span>
                          ) : (
                            <span>{translate("run.tokensUnavailable", language)}</span>
                          )}
                        </div>
                        <div className="task-panel-actions">
                          <button className="quiet-button" onClick={() => { setTaskPanelOpen(false); void onOpenTaskProject(next); }}>
                            {translate("run.openProject", language)}
                          </button>
                          {canCancelTaskStatus(next.status) && (
                            <button className="danger-link" onClick={() => onCancelTask(next.task_id)}>
                              {translate("run.cancel", language)}
                            </button>
                          )}
                        </div>
                      </article>
                    );
                  })}
                </div>
              )}
            </div>
          )}
        </div>
        <button className="icon-button" aria-label={themeTitle} title={themeTitle} onClick={onTheme}>
          {themeIcon}
        </button>
        <button className="icon-button" aria-label={translate("nav.settings", language)} onClick={() => onStage("settings")}>
          {icons.settings}
        </button>
        <button className="language-button" onClick={onLanguage}>{translate("language.switch", language)}</button>
      </header>
      {task && (
        <section className="global-run-status" ref={runStatusRef} aria-label={translate("shell.globalTaskStatus", language)}>
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
            ) : task.usage.partial ? <span>{translate("run.tokensPartial", language, { input: task.usage.input_tokens, output: task.usage.output_tokens })}</span>
              : <span>{translate("run.tokensUnavailable", language)}</span>}
          </div>
          {failed > 0 && <button className="run-failure-link" onClick={onShowFailures}>{translate("run.failedSegments", language, { count: failed })}</button>}
          {task.error && (
            <span
              className="error-text run-error"
              role="alert"
              title={errorMessage(task.error, language)}
            >
              {errorMessage(task.error, language)}
            </span>
          )}
          {canCancelTaskStatus(task.status) && <button className="danger-link" onClick={onCancel}>{translate("run.cancel", language)}</button>}
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
      <main className={`main${stage === "overview" ? " main-overview" : ""}`}>
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
