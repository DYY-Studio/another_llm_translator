import { useCallback, useEffect, useState } from "react";
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
  Stage,
  TaskOptions,
  TaskState,
  ThemeMode,
} from "./types";
import { detectLanguage, setUiLanguage, translate, type Language } from "./i18n";
import "./styles.css";

const THEME_STORAGE_KEY = "minimal-llm-translator.theme.v1";
const RECENT_PROJECTS_STORAGE_KEY = "minimal-llm-translator.recent-projects.v1";
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

export default function App() {
  const [projects, setProjects] = useState<ProjectSummary[]>([]);
  const [project, setProject] = useState("");
  const [stage, setStage] = useState<Stage>("overview");
  const [pendingJump, setPendingJump] = useState<{
    search: string;
    segmentId: string;
  } | null>(null);
  const [overview, setOverview] = useState<ProjectOverview | null>(null);
  const [task, setTask] = useState<TaskState | null>(null);
  const [failureFocus, setFailureFocus] = useState<LLMStage | null>(null);
  const [createOpen, setCreateOpen] = useState(false);
  const [error, setError] = useState("");
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
    setUiLanguage(language);
    document.documentElement.lang = language;
    document.title = translate("brand", language);
    try {
      window.localStorage.setItem("minimal-llm-translator.language.v1", language);
    } catch {
      // The selected language still applies for this page when storage is unavailable.
    }
  }, [language]);

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
    if (!project && value.projects[0]) setProject(value.projects[0].selector);
  }, [project]);

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
    }).catch((value) => setError(String(value)));
  }, []);
  useEffect(() => { void refresh().catch((value) => setError(String(value))); }, [refresh]);
  // Warm the terminology and segment head caches when a project is opened so
  // the first visit to those pages renders instantly; the pages restore the
  // cached data synchronously and refresh it in the background.
  useEffect(() => {
    if (!project) return;
    prefetchTerms(project);
    prefetchWorkspace(project);
  }, [project]);
  useEffect(() => {
    if (!task || !["queued", "running", "cancelling"].includes(task.status)) return;
    const timer = window.setInterval(() => {
      void api<TaskState>(`/api/v1/tasks/${task.task_id}`).then((value) => {
        setTask(value);
        if (!["queued", "running", "cancelling"].includes(value.status)) void refresh();
      });
    }, 800);
    return () => window.clearInterval(timer);
  }, [task, refresh]);

  async function openRunDialog() {
    const taskStage = runnable[stage];
    if (!project || !taskStage) return;
    setRunOptionsLoading(true);
    try {
      setRunOptions(await api<TaskOptions>(
        `/api/v1/projects/${project}/task-options/${taskStage}`,
      ));
    } catch (value) {
      setError(String(value));
    } finally {
      setRunOptionsLoading(false);
    }
  }

  async function startRun(decision: RunDecision) {
    if (!project || !runOptions) return;
    setRunOptions(null);
    setStarting(true);
    try {
      setTask(await api<TaskState>(`/api/v1/projects/${project}/tasks`, {
        method: "POST",
        body: JSON.stringify({ stage: runOptions.stage, language, ...decision }),
      }));
    } catch (value) {
      setError(String(value));
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
    setTask(await api<TaskState>(`/api/v1/tasks/${task.task_id}/cancel`, { method: "POST" }));
  }

  async function handleProjectDeleted(path: string) {
    writeRecentProjectPaths(readRecentProjectPaths().filter((value) => value !== path));
    setProject("");
    setOverview(null);
    setTask(null);
    setFailureFocus(null);
    await loadProjects();
  }

  function navigateStage(value: Stage) {
    setStage(value);
    setFailureFocus(null);
  }

  function showFailures() {
    const target = runnable[stage] ?? (
      task && runnable[task.stage as Stage] ? runnable[task.stage as Stage] : null
    );
    if (!target) return;
    setFailureFocus(target);
    setStage(target);
  }

  function jumpToSegment(source: string, segmentId: string) {
    setPendingJump({ search: source, segmentId });
    setFailureFocus(null);
    setStage("translation");
  }

  let content = <div className="empty-page">{translate("app.selectOrCreate", language)}</div>;
  if (stage === "diagnostics") content = <DiagnosticsView language={language} />;
    else if (stage === "settings") content = <SettingsView project={project} language={language} />;
  else if (stage === "overview") content = (
    <Overview
      projects={projects}
      project={project}
      value={overview}
      onProject={setProject}
      onCreate={() => setCreateOpen(true)}
      onFilesChanged={refreshProject}
      onDeleted={handleProjectDeleted}
      language={language}
    />
  );
  else if (project && overview) {
    if (stage === "terminology") content = <TermsView project={project} focusFailures={failureFocus === "terminology"} language={language} onFindSegment={jumpToSegment} />;
    else if (stage === "translation" || stage === "proofreading" || stage === "polishing") {
      content = <SegmentWorkspace project={project} stage={stage} overview={overview} onRefresh={refresh} focusFailures={failureFocus === stage} language={language} pendingJump={pendingJump} onJumpConsumed={() => setPendingJump(null)} />;
    } else if (stage === "export") content = <ExportView project={project} overview={overview} language={language} />;
  }

  if (serverStatus?.auth.required && !serverStatus.authed) {
    return (
      <LoginView
        language={language}
        onLoggedIn={() => {
          setError("");
          setWarningDismissed(false);
          setServerStatus((current) => current ? { ...current, authed: true } : current);
          void loadProjects().catch((value) => setError(String(value)));
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
            window.localStorage.setItem("minimal-llm-translator.language.v1", next);
          } catch {
            // The selected language still applies for this page when storage is unavailable.
          }
          return next;
        })}
      >
        {serverStatus?.lan.enabled && !serverStatus.auth.required && !warningDismissed && (
          <button className="warning-banner warning-banner-sticky" onClick={() => setWarningDismissed(true)}>{translate("server.warningEnabled", language)}</button>
        )}
        {error && <button className="error-banner" onClick={() => setError("")}>{error}</button>}
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
