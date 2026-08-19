const STORAGE_NAMESPACE = "another-llm-translator";
const LEGACY_STORAGE_NAMESPACE = "minimal-llm-translator";

export const STORAGE_KEYS = {
  theme: `${STORAGE_NAMESPACE}.theme.v1`,
  recentProjects: `${STORAGE_NAMESPACE}.recent-projects.v1`,
  language: `${STORAGE_NAMESPACE}.language.v1`,
  throughput: `${STORAGE_NAMESPACE}.throughput.v1`,
} as const;

const LEGACY_STORAGE_KEYS = {
  theme: `${LEGACY_STORAGE_NAMESPACE}.theme.v1`,
  recentProjects: `${LEGACY_STORAGE_NAMESPACE}.recent-projects.v1`,
  language: `${LEGACY_STORAGE_NAMESPACE}.language.v1`,
  throughput: `${LEGACY_STORAGE_NAMESPACE}.throughput.v1`,
} as const;

export function migrateLegacyLocalStorage(storage: Storage): void {
  for (const key of Object.keys(STORAGE_KEYS) as Array<keyof typeof STORAGE_KEYS>) {
    const currentKey = STORAGE_KEYS[key];
    if (storage.getItem(currentKey) !== null) {
      continue;
    }
    const legacyKey = LEGACY_STORAGE_KEYS[key];
    const value = storage.getItem(legacyKey);
    if (value === null) {
      continue;
    }
    storage.setItem(currentKey, value);
    storage.removeItem(legacyKey);
  }
}
