// Decides whether a meeting whose state is "recording" is actually being
// captured right now, or was left "recording" by a crash (spec §9: never
// auto-resume; ask the user). The contract has no explicit flag for this, so
// we combine several signals (see findings: contract gaps).

import type { Meeting } from "./types";

const STORAGE_KEY = "sekretaer.startedHere";
const LIVE_SIGNAL_WINDOW_MS = 10_000;
const INTERRUPTED_CODES = new Set(["interrupted", "recovery_required", "crashed", "orphaned"]);

/** Non-cryptographic 32-bit FNV-1a; only used to tell sidecar processes apart. */
export function fnv1a(s: string): string {
  let h = 0x811c9dc5;
  for (let i = 0; i < s.length; i++) {
    h ^= s.charCodeAt(i);
    h = Math.imul(h, 0x01000193) >>> 0;
  }
  return h.toString(16).padStart(8, "0");
}

/** Identifies one sidecar process (tokens live only for the process lifetime). Never stores the token. */
export function connectionKey(port: number, token: string): string {
  return `${port}:${fnv1a(token)}`;
}

function read(storage: Storage | null): Record<string, string> {
  if (!storage) return {};
  try {
    const v = JSON.parse(storage.getItem(STORAGE_KEY) ?? "{}");
    return v && typeof v === "object" ? (v as Record<string, string>) : {};
  } catch {
    return {};
  }
}

function sessionStore(): Storage | null {
  try {
    return typeof window !== "undefined" ? window.sessionStorage : null;
  } catch {
    return null;
  }
}

export function markStartedHere(meetingId: string, connKey: string, storage: Storage | null = sessionStore()): void {
  if (!storage) return;
  const all = read(storage);
  all[meetingId] = connKey;
  try {
    storage.setItem(STORAGE_KEY, JSON.stringify(all));
  } catch {
    /* storage full / disabled: fall back to live signals only */
  }
}

export function wasStartedHere(meetingId: string, connKey: string, storage: Storage | null = sessionStore()): boolean {
  return read(storage)[meetingId] === connKey;
}

export type RecordingLiveness = "not_recording" | "live" | "interrupted" | "unknown";

export function classifyRecording(
  meeting: Meeting,
  signals: { startedHere: boolean; lastLiveSignalAt: number | null; now: number },
): RecordingLiveness {
  if (meeting.state !== "recording") return "not_recording";
  if (meeting.degraded.some((c) => INTERRUPTED_CODES.has(c))) return "interrupted";
  if (signals.lastLiveSignalAt !== null && signals.now - signals.lastLiveSignalAt <= LIVE_SIGNAL_WINDOW_MS) return "live";
  if (signals.startedHere) return "live";
  return "unknown";
}
