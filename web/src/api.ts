import { translateError } from "./i18n";

const authRequiredHandlers = new Set<() => void>();

export function onAuthRequired(handler: () => void): () => void {
  authRequiredHandlers.add(handler);
  return () => authRequiredHandlers.delete(handler);
}

function notifyAuthRequired() {
  for (const handler of authRequiredHandlers) handler();
}

export async function api<T>(
  path: string,
  options: RequestInit = {},
): Promise<T> {
  const response = await fetch(path, {
    ...options,
    headers: options.body instanceof FormData
      ? options.headers
      : { "Content-Type": "application/json", ...options.headers },
  });
  const value = await response.json().catch(() => null);
  if (!response.ok) {
    const code: unknown = value?.code;
    const params: Record<string, unknown> = value?.params ?? {};
    if (response.status === 401 && code === "auth_required") {
      notifyAuthRequired();
    }
    const localized = typeof code === "string"
      ? translateError(code, params)
      : null;
    throw new Error(
      localized || value?.error || `请求失败：${response.status}`,
    );
  }
  return value as T;
}
