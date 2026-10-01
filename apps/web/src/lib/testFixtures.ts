// Shared fixtures for unit tests (not imported by application code).

import type { EventEnvelope, EventName, Meeting, Note, Segment } from "./types";

export const MEETING_ID = "11111111-1111-4111-8111-111111111111";

export function meeting(p: Partial<Meeting> = {}): Meeting {
  return {
    id: MEETING_ID,
    title: "テスト会議",
    language: "ja",
    state: "recording",
    degraded: [],
    startedAt: null,
    endedAt: null,
    timezone: "Asia/Tokyo",
    createdAt: "2026-09-29T10:00:00+09:00",
    updatedAt: "2026-09-29T10:00:00+09:00",
    elapsedMs: 0,
    sources: [
      { id: "src-mic", kind: "microphone", deviceKey: "mic", sampleRate: 48000, channels: 1, permissionState: "granted", offsetMs: 0 },
    ],
    consent: { recording: "granted", externalProcessing: "unconfirmed" },
    settings: { summarizationEnabled: false, retainAudio: false, audioRetentionDays: 7 },
    processing: { sttBacklogMs: 0, pendingJobs: 0, lastError: null },
    ...p,
  };
}

export function segment(p: Partial<Segment> & { id: string }): Segment {
  return {
    meetingId: MEETING_ID,
    sourceId: "src-mic",
    source: "microphone",
    startMs: 0,
    endMs: 1000,
    text: "テキスト",
    language: "ja",
    isFinal: true,
    revision: 1,
    speakerLabel: null,
    stt: null,
    ...p,
  };
}

export function ev(event: EventName, seq: number, data: Record<string, unknown>, meetingId: string | null = MEETING_ID): EventEnvelope {
  return { event, eventId: `e${seq}`, meetingId, seq, occurredAt: "2026-09-29T10:03:12+09:00", data };
}

export function note(p: Partial<Note> = {}): Note {
  return {
    schemaVersion: "1.0",
    meetingId: MEETING_ID,
    revisionId: "22222222-2222-4222-8222-222222222222",
    generatedAt: "2026-09-29T10:10:00+09:00",
    coverage: { fromMs: 0, toMs: 60000 },
    cornell: { cues: [{ text: "論点", evidenceSegmentIds: ["s1"] }], notes: [], summary: "要約" },
    bullets: [{ id: "b1", text: "項目", evidenceSegmentIds: ["s1"], children: [{ id: "b2", text: "子", evidenceSegmentIds: ["s2"], children: [] }] }],
    decisions: [{ id: "d1", text: "決定", status: "needs_review", certainty: 0.7, evidenceSegmentIds: ["s1"] }],
    actions: [
      { id: "a1", text: "作業", assignee: null, dueDate: "来週金曜", status: "open", confirmed: false, origin: "ai", evidenceSegmentIds: ["s2"] },
    ],
    openQuestions: [{ text: "未定の点", evidenceSegmentIds: [], noEvidenceReason: "推定" }],
    ...p,
  };
}
