import { useCallback, useEffect, useState } from "react";
import { api } from "./api";
import { AppShell } from "./components/AppShell";
import { SegmentWorkspace } from "./components/SegmentWorkspace";
import { TermsView } from "./components/TermsView";
import { CreateProjectDialog, ExportView, Overview } from "./components/UtilityViews";
import { SettingsView } from "./components/SettingsView";
import type { ProjectOverview, ProjectSummary, Stage, TaskState, ThemeMode } from "./types";
import "./styles.css";

const THEME_STORAGE_KEY = "minimal-llm-translator.theme.v1";
const runnable: Partial<Record<Stage, string>> = {
  terminology: "terminology",
  translation: "translation",
  proofreading: "proofreading",
  polishing: "polishing",
};

export default function App() {
  const [projects, setProjects] = useState<ProjectSummary[]>([]);
  const [project, setProject] = useState("");
  const [stage, setStage] = useState<Stage>("overview");
  const [overview, setOverview] = useState<ProjectOverview | null>(null);
  const [task, setTask] = useState<TaskState | null>(null);
  const [createOpen, setCreateOpen] = useState(false);
  const [error, setError] = useState("");
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
    if (!project && value.projects[0]) setProject(value.projects[0].name);
  }, [project]);

  const refresh = useCallback(async () => {
    if (!project) { setOverview(null); return; }
    setOverview(await api<ProjectOverview>(`/api/v1/projects/${project}`));
  }, [project]);

  useEffect(() => { void loadProjects().catch((value) => setError(String(value))); }, []);
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

  async function startRun() {
    const taskStage = runnable[stage];
    if (!project || !taskStage) return;
    try {
      setTask(await api<TaskState>(`/api/v1/projects/${project}/tasks`, {
        method: "POST",
        body: JSON.stringify({ stage: taskStage, reuse_mixed_fingerprints: false }),
      }));
    } catch (value) { setError(String(value)); }
  }

  async function cancelRun() {
    if (!task) return;
    setTask(await api<TaskState>(`/api/v1/tasks/${task.task_id}/cancel`, { method: "POST" }));
  }

  let content = <div className="empty-page">选择或创建项目后开始工作。</div>;
  if (project && overview) {
    if (stage === "overview") content = <Overview value={overview} />;
    else if (stage === "terminology") content = <TermsView project={project} />;
    else if (stage === "translation" || stage === "proofreading" || stage === "polishing") {
      content = <SegmentWorkspace project={project} stage={stage} overview={overview} onRefresh={refresh} />;
    } else if (stage === "export") content = <ExportView project={project} />;
    else if (stage === "settings") content = <SettingsView project={project} />;
  }

  return (
    <>
      <AppShell
        projects={projects}
        project={project}
        stage={stage}
        task={task}
        onProject={setProject}
        onStage={setStage}
        onCreate={() => setCreateOpen(true)}
        onRun={startRun}
        onCancel={cancelRun}
        themeMode={themeMode}
        onTheme={() => setThemeMode((current) => current === "system" ? "light" : current === "light" ? "dark" : "system")}
      >
        {error && <button className="error-banner" onClick={() => setError("")}>{error}</button>}
        {content}
      </AppShell>
      {createOpen && <CreateProjectDialog onClose={() => setCreateOpen(false)} onCreated={async (name) => { setCreateOpen(false); await loadProjects(); setProject(name); }} />}
    </>
  );
}
