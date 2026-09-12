export interface RecentProjectAttempt {
  path: string;
  openedPath?: string;
  errorCode?: string;
}

export interface RecentProjectPathResult {
  paths: string[];
  invalidCount: number;
  transientFailureCount: number;
}

export function reconcileRecentProjectPaths(
  attempts: readonly RecentProjectAttempt[],
): RecentProjectPathResult {
  const paths: string[] = [];
  let invalidCount = 0;
  let transientFailureCount = 0;

  for (const attempt of attempts) {
    if (attempt.openedPath) {
      paths.push(attempt.openedPath);
    } else if (attempt.errorCode === "project_error") {
      invalidCount += 1;
    } else {
      paths.push(attempt.path);
      transientFailureCount += 1;
    }
  }

  return { paths, invalidCount, transientFailureCount };
}
