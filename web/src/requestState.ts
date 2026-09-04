export function isCurrentProjectRequest(
  requestId: number,
  activeRequestId: number,
  requestProject: string,
  activeProject: string,
): boolean {
  return requestId === activeRequestId && requestProject === activeProject;
}

export function canAutoSelectProject(requestProject: string, activeProject: string): boolean {
  return activeProject === "" || requestProject === activeProject;
}
