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

export function saveExport(
  path: string,
  filename: string,
  body?: string,
): Promise<string> {
  return window.__TAURI__!.core.invoke("save_export", {
    path,
    filename,
    body,
  }) as Promise<string>;
}
