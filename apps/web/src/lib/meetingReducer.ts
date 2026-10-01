// Per-meeting event reducer (pure). Combines REST snapshots with WS events.

import { emptyTranscript, eventToLine, upsertLines, type TranscriptState } from "./transcript";
import {
  EPHEMERAL_EVENTS,
  type AudioGapData,
  type EventEnvelope,
  type Job,
  type JobFailedData,
  type JobStatus,
  type JobTrigger,
  type Meeting,
  type MeetingStateData,
  type Segment,
  type SourceKind,
  type SummaryQueuedData,
  type SummaryUpdatedData,
  type TranscriptEventData,
} from "./types";

export interface JobView {
  id: string;
  trigger: JobTrigger | null;
  status: JobStatus;
  errorCode: string | null;
  retryAfter: string | null;
  willRetry: boolean;
  attempt: number | null;
  createdAt: string | null;
  updatedAtMs: number;
}

export interface LevelReading {
  /** 0..1 */
  level: number;
  at: number;
}

export interface WarningEntry {
  code: string;
  message: string | null;
  at: number;
}

export interface MeetingViewState {
  meetingId: string;
  meeting: Meeting | null;
  /** Local clock (ms) when the meeting snapshot was applied; used for elapsed-time fallback. */
  snapshotAt: number | null;
  transcript: TranscriptState;
  /** Highest persisted event seq applied to this view. */
  lastSeq: number;
  jobs: Record<string, JobView>;
  /** Incremented whenever the notes must be re-fetched (summary.updated). */
  notesVersion: number;
  /** summary.updated carried conflict:true since the last notes fetch. */
  conflictHint: boolean;
  /** Incremented on privacy.state; the view re-fetches meeting/privacy. */
  privacyVersion: number;
  levels: Record<string, LevelReading>;
  gaps: AudioGapData[];
  warnings: WarningEntry[];
  /** Local time of the last ephemeral (live-only) event for this meeting. */
  lastLiveSignalAt: number | null;
}

export type MeetingAction =
  | { type: "snapshot/meeting"; meeting: Meeting; at: number }
  | { type: "snapshot/transcript"; segments: Segment[] }
  | { type: "snapshot/jobs"; jobs: Job[]; at: number }
  | { type: "segment/updated"; segment: Segment }
  | { type: "job/updated"; job: Job; at: number }
  | { type: "notes/fetched" }
  | { type: "event"; event: EventEnvelope; now: number };

export function initialMeetingState(meetingId: string): MeetingViewState {
  return {
    meetingId,
    meeting: null,
    snapshotAt: null,
    transcript: emptyTranscript,
    lastSeq: 0,
    jobs: {},
    notesVersion: 0,
    conflictHint: false,
    privacyVersion: 0,
    levels: {},
    gaps: [],
    warnings: [],
    lastLiveSignalAt: null,
  };
}

const MAX_GAPS = 50;
const MAX_WARNINGS = 20;

function jobFromRest(job: Job, at: number): JobView {
  return {
    id: job.id,
    trigger: job.trigger,
    status: job.status,
    errorCode: job.errorCode,
    retryAfter: job.retryAfter,
    willRetry: job.status === "retry_wait",
    attempt: job.attempt,
    createdAt: job.createdAt,
    updatedAtMs: at,
  };
}

function num(v: unknown): number | null {
  return typeof v === "number" && Number.isFinite(v) ? v : null;
}

/** Normalise a level reading of unknown scale to 0..1. dBFS (≤0) maps -60..0 → 0..1. */
export function normalizeLevel(raw: Record<string, unknown>): number | null {
  const db = num(raw.db) ?? num(raw.dbfs);
  if (db !== null) return Math.min(1, Math.max(0, (db + 60) / 60));
  const v = num(raw.level) ?? num(raw.rms) ?? num(raw.peak);
  if (v === null) return null;
  return Math.min(1, Math.max(0, v > 1 ? v / 100 : v));
}

/**
 * The contract does not fix the audio.level payload. Accept either a single
 * reading `{sourceId?|source?, level|rms|peak|db}` or `{levels:[...]}`.
 */
export function parseLevels(data: Record<string, unknown>): { key: string; level: number }[] {
  const items: Record<string, unknown>[] = Array.isArray(data.levels)
    ? (data.levels.filter((x) => x && typeof x === "object") as Record<string, unknown>[])
    : [data];
  const out: { key: string; level: number }[] = [];
  for (const it of items) {
    const level = normalizeLevel(it);
    if (level === null) continue;
    const key =
      (typeof it.sourceId === "string" && it.sourceId) ||
      (typeof it.source === "string" && it.source) ||
      (typeof it.kind === "string" && it.kind) ||
      "default";
    out.push({ key, level });
  }
  return out;
}

function sourceIdByKind(meeting: Meeting | null): Partial<Record<SourceKind, string>> {
  const map: Partial<Record<SourceKind, string>> = {};
  for (const s of meeting?.sources ?? []) map[s.kind] = s.id;
  return map;
}

function applyEvent(state: MeetingViewState, ev: EventEnvelope, now: number): MeetingViewState {
  // Ignore events for other meetings. Global events (meetingId null) only matter for privacy.
  if (ev.meetingId && ev.meetingId !== state.meetingId) return state;
  if (!ev.meetingId && ev.event !== "privacy.state" && ev.event !== "warning") return state;

  const ephemeral = EPHEMERAL_EVENTS.has(ev.event);
  if (!ephemeral) {
    if (ev.seq <= state.lastSeq) return state; // duplicate from replay
    state = { ...state, lastSeq: ev.seq };
  } else {
    state = { ...state, lastLiveSignalAt: now };
  }

  const data = ev.data as Record<string, unknown>;
  switch (ev.event) {
    case "meeting.state": {
      const d = data as unknown as MeetingStateData;
      if (!state.meeting) return state;
      return {
        ...state,
        meeting: {
          ...state.meeting,
          state: d.state,
          degraded: Array.isArray(d.degraded) ? d.degraded : state.meeting.degraded,
        },
      };
    }
    case "transcript.partial":
    case "transcript.final": {
      const d = data as unknown as TranscriptEventData;
      if (typeof d.segmentId !== "string") return state;
      const line = eventToLine(state.meetingId, d, ev.event === "transcript.final", sourceIdByKind(state.meeting));
      const transcript = upsertLines(state.transcript, [line]);
      return transcript === state.transcript ? state : { ...state, transcript };
    }
    case "summary.queued": {
      const d = data as unknown as SummaryQueuedData;
      const prev = state.jobs[d.jobId];
      return {
        ...state,
        jobs: {
          ...state.jobs,
          [d.jobId]: {
            id: d.jobId,
            trigger: d.trigger ?? prev?.trigger ?? null,
            status: "queued",
            errorCode: null,
            retryAfter: null,
            willRetry: false,
            attempt: prev?.attempt ?? null,
            createdAt: prev?.createdAt ?? ev.occurredAt,
            updatedAtMs: now,
          },
        },
      };
    }
    case "summary.updated": {
      const d = data as unknown as SummaryUpdatedData;
      const prev = state.jobs[d.jobId];
      return {
        ...state,
        jobs: {
          ...state.jobs,
          [d.jobId]: {
            id: d.jobId,
            trigger: prev?.trigger ?? null,
            status: "succeeded",
            errorCode: null,
            retryAfter: null,
            willRetry: false,
            attempt: prev?.attempt ?? null,
            createdAt: prev?.createdAt ?? null,
            updatedAtMs: now,
          },
        },
        notesVersion: state.notesVersion + 1,
        conflictHint: state.conflictHint || d.conflict === true,
      };
    }
    case "job.failed": {
      const d = data as unknown as JobFailedData;
      const prev = state.jobs[d.jobId];
      const willRetry = d.willRetry === true;
      return {
        ...state,
        jobs: {
          ...state.jobs,
          [d.jobId]: {
            id: d.jobId,
            trigger: prev?.trigger ?? null,
            status: willRetry ? "retry_wait" : "failed",
            errorCode: d.errorCode,
            retryAfter: d.retryAfter ?? null,
            willRetry,
            attempt: prev?.attempt ?? null,
            createdAt: prev?.createdAt ?? null,
            updatedAtMs: now,
          },
        },
      };
    }
    case "audio.level": {
      const readings = parseLevels(data);
      if (readings.length === 0) return state;
      const levels = { ...state.levels };
      for (const r of readings) levels[r.key] = { level: r.level, at: now };
      return { ...state, levels };
    }
    case "audio.gap": {
      const d = data as unknown as AudioGapData;
      return { ...state, gaps: [...state.gaps, d].slice(-MAX_GAPS) };
    }
    case "privacy.state":
      return { ...state, privacyVersion: state.privacyVersion + 1 };
    case "warning": {
      const code = typeof data.code === "string" ? data.code : "warning";
      const message = typeof data.message === "string" ? data.message : null;
      return { ...state, warnings: [...state.warnings, { code, message, at: now }].slice(-MAX_WARNINGS) };
    }
    default:
      return state;
  }
}

export function meetingReducer(state: MeetingViewState, action: MeetingAction): MeetingViewState {
  switch (action.type) {
    case "snapshot/meeting":
      return { ...state, meeting: action.meeting, snapshotAt: action.at };
    case "snapshot/transcript":
      return { ...state, transcript: upsertLines(state.transcript, action.segments) };
    case "segment/updated":
      return { ...state, transcript: upsertLines(state.transcript, [action.segment]) };
    case "snapshot/jobs": {
      const jobs = { ...state.jobs };
      for (const j of action.jobs) jobs[j.id] = jobFromRest(j, action.at);
      return { ...state, jobs };
    }
    case "job/updated":
      return { ...state, jobs: { ...state.jobs, [action.job.id]: jobFromRest(action.job, action.at) } };
    case "notes/fetched":
      return state.conflictHint ? { ...state, conflictHint: false } : state;
    case "event":
      return applyEvent(state, action.event, action.now);
    default:
      return state;
  }
}

// ---- selectors ---------------------------------------------------------------

const ACTIVE: ReadonlySet<JobStatus> = new Set<JobStatus>(["queued", "running", "retry_wait"]);

export function jobsNewestFirst(state: MeetingViewState): JobView[] {
  return Object.values(state.jobs).sort((a, b) => {
    const ca = a.createdAt ? Date.parse(a.createdAt) : 0;
    const cb = b.createdAt ? Date.parse(b.createdAt) : 0;
    return cb - ca || b.updatedAtMs - a.updatedAtMs;
  });
}

export function activeJobs(state: MeetingViewState): JobView[] {
  return jobsNewestFirst(state).filter((j) => ACTIVE.has(j.status));
}

/** Notes are 「暫定」 while any summary job is queued / running / waiting to retry. */
export function isProvisional(state: MeetingViewState): boolean {
  return activeJobs(state).length > 0;
}

/** Failed jobs newer than the newest success — these are the ones worth retrying. */
export function unresolvedFailures(state: MeetingViewState): JobView[] {
  const list = jobsNewestFirst(state);
  const out: JobView[] = [];
  for (const j of list) {
    if (j.status === "succeeded") break;
    if (j.status === "failed") out.push(j);
  }
  return out;
}

/** Elapsed recording time. Sidecar and UI share a machine, so wall-clock is reliable. */
export function computeElapsedMs(meeting: Meeting, now: number, snapshotAt: number | null): number {
  const started = meeting.startedAt ? Date.parse(meeting.startedAt) : NaN;
  if (!Number.isNaN(started)) {
    if (meeting.state === "recording") return Math.max(0, now - started);
    const ended = meeting.endedAt ? Date.parse(meeting.endedAt) : NaN;
    if (!Number.isNaN(ended)) return Math.max(0, ended - started);
  }
  if (meeting.state === "recording" && snapshotAt !== null) {
    return Math.max(0, meeting.elapsedMs + (now - snapshotAt));
  }
  return meeting.elapsedMs;
}
