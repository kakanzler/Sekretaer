// Transcript merge logic (pure): partial → final replacement, revisions, search.

import { normalizeForSearch } from "./format";
import type { Segment, SourceKind, TranscriptEventData } from "./types";

/**
 * View model for a transcript line. `isFinal=false` lines are the live partial
 * hypotheses; they are replaced when the final for the same segmentId arrives
 * (spec §5 "暫定結果を低遅延表示し、発話終了後に確定結果で置き換える").
 */
export type TranscriptLine = Segment;

export interface TranscriptState {
  /** segmentId → line */
  byId: Record<string, TranscriptLine>;
  /** ids sorted by startMs, then id */
  order: string[];
}

export const emptyTranscript: TranscriptState = { byId: {}, order: [] };

function sortIds(byId: Record<string, TranscriptLine>): string[] {
  return Object.keys(byId).sort((a, b) => {
    const x = byId[a]!;
    const y = byId[b]!;
    return x.startMs - y.startMs || x.endMs - y.endMs || (a < b ? -1 : a > b ? 1 : 0);
  });
}

/**
 * Decide whether `incoming` should replace `existing`.
 * - a final always wins over a partial;
 * - a partial never replaces a final;
 * - between finals, a higher (or equal, for idempotent replays) revision wins;
 * - between partials, the latest wins.
 */
export function shouldReplace(existing: TranscriptLine | undefined, incoming: TranscriptLine): boolean {
  if (!existing) return true;
  if (existing.isFinal && !incoming.isFinal) return false;
  if (!existing.isFinal && incoming.isFinal) return true;
  if (existing.isFinal && incoming.isFinal) return incoming.revision >= existing.revision;
  return true;
}

export function upsertLines(state: TranscriptState, lines: TranscriptLine[]): TranscriptState {
  let changed = false;
  const byId = { ...state.byId };
  for (const line of lines) {
    const cur = byId[line.id];
    if (shouldReplace(cur, line)) {
      // Preserve a manual speaker label if the incoming STT event does not carry one.
      const speakerLabel = line.speakerLabel ?? cur?.speakerLabel ?? null;
      byId[line.id] = { ...line, speakerLabel };
      changed = true;
      if (line.isFinal) {
        // The contract does not guarantee that a partial and its final share a
        // segmentId. Drop any stale partial from the same source that started
        // before this final ended, so a superseded hypothesis never lingers.
        for (const [id, other] of Object.entries(byId)) {
          if (id !== line.id && !other.isFinal && other.source === line.source && other.startMs < line.endMs) {
            delete byId[id];
          }
        }
      }
    }
  }
  if (!changed) return state;
  return { byId, order: sortIds(byId) };
}

export function eventToLine(
  meetingId: string,
  data: TranscriptEventData,
  isFinal: boolean,
  sourceIdByKind?: Partial<Record<SourceKind, string>>,
): TranscriptLine {
  return {
    id: data.segmentId,
    meetingId,
    sourceId: data.sourceId ?? sourceIdByKind?.[data.source] ?? "",
    source: data.source,
    startMs: data.startMs,
    endMs: data.endMs,
    text: data.text,
    language: null,
    isFinal,
    revision: data.revision ?? 1,
    speakerLabel: data.speakerLabel ?? null,
    stt: null,
  };
}

export function linesInOrder(state: TranscriptState): TranscriptLine[] {
  return state.order.map((id) => state.byId[id]!);
}

export function filterLines(lines: TranscriptLine[], query: string): TranscriptLine[] {
  const q = normalizeForSearch(query.trim());
  if (!q) return lines;
  return lines.filter(
    (l) =>
      normalizeForSearch(l.text).includes(q) ||
      (l.speakerLabel ? normalizeForSearch(l.speakerLabel).includes(q) : false),
  );
}

export function countByFinality(lines: TranscriptLine[]): { final: number; partial: number } {
  let final = 0;
  let partial = 0;
  for (const l of lines) {
    if (l.isFinal) final++;
    else partial++;
  }
  return { final, partial };
}
