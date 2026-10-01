// Small browser-only helpers. Call only from event handlers / effects.

/** Save text as a file via a Blob download (a native save dialog may replace this later in Tauri). */
export function downloadText(filename: string, contentType: string, content: string): void {
  const blob = new Blob([content], { type: contentType || "text/plain;charset=utf-8" });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = sanitizeFilename(filename);
  a.rel = "noopener";
  document.body.appendChild(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

export function sanitizeFilename(name: string): string {
  const cleaned = name.replace(/[\\/:*?"<>|\u0000-\u001F]/g, "_").trim();
  return cleaned || "sekretaer-export";
}

const PREVIEW_ACK_KEY = "sekretaer.previewAck";

/** Whether the user has already reviewed the external-send preview for this meeting (this session). */
export function hasPreviewAck(meetingId: string): boolean {
  try {
    const v = JSON.parse(window.sessionStorage.getItem(PREVIEW_ACK_KEY) ?? "[]");
    return Array.isArray(v) && v.includes(meetingId);
  } catch {
    return false;
  }
}

export function setPreviewAck(meetingId: string): void {
  try {
    const v = JSON.parse(window.sessionStorage.getItem(PREVIEW_ACK_KEY) ?? "[]");
    const list: string[] = Array.isArray(v) ? v : [];
    if (!list.includes(meetingId)) list.push(meetingId);
    window.sessionStorage.setItem(PREVIEW_ACK_KEY, JSON.stringify(list));
  } catch {
    /* ignore */
  }
}

export function prefersReducedMotion(): boolean {
  return typeof window !== "undefined" && window.matchMedia?.("(prefers-reduced-motion: reduce)").matches === true;
}
