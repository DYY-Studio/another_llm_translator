import { ApiError, errorPayloadFrom } from "./api";

declare global {
  interface Window {
    __TAURI__?: {
      core: {
        invoke: (command: string, args?: Record<string, unknown>) => Promise<unknown>;
      };
    };
  }
}

export function nativeBridgeAvailable(): boolean {
  return typeof window.__TAURI__?.core?.invoke === "function";
}

async function pick(command: string): Promise<string | null> {
  const value = await window.__TAURI__!.core.invoke(command);
  return typeof value === "string" ? value : null;
}

export function pickNativeFile(): Promise<string | null> {
  return pick("select_file");
}

export function pickNativeFolder(): Promise<string | null> {
  return pick("select_folder");
}

export async function saveExport(
  path: string,
  filename: string,
  body?: string,
): Promise<string> {
  try {
    return await window.__TAURI__!.core.invoke("save_export", {
      path,
      filename,
      body,
    }) as string;
  } catch (reason) {
    const text = reason instanceof Error ? reason.message : String(reason ?? "");
    let parsed: unknown = null;
    try {
      parsed = JSON.parse(text);
    } catch {
      // Native transport errors are plain strings rather than JSON envelopes.
    }
    const payload = errorPayloadFrom(parsed);
    if (payload) throw new ApiError(0, payload);
    if (reason instanceof Error) throw reason;
    throw new Error(text || "Native request failed");
  }
}
