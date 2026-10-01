// Time and text formatting helpers (pure).

function pad2(n: number): string {
  return n < 10 ? `0${n}` : String(n);
}

/** Meeting-relative ms → "mm:ss" (or "h:mm:ss" from one hour). Negative → 00:00. */
export function formatMs(ms: number): string {
  const total = Math.max(0, Math.floor((Number.isFinite(ms) ? ms : 0) / 1000));
  const h = Math.floor(total / 3600);
  const m = Math.floor((total % 3600) / 60);
  const s = total % 60;
  return h > 0 ? `${h}:${pad2(m)}:${pad2(s)}` : `${pad2(m)}:${pad2(s)}`;
}

/** Spoken form for screen readers, e.g. "1分05秒". */
export function formatMsSpoken(ms: number): string {
  const total = Math.max(0, Math.floor(ms / 1000));
  const h = Math.floor(total / 3600);
  const m = Math.floor((total % 3600) / 60);
  const s = total % 60;
  return `${h > 0 ? `${h}時間` : ""}${m}分${pad2(s)}秒`;
}

/** Range "mm:ss–mm:ss". */
export function formatRange(fromMs: number, toMs: number): string {
  return `${formatMs(fromMs)}–${formatMs(toMs)}`;
}

/** ISO timestamp → "2026/09/29 10:03" in the given (or local) time zone. */
export function formatDateTime(iso: string | null | undefined, timeZone?: string): string {
  if (!iso) return "—";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "—";
  try {
    return new Intl.DateTimeFormat("ja-JP", {
      year: "numeric",
      month: "2-digit",
      day: "2-digit",
      hour: "2-digit",
      minute: "2-digit",
      timeZone,
    }).format(d);
  } catch {
    return new Intl.DateTimeFormat("ja-JP", {
      year: "numeric",
      month: "2-digit",
      day: "2-digit",
      hour: "2-digit",
      minute: "2-digit",
    }).format(d);
  }
}

/** Seconds from a backlog in ms, rounded, for "認識待ち 12 秒". */
export function formatSeconds(ms: number): string {
  return `${Math.round(ms / 1000)} 秒`;
}

/** 0..1 model hint → "目安 72%". Never presented as a probability. */
export function formatCertainty(v: number | null | undefined): string {
  if (v === null || v === undefined || !Number.isFinite(v)) return "目安 —";
  return `目安 ${Math.round(Math.min(1, Math.max(0, v)) * 100)}%`;
}

/**
 * Plain-text normalisation for AI/transcript content. The UI never interprets
 * markdown or HTML from the sidecar; text is rendered as React text nodes. This
 * only strips control characters (except newline/tab) so they do not break layout.
 */
export function plainText(s: string | null | undefined): string {
  if (!s) return "";
  return s.replace(/[\u0000-\u0008\u000B\u000C\u000E-\u001F\u007F]/g, "");
}

/** Case-insensitive, width-insensitive match used by transcript search. */
export function normalizeForSearch(s: string): string {
  return s.normalize("NFKC").toLowerCase();
}

/** Split text into [before, match, after...] chunks for highlighting search hits. */
export function splitForHighlight(text: string, query: string): { text: string; hit: boolean }[] {
  const q = query.trim();
  if (!q) return [{ text, hit: false }];
  const nText = normalizeForSearch(text);
  const nQ = normalizeForSearch(q);
  // Only highlight when normalisation kept lengths aligned (true for most JP/EN text).
  if (nText.length !== text.length) return [{ text, hit: nText.includes(nQ) }];
  const parts: { text: string; hit: boolean }[] = [];
  let i = 0;
  while (i <= text.length) {
    const j = nText.indexOf(nQ, i);
    if (j < 0) break;
    if (j > i) parts.push({ text: text.slice(i, j), hit: false });
    parts.push({ text: text.slice(j, j + nQ.length), hit: true });
    i = j + nQ.length;
  }
  if (i < text.length) parts.push({ text: text.slice(i), hit: false });
  return parts.length ? parts : [{ text, hit: false }];
}
