const STORAGE_NAMESPACE = "another-llm-translator";

export const STORAGE_KEYS = {
  theme: `${STORAGE_NAMESPACE}.theme.v1`,
  recentProjects: `${STORAGE_NAMESPACE}.recent-projects.v1`,
  language: `${STORAGE_NAMESPACE}.language.v1`,
  throughput: `${STORAGE_NAMESPACE}.throughput.v1`,
} as const;
