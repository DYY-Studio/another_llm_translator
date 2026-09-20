import { ApiError, errorPayloadFrom } from "./api.ts";

declare global {
  interface Window {
    __TAURI__?: {
      core: {
        invoke: (command: string, args?: Record<string, unknown>) => Promise<unknown>;
      };
      opener?: {
        openUrl: (url: string) => Promise<void>;
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

export async function openExternalUrl(url: string): Promise<void> {
  if (nativeBridgeAvailable()) {
    const openUrl = window.__TAURI__?.opener?.openUrl;
    if (typeof openUrl !== "function") {
      throw new Error("Tauri opener is unavailable");
    }
    await openUrl(url);
    return;
  }

  const popup = window.open(url, "_blank", "noopener,noreferrer");
  if (!popup) {
    throw new Error("Could not open external URL");
  }
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
