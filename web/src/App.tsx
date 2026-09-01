import { useCallback, useEffect, useRef, useState } from "react";
import { api, onAuthRequired } from "./api";
import { AppShell } from "./components/AppShell";
import { SegmentWorkspace, prefetchWorkspace } from "./components/SegmentWorkspace";
import { TermsView, prefetchTerms } from "./components/TermsView";
import { CreateProjectDialog, ExportView, Overview } from "./components/UtilityViews";
import { SettingsView } from "./components/SettingsView";
import { LoginView } from "./components/ServerSettings";
import { RunDialog } from "./components/RunDialog";
import { DiagnosticsView } from "./components/DiagnosticsView";
import type {
  LLMStage,
  ProjectOverview,
  ProjectSummary,
  RunDecision,
  ServerStatus,
  SettingsField,
  Stage,
  TaskOptions,
  TaskState,
  ThemeMode,
} from "./types";
import { detectLanguage, errorMessage, translate, type Language } from "./i18n";
import { STORAGE_KEYS } from "./storageKeys";
import { isActiveTaskStatus, isTerminalTaskStatus, reconcileTaskCollection } from "./taskState";
import "./styles.css";

const THEME_STORAGE_KEY = STORAGE_KEYS.theme;
const RECENT_PROJECTS_STORAGE_KEY = STORAGE_KEYS.recentProjects;
const SELECTED_PROJECT_STORAGE_KEY = "another-llm-translator.selected-project.v1";
const runnable: Partial<Record<Stage, LLMStage>> = {
  terminology: "terminology",
  translation: "translation",
  proofreading: "proofreading",
  polishing: "polishing",
};

function readRecentProjectPaths(): string[] {
  try {
    const stored: unknown = JSON.parse(
      window.localStorage.getItem(RECENT_PROJECTS_STORAGE_KEY) ?? "[]",
    );
    return Array.isArray(stored)
      ? Array.from(new Set(stored.filter((value): value is string => typeof value === "string")))
      : [];
  } catch {
    return [];
  }
}

function writeRecentProjectPaths(paths: string[]) {
  try {
    window.localStorage.setItem(RECENT_PROJECTS_STORAGE_KEY, JSON.stringify(paths));
  } catch {
    // The projects remain available for this server session.
  }
}

function rememberProjectPath(path: string) {
  writeRecentProjectPaths([
    path,
    ...readRecentProjectPaths().filter((value) => value !== path),
  ]);
}

function readSelectedProjectId(): string {
  try {
    return window.localStorage.getItem(SELECTED_PROJECT_STORAGE_KEY) ?? "";
  } catch {
    return "";
  }
}

export default function App() {
  const [projects, setProjects] = useState<ProjectSummary[]>([]);
  const [project, setProject] = useState("");
  const [stage, setStage] = useState<Stage>("overview");
  const [pendingJump, setPendingJump] = useState<{
    search: string;
    segmentId: string;
  } | null>(null);
  const [overview, setOverview] = useState<ProjectOverview | null>(null);
  const [tasks, setTasks] = useState<Record<string, TaskState>>({});
  const [failureFocus, setFailureFocus] = useState<LLMStage | null>(null);
  const [settingsField, setSettingsField] = useState<SettingsField | null>(null);
  const [createOpen, setCreateOpen] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const [warningDismissed, setWarningDismissed] = useState(false);
  const [runOptions, setRunOptions] = useState<TaskOptions | null>(null);
  const [runOptionsLoading, setRunOptionsLoading] = useState(false);
  const [starting, setStarting] = useState(false);
  const [themeMode, setThemeMode] = useState<ThemeMode>(() => {
    try {
      const stored = window.localStorage.getItem(THEME_STORAGE_KEY);
      return stored === "light" || stored === "dark" || stored === "system"
        ? stored
        : "system";
    } catch {
      return "system";
    }
  });
  const [language, setLanguage] = useState<Language>(detectLanguage);
  const [serverStatus, setServerStatus] = useState<ServerStatus | null>(null);
  const [welcomeOpen, setWelcomeOpen] = useState(false);
  const tasksRef = useRef<Record<string, TaskState>>({});
  const syncingTasksRef = useRef(false);
  const consumeSettingsFocus = useCallback(() => setSettingsField(null), []);
  const selectedProject = projects.find((item) => item.selector === project) ?? null;
  const task = selectedProject ? tasks[selectedProject.project_id] ?? null : null;
  const runningProjectIds = new Set(
    Object.values(tasks)
      .filter((item) => isActiveTaskStatus(item.status))
      .map((item) => item.project_id),
  );

  useEffect(() => {
    tasksRef.current = tasks;
  }, [tasks]);

  const updateTask = useCallback((next: TaskState | null) => {
    setTasks((current) => {
      if (next === null) {
        if (!selectedProject) return current;
        const updated = { ...current };
        delete updated[selectedProject.project_id];
        return updated;
      }
      return { ...current, [next.project_id]: next };
    });
  }, [selectedProject]);

  const syncActiveTasks = useCallback(async () => {
    if (syncingTasksRef.current) return;
    syncingTasksRef.current = true;
    try {
      const value = await api<{ tasks: TaskState[] }>("/api/v1/tasks/active");
      const missing = Object.values(tasksRef.current).filter(
        (item) => isActiveTaskStatus(item.status)
          && !value.tasks.some((next) => next.task_id === item.task_id),
      );
      const terminalResults = await Promise.allSettled(
        missing.map((item) => api<TaskState>(`/api/v1/tasks/${item.task_id}`)),
      );
      setTasks((current) => {
        const terminal = Object.fromEntries(missing.map((item, index) => {
          const result = terminalResults[index];
          return [item.task_id, result.status === "fulfilled" ? result.value : null];
        }));
        const updated = reconcileTaskCollection(current, value.tasks, missing, terminal);
        tasksRef.current = updated;
        return updated;
      });
    } finally {
      syncingTasksRef.current = false;
    }
  }, []);

  useEffect(() => {
    let active = true;
    const remove = onAuthRequired(() => {
      if (active) setServerStatus((current) => current ? { ...current, authed: false } : current);
    });
    void api<ServerStatus>("/api/v1/server/status").then((value) => {
      if (active) setServerStatus(value);
    }).catch(() => {
      // The status endpoint is public; failure here leaves the app on the
      // normal flow and the next 401 surfaces the login gate.
    });
    return () => { active = false; remove(); };
  }, []);

  useEffect(() => {
    let active = true;
    void api<{ first: boolean }>("/api/v1/welcome").then((value) => {
      if (active && value.first) setWelcomeOpen(true);
    }).catch(() => {
      // The welcome endpoint needs auth on LAN; leave the modal hidden.
    });
    return () => { active = false; };
  }, []);

  useEffect(() => {
    document.documentElement.lang = language;
    document.title = translate("brand", language);
    try {
      window.localStorage.setItem(STORAGE_KEYS.language, language);
    } catch {
      // The selected language still applies for this page when storage is unavailable.
    }
  }, [language]);

  useEffect(() => {
    if (!selectedProject) return;
    try {
      window.localStorage.setItem(SELECTED_PROJECT_STORAGE_KEY, selectedProject.project_id);
    } catch {
      // The selected project still applies for this page when storage is unavailable.
    }
  }, [selectedProject]);

  useEffect(() => {
    const media = window.matchMedia("(prefers-color-scheme: dark)");
    const applyTheme = () => {
      const resolved = themeMode === "system"
        ? media.matches ? "dark" : "light"
        : themeMode;
      document.documentElement.dataset.theme = resolved;
      document.documentElement.dataset.themeMode = themeMode;
    };
    try {
      window.localStorage.setItem(THEME_STORAGE_KEY, themeMode);
    } catch {
      // The selected theme still applies for this page when storage is unavailable.
    }
    applyTheme();
    if (themeMode === "system") media.addEventListener("change", applyTheme);
    return () => media.removeEventListener("change", applyTheme);
  }, [themeMode]);

  const loadProjects = useCallback(async () => {
    const value = await api<{ projects: ProjectSummary[] }>("/api/v1/projects");
    setProjects(value.projects);
    await syncActiveTasks();
    const storedProjectId = readSelectedProjectId();
    setProject((current) => {
      if (current && value.projects.some((item) => item.selector === current)) return current;
      return value.projects.find((item) => item.project_id === storedProjectId)?.selector
        ?? value.projects[0]?.selector
        ?? "";
    });
    return value.projects;
  }, [syncActiveTasks]);

  const refresh = useCallback(async () => {
    if (!project) { setOverview(null); return; }
    // The shell only needs project totals and file metadata. Segment rows are
    // loaded by SegmentWorkspace in bounded windows, so do not fetch a second
    // full page just to refresh the summary after an edit.
    setOverview(await api<ProjectOverview>(`/api/v1/projects/${project}?offset=0&limit=1`));
  }, [project]);

  const refreshProject = useCallback(async () => {
    await Promise.all([loadProjects(), refresh()]);
  }, [loadProjects, refresh]);

  useEffect(() => {
    const paths = readRecentProjectPaths();
    void Promise.allSettled(paths.map((path) => api<{ path: string }>(
      "/api/v1/projects/open",
      { method: "POST", body: JSON.stringify({ path }) },
    ))).then(async (results) => {
      const validPaths = results.flatMap((result) => (
        result.status === "fulfilled" ? [result.value.path] : []
      ));
      writeRecentProjectPaths(validPaths);
      const failures = results.length - validPaths.length;
      if (failures) setError(translate("app.recentPathsInvalid", language, { count: failures }));
      await loadProjects();
    }).catch((value) => setError(value));
  }, []);
  useEffect(() => { void refresh().catch((value) => setError(value)); }, [refresh]);
  // Warm the terminology and segment head caches when a project is opened so
  // the first visit to those pages renders instantly; the pages restore the
  // cached data synchronously and refresh it in the background.
  useEffect(() => {
    if (!project) return;
    prefetchTerms(project);
    prefetchWorkspace(project);
  }, [project]);
  useEffect(() => {
    let active = true;
    const poll = () => {
      void syncActiveTasks().catch((value) => {
        if (active) setError(value);
      });
    };
    poll();
    const timer = window.setInterval(poll, 800);
    return () => {
      active = false;
      window.clearInterval(timer);
    };
  }, [syncActiveTasks]);
  useEffect(() => {
    if (!task || isActiveTaskStatus(task.status)) return;
    void refresh().catch((value) => setError(value));
  }, [task?.task_id, task?.status, refresh]);

  async function openRunDialog() {
    const taskStage = runnable[stage];
    if (!project || !taskStage) return;
    setRunOptionsLoading(true);
    try {
      setRunOptions(await api<TaskOptions>(
        `/api/v1/projects/${project}/task-options/${taskStage}`,
      ));
    } catch (value) {
      setError(value);
    } finally {
      setRunOptionsLoading(false);
    }
  }

  async function startRun(decision: RunDecision) {
    if (!project || !runOptions) return;
    setRunOptions(null);
    setStarting(true);
    try {
      updateTask(await api<TaskState>(`/api/v1/projects/${project}/tasks`, {
        method: "POST",
        body: JSON.stringify({ stage: runOptions.stage, language, ...decision }),
      }));
    } catch (value) {
      setError(value);
      try {
        setRunOptions(await api<TaskOptions>(
          `/api/v1/projects/${project}/task-options/${runOptions.stage}`,
        ));
      } catch {
        setRunOptions(null);
      }
    } finally {
      setStarting(false);
    }
  }

  async function cancelRun() {
    if (!task) return;
    updateTask(await api<TaskState>(`/api/v1/tasks/${task.task_id}/cancel`, { method: "POST" }));
  }

  async function cancelTask(taskId: string) {
    try {
      updateTask(await api<TaskState>(`/api/v1/tasks/${taskId}/cancel`, { method: "POST" }));
    } catch (value) {
      setError(value);
    }
  }

  function dismissTask(taskId: string) {
    setTasks((current) => {
      const entry = Object.entries(current).find(([, item]) => item.task_id === taskId);
      if (!entry || !isTerminalTaskStatus(entry[1].status)) return current;
      const updated = { ...current };
      delete updated[entry[0]];
      return updated;
    });
  }

  async function openTaskProject(next: TaskState) {
    let summary = projects.find((item) => item.project_id === next.project_id);
    if (!summary) {
      try {
        const available = await loadProjects();
        summary = available.find((item) => item.project_id === next.project_id);
      } catch (value) {
        setError(value);
        return;
      }
    }
    if (!summary) {
      setError(translate("run.projectUnavailable", language));
      return;
    }
    setProject(summary.selector);
    const destination = next.stage === "terminology_decision"
      ? "terminology"
      : ["terminology", "translation", "proofreading", "polishing"].includes(next.stage)
        ? next.stage as Stage
        : "overview";
    setStage(destination);
    setFailureFocus(null);
  }

  async function handleProjectDeleted(path: string) {
    writeRecentProjectPaths(readRecentProjectPaths().filter((value) => value !== path));
    setProject("");
    setOverview(null);
    if (selectedProject) {
      setTasks((current) => {
        const updated = { ...current };
        delete updated[selectedProject.project_id];
        return updated;
      });
    }
    setFailureFocus(null);
    setSettingsField(null);
    await loadProjects();
  }

  function navigateStage(value: Stage) {
    if (value !== "settings") setSettingsField(null);
    setStage(value);
    setFailureFocus(null);
  }

  function openSettingsField(field: SettingsField) {
    setSettingsField(field);
    setStage("settings");
    setFailureFocus(null);
  }

  function showFailures() {
    const target = runnable[stage] ?? (
      task && runnable[task.stage as Stage] ? runnable[task.stage as Stage] : null
    );
    if (!target) return;
    setFailureFocus(target);
    setStage(target as Stage);
  }

  function jumpToSegment(source: string, segmentId: string) {
    setPendingJump({ search: source, segmentId });
    setFailureFocus(null);
    setStage("translation");
  }

  let content = <div className="empty-page">{translate("app.selectOrCreate", language)}</div>;
  if (stage === "diagnostics") content = <DiagnosticsView language={language} />;
    else if (stage === "settings") content = <SettingsView project={project} language={language} focusField={settingsField} onFocusConsumed={consumeSettingsFocus} />;
  else if (stage === "overview") content = (
    <Overview
      projects={projects}
      project={project}
      value={overview}
      onProject={setProject}
      runningProjectIds={runningProjectIds}
      onCreate={() => setCreateOpen(true)}
      onFilesChanged={refreshProject}
      onDeleted={handleProjectDeleted}
      language={language}
    />
  );
  else if (project && overview) {
    if (stage === "terminology") content = <TermsView project={project} focusFailures={failureFocus === "terminology"} language={language} onFindSegment={jumpToSegment} task={task} onTask={updateTask} />;
    else if (stage === "translation" || stage === "proofreading" || stage === "polishing") {
      content = <SegmentWorkspace project={project} stage={stage} overview={overview} onRefresh={refresh} focusFailures={failureFocus === stage} language={language} pendingJump={pendingJump} onJumpConsumed={() => setPendingJump(null)} />;
    } else if (stage === "export") content = <ExportView project={project} overview={overview} language={language} onNavigateStage={navigateStage} onOpenSettings={openSettingsField} />;
  }

  if (serverStatus?.auth.required && !serverStatus.authed) {
    return (
      <LoginView
        language={language}
        onLoggedIn={() => {
          setError(null);
          setWarningDismissed(false);
          setServerStatus((current) => current ? { ...current, authed: true } : current);
          void loadProjects().catch((value) => setError(value));
        }}
      />
    );
  }

  return (
    <>
      <AppShell
        project={project}
        stage={stage}
        task={task}
        tasks={Object.values(tasks).filter((item) => isActiveTaskStatus(item.status))}
        onOpenTaskProject={(next) => { void openTaskProject(next); }}
        onCancelTask={cancelTask}
        onDismissTask={dismissTask}
        onStage={navigateStage}
        onShowFailures={showFailures}
        onRun={openRunDialog}
        onCancel={cancelRun}
        canRun={Boolean(runnable[stage] && overview?.nonempty_segment_count)}
        runLoading={runOptionsLoading}
        starting={starting}
        themeMode={themeMode}
        onTheme={() => setThemeMode((current) => current === "system" ? "light" : current === "light" ? "dark" : "system")}
        language={language}
        onLanguage={() => setLanguage((current) => {
          const next = current === "zh-CN" ? "en" : "zh-CN";
          try {
      window.localStorage.setItem(STORAGE_KEYS.language, next);
          } catch {
            // The selected language still applies for this page when storage is unavailable.
          }
          return next;
        })}
      >
        {serverStatus?.lan.enabled && !serverStatus.auth.required && !warningDismissed && (
          <button className="warning-banner warning-banner-sticky" onClick={() => setWarningDismissed(true)}>{translate("server.warningEnabled", language)}</button>
        )}
        {error != null ? <button className="error-banner" onClick={() => setError(null)}>{errorMessage(error, language)}</button> : null}
        {content}
      </AppShell>
      {createOpen && <CreateProjectDialog language={language} onClose={() => setCreateOpen(false)} onCreated={async (selector, path) => { setCreateOpen(false); if (path) rememberProjectPath(path); await loadProjects(); setProject(selector); }} />}
      {welcomeOpen && (
        <div className="welcome-overlay" role="dialog" aria-modal="true">
          <div className="welcome-card">
            <h1>{translate("welcome.title", language)}</h1>
            <p className="muted">{translate("welcome.subtitle", language)}</p>
            <ul className="welcome-list">
              <li>{translate("welcome.userData", language)}</li>
              <li>{translate("welcome.credentials", language)}</li>
              <li>{translate("welcome.lan", language)}</li>
            </ul>
            <div className="button-group">
              <button className="primary-button" onClick={() => { void api("/api/v1/welcome/dismiss", { method: "POST" }).catch(() => {}); setWelcomeOpen(false); }}>{translate("welcome.getStarted", language)}</button>
              <button className="quiet-button" onClick={() => { void api("/api/v1/welcome/dismiss", { method: "POST" }).catch(() => {}); setWelcomeOpen(false); }}>{translate("welcome.skip", language)}</button>
            </div>
          </div>
        </div>
      )}
      {runOptions && (
        <RunDialog
          key={`${runOptions.stage}-${runOptions.running_run?.run_id ?? "new"}-${runOptions.mismatched_fingerprint_completed}`}
          options={runOptions}
          language={language}
          onClose={() => setRunOptions(null)}
          onStart={startRun}
        />
      )}
    </>
  );
}
