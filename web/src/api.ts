import { translateError } from "./i18n";

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
    const localized = typeof code === "string"
      ? translateError(code, params)
      : null;
    throw new Error(
      localized || value?.error || `请求失败：${response.status}`,
    );
  }
  return value as T;
}
