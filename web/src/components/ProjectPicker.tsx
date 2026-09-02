import { useEffect, useRef, useState, useId, type DragEvent as ReactDragEvent, type KeyboardEvent as ReactKeyboardEvent, type RefObject } from "react";
import { api, apiErrorFromResponse, errorPayloadFrom } from "../api";
import { nativeBridgeAvailable, pickNativeFile, pickNativeFolder, saveExport } from "../native";
import { moveFileBlock, moveFilesByCommand, type DropPosition, type FileMoveCommand } from "../fileOrder";
import { useClassicSelection } from "../useClassicSelection";
import { errorMessage, formatErrorPayload, translate, type Language } from "../i18n";
import type { ProjectSummary } from "../types";

export function ProjectBar({
  projects,
  project,
  runningProjectIds,
  onProject,
  onCreate,
  language,
}: {
  projects: ProjectSummary[];
  project: string;
  runningProjectIds: ReadonlySet<string>;
  onProject: (value: string) => void;
  onCreate: () => void;
  language: Language;
}) {
  return (
    <div className="overview-project-bar">
      <ProjectPicker projects={projects} project={project} runningProjectIds={runningProjectIds} onProject={onProject} language={language} />
      <button className="quiet-button" onClick={onCreate}>{translate("project.create", language)}</button>
    </div>
  );
}

export function ProjectPicker({
  projects,
  project,
  runningProjectIds,
  onProject,
  language,
}: {
  projects: ProjectSummary[];
  project: string;
  runningProjectIds: ReadonlySet<string>;
  onProject: (value: string) => void;
  language: Language;
}) {
  const rootRef = useRef<HTMLDivElement>(null);
  const searchRef = useRef<HTMLInputElement>(null);
  const listId = useId();
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState("");
  const [activeIndex, setActiveIndex] = useState(-1);
  const selected = projects.find((item) => item.selector === project) ?? null;
  const selectedRunning = Boolean(selected && runningProjectIds.has(selected.project_id));
  const otherRunning = projects.filter((item) => item.selector !== project && runningProjectIds.has(item.project_id)).length;
  const normalizedQuery = query.trim().toLocaleLowerCase();
  const filteredProjects = normalizedQuery
    ? projects.filter((item) => (
      item.name.toLocaleLowerCase().includes(normalizedQuery)
      || item.path.toLocaleLowerCase().includes(normalizedQuery)
    ))
    : projects;

  useEffect(() => {
    function closeOnOutsideClick(event: PointerEvent) {
      if (!rootRef.current?.contains(event.target as Node)) closePicker();
    }
    document.addEventListener("pointerdown", closeOnOutsideClick);
    return () => document.removeEventListener("pointerdown", closeOnOutsideClick);
  }, []);

  useEffect(() => {
    if (!open) return;
    const selectedIndex = filteredProjects.findIndex((item) => item.selector === project);
    setActiveIndex(selectedIndex >= 0 ? selectedIndex : filteredProjects.length > 0 ? 0 : -1);
  }, [open, query, project, projects]);

  function closePicker() {
    setOpen(false);
    setQuery("");
  }

  function openPicker() {
    setOpen(true);
    window.setTimeout(() => searchRef.current?.focus(), 0);
  }

  function choose(item: ProjectSummary) {
    onProject(item.selector);
    closePicker();
  }

  function moveActive(direction: 1 | -1) {
    if (!open) openPicker();
    if (!filteredProjects.length) return;
    setActiveIndex((current) => {
      const start = current < 0 ? (direction > 0 ? -1 : 0) : current;
      return (start + direction + filteredProjects.length) % filteredProjects.length;
    });
  }

  function handleKeys(event: ReactKeyboardEvent<HTMLInputElement | HTMLButtonElement>) {
    if (event.key === "Escape") {
      closePicker();
      event.preventDefault();
      return;
    }
    if (event.key === "ArrowDown" || event.key === "ArrowUp") {
      moveActive(event.key === "ArrowDown" ? 1 : -1);
      event.preventDefault();
      return;
    }
    if (event.key === "Enter" && open && activeIndex >= 0) {
      const item = filteredProjects[activeIndex];
      if (item) choose(item);
      event.preventDefault();
    }
  }

  const activeOptionId = open && activeIndex >= 0
    ? `${listId}-option-${activeIndex}`
    : undefined;

  return (
    <div className="project-picker" ref={rootRef}>
      <button
        type="button"
        className="project-picker-trigger"
        role="combobox"
        aria-label={translate("project.select", language)}
        aria-expanded={open}
        aria-controls={listId}
        aria-activedescendant={activeOptionId}
        onClick={() => (open ? closePicker() : openPicker())}
        onKeyDown={handleKeys}
      >
        <span className="project-picker-value">
          <strong>{selected?.name ?? translate("project.select", language)}</strong>
          {selected && <small>{selected.path}</small>}
        </span>
        {(selectedRunning || otherRunning > 0) && <span className="project-picker-status" aria-label={selectedRunning ? translate("project.running", language) : translate("project.otherRunning", language, { count: otherRunning })}>
          {selectedRunning ? translate("project.running", language) : translate("project.otherRunning", language, { count: otherRunning })}
        </span>}
        <span className="project-picker-chevron" aria-hidden="true">⌄</span>
      </button>
      {open && (
        <div className="project-picker-popover">
          <div className="project-search">
            <input
              ref={searchRef}
              aria-label={translate("project.search", language)}
              placeholder={translate("project.searchPlaceholder", language)}
              value={query}
              onKeyDown={handleKeys}
              onChange={(event) => setQuery(event.target.value)}
            />
          </div>
          <div className="project-options" id={listId} role="listbox" aria-label={translate("project.select", language)}>
            {filteredProjects.length === 0 && (
              <div className="project-picker-state" role="status">{translate("project.noMatch", language)}</div>
            )}
            {filteredProjects.map((item, index) => (
              <button
                type="button"
                key={item.selector}
                id={`${listId}-option-${index}`}
                role="option"
                aria-selected={item.selector === project}
                className={`project-option${index === activeIndex ? " active" : ""}${item.selector === project ? " selected" : ""}`}
                onMouseEnter={() => setActiveIndex(index)}
                onClick={() => choose(item)}
              >
                <span>
                  <strong>{item.name}</strong>
                  <small title={item.path}>{item.path}</small>
                </span>
                <span className="project-option-meta">
                  {runningProjectIds.has(item.project_id) && <small className="project-running-badge">{translate("project.running", language)}</small>}
                  {item.selector === project && <span className="project-option-check" aria-hidden="true">✓</span>}
                </span>
              </button>
            ))}
          </div>
        </div>
      )}
    </div>
  );
}
