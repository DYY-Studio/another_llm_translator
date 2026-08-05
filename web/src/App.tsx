import { useCallback, useEffect, useState } from "react";
import { api } from "./api";
import { AppShell } from "./components/AppShell";
import { SegmentWorkspace } from "./components/SegmentWorkspace";
import { TermsView } from "./components/TermsView";
import { CreateProjectDialog, ExportView, Overview } from "./components/UtilityViews";
import { SettingsView } from "./components/SettingsView";
import { RunDialog } from "./components/RunDialog";
import { DiagnosticsView } from "./components/DiagnosticsView";
import type {
  LLMStage,
  ProjectOverview,
  ProjectSummary,
  RunDecision,
  Stage,
  TaskOptions,
  TaskState,
  ThemeMode,
} from "./types";
import { detectLanguage, translate, type Language } from "./i18n";
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
  const [overview, setOverview] = useState<ProjectOverview | null>(null);
  const [task, setTask] = useState<TaskState | null>(null);
  const [failureFocus, setFailureFocus] = useState<LLMStage | null>(null);
  const [createOpen, setCreateOpen] = useState(false);
  const [error, setError] = useState("");
  const [runOptions, setRunOptions] = useState<TaskOptions | null>(null);
  const [runOptionsLoading, setRunOptionsLoading] = useState(false);
  const [runStarting, setRunStarting] = useState(false);
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

  useEffect(() => {
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
    setRunStarting(true);
    try {
      setTask(await api<TaskState>(`/api/v1/projects/${project}/tasks`, {
        method: "POST",
        body: JSON.stringify({ stage: runOptions.stage, ...decision }),
      }));
      setRunOptions(null);
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
      setRunStarting(false);
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

  let content = <div className="empty-page">{translate("app.selectOrCreate", language)}</div>;
  if (stage === "diagnostics") content = <DiagnosticsView language={language} />;
    else if (stage === "settings") content = <SettingsView project={project} language={language} />;
  else if (project && overview) {
    if (stage === "overview") content = (
      <Overview
        project={project}
        value={overview}
        onFilesChanged={refreshProject}
        onDeleted={handleProjectDeleted}
        language={language}
      />
    );
    else if (stage === "terminology") content = <TermsView project={project} focusFailures={failureFocus === "terminology"} language={language} />;
    else if (stage === "translation" || stage === "proofreading" || stage === "polishing") {
      content = <SegmentWorkspace project={project} stage={stage} overview={overview} onRefresh={refresh} focusFailures={failureFocus === stage} language={language} />;
    } else if (stage === "export") content = <ExportView project={project} overview={overview} language={language} />;
  }

  return (
    <>
      <AppShell
        projects={projects}
        project={project}
        stage={stage}
        task={task}
        onProject={setProject}
        onStage={navigateStage}
        onShowFailures={showFailures}
        onCreate={() => setCreateOpen(true)}
        onRun={openRunDialog}
        onCancel={cancelRun}
        canRun={Boolean(runnable[stage] && overview?.nonempty_segment_count)}
        runLoading={runOptionsLoading}
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
        {error && <button className="error-banner" onClick={() => setError("")}>{error}</button>}
        {content}
      </AppShell>
      {createOpen && <CreateProjectDialog language={language} onClose={() => setCreateOpen(false)} onCreated={async (selector, path) => { setCreateOpen(false); if (path) rememberProjectPath(path); await loadProjects(); setProject(selector); }} />}
      {runOptions && (
        <RunDialog
          key={`${runOptions.stage}-${runOptions.running_run?.run_id ?? "new"}-${runOptions.mismatched_fingerprint_completed}`}
          options={runOptions}
          language={language}
          starting={runStarting}
          onClose={() => setRunOptions(null)}
          onStart={startRun}
        />
      )}
    </>
  );
}
