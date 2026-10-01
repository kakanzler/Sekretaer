// Sidecar connection bootstrap.
// - Inside Tauri: invoke("sidecar_connection") and listen to "sidecar://state".
// - Browser dev mode: the developer pastes port + token; kept in sessionStorage
//   only, never in the URL.
// - Mock mode (opt-in, NEXT_PUBLIC_SEKRETAER_MOCK=1): in-memory fake sidecar.

// "failed" is emitted by the desktop shell when it gave up restarting; the UI
// then offers sidecar_restart.
export type SidecarPhase = "starting" | "ready" | "crashed" | "restarting" | "failed";

export interface SidecarConnectionInfo {
  state: SidecarPhase;
  port: number | null;
  token: string | null;
  error: string | null;
}

export const SIDECAR_STATE_EVENT = "sidecar://state";
const DEV_STORAGE_KEY = "sekretaer.devConnection";

const PHASES: ReadonlySet<string> = new Set(["starting", "ready", "crashed", "restarting", "failed"]);

/** Validate the payload of sidecar_connection / sidecar://state. */
export function parseConnectionInfo(raw: unknown): SidecarConnectionInfo {
  const o = (raw && typeof raw === "object" ? raw : {}) as Record<string, unknown>;
  const state = typeof o.state === "string" && PHASES.has(o.state) ? (o.state as SidecarPhase) : "starting";
  const port = typeof o.port === "number" && Number.isInteger(o.port) && o.port > 0 && o.port < 65536 ? o.port : null;
  const token = typeof o.token === "string" && o.token.length > 0 ? o.token : null;
  const error = typeof o.error === "string" ? o.error : null;
  return { state, port, token, error };
}

export function isTauri(): boolean {
  return typeof window !== "undefined" && "__TAURI_INTERNALS__" in window;
}

export async function tauriInvoke<T>(cmd: string, args?: Record<string, unknown>): Promise<T> {
  const { invoke } = await import("@tauri-apps/api/core");
  return invoke<T>(cmd, args);
}

export async function getTauriConnection(): Promise<SidecarConnectionInfo> {
  return parseConnectionInfo(await tauriInvoke<unknown>("sidecar_connection"));
}

export async function listenTauriConnection(cb: (info: SidecarConnectionInfo) => void): Promise<() => void> {
  const { listen } = await import("@tauri-apps/api/event");
  const unlisten = await listen<unknown>(SIDECAR_STATE_EVENT, (e) => cb(parseConnectionInfo(e.payload)));
  return unlisten;
}

/** Ask the Tauri shell to restart the sidecar (e.g. after it reached "failed"). */
export async function restartSidecar(): Promise<boolean> {
  if (!isTauri()) return false;
  try {
    await tauriInvoke("sidecar_restart");
    return true;
  } catch {
    return false;
  }
}

/**
 * Ask the Tauri shell to open the OS privacy settings (e.g. microphone
 * permission). Returns false when not in Tauri or the command is unavailable.
 */
export async function openOsPrivacySettings(kind: "microphone" = "microphone"): Promise<boolean> {
  if (!isTauri()) return false;
  try {
    await tauriInvoke("open_privacy_settings", { kind });
    return true;
  } catch {
    return false;
  }
}

export interface DevConnection {
  port: number;
  token: string;
}

export function loadDevConnection(): DevConnection | null {
  if (typeof window === "undefined") return null;
  try {
    const raw = window.sessionStorage.getItem(DEV_STORAGE_KEY);
    if (!raw) return null;
    const info = parseConnectionInfo({ ...JSON.parse(raw), state: "ready" });
    return info.port && info.token ? { port: info.port, token: info.token } : null;
  } catch {
    return null;
  }
}

export function saveDevConnection(c: DevConnection): void {
  window.sessionStorage.setItem(DEV_STORAGE_KEY, JSON.stringify({ port: c.port, token: c.token }));
}

export function clearDevConnection(): void {
  window.sessionStorage.removeItem(DEV_STORAGE_KEY);
}

/**
 * Accept what a developer is likely to paste: a port number, or the sidecar's
 * ready line `{"type":"ready","port":..,"token":..}`.
 */
export function parseDevInput(portText: string, tokenText: string): DevConnection | null {
  const trimmed = portText.trim();
  if (trimmed.startsWith("{")) {
    try {
      const o = JSON.parse(trimmed) as Record<string, unknown>;
      const info = parseConnectionInfo({ ...o, state: "ready" });
      if (info.port && info.token) return { port: info.port, token: info.token };
    } catch {
      return null;
    }
    return null;
  }
  const port = Number(trimmed);
  const token = tokenText.trim();
  if (!Number.isInteger(port) || port <= 0 || port >= 65536 || !token) return null;
  return { port, token };
}
