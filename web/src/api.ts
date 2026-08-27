import type { ErrorPayload } from "./types";

const authRequiredHandlers = new Set<() => void>();

export function onAuthRequired(handler: () => void): () => void {
  authRequiredHandlers.add(handler);
  return () => authRequiredHandlers.delete(handler);
}

function notifyAuthRequired() {
  for (const handler of authRequiredHandlers) handler();
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

export class ApiError extends Error {
  readonly status: number;
  readonly payload: ErrorPayload;

  constructor(status: number, payload: ErrorPayload) {
    super(payload.error || `Request failed: ${status}`);
    this.name = "ApiError";
    this.status = status;
    this.payload = payload;
  }
}

export function parseErrorPayload(
  value: unknown,
  status: number,
): ErrorPayload {
  const record = isRecord(value) ? value : {};
  const code = typeof record.code === "string" && record.code
    ? record.code
    : "http_error";
  const params = isRecord(record.params) ? { ...record.params } : {};
  if (code === "http_error" && !("status" in params)) params.status = status;
  return {
    code,
    params,
    error: typeof record.error === "string" ? record.error : "",
  };
}

export async function apiErrorFromResponse(response: Response): Promise<ApiError> {
  const value: unknown = await response.json().catch(() => null);
  return new ApiError(response.status, parseErrorPayload(value, response.status));
}

export function errorPayloadFrom(reason: unknown): ErrorPayload | null {
  if (reason instanceof ApiError) return reason.payload;
  if (!isRecord(reason) || typeof reason.code !== "string") return null;
  if (!isRecord(reason.params) || typeof reason.error !== "string") return null;
  return {
    code: reason.code,
    params: reason.params,
    error: reason.error,
  };
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
  if (!response.ok) {
    const error = await apiErrorFromResponse(response);
    if (response.status === 401 && error.payload.code === "auth_required") {
      notifyAuthRequired();
    }
    throw error;
  }
  const value = await response.json().catch(() => null);
  return value as T;
}
