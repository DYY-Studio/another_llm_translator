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
  const value = await response.json();
  if (!response.ok) {
    throw new Error(value.error || `请求失败：${response.status}`);
  }
  return value as T;
}
