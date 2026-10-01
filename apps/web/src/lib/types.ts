// Typed mirror of contracts/api-v1.md, contracts/notes/note-1.0.schema.json and
// contracts/events/event-1.0.schema.json. The contract files are normative; keep
// this file in sync with them rather than the other way round.

export type Uuid = string;

export type MeetingLanguage = "auto" | "ja" | "en";

export type MeetingState =
  | "preparing"
  | "recording"
  | "finalizing"
  | "completed"
  | "error"
  | "deleting"
  | "deleted";

export type SourceKind = "microphone" | "system";

export type PermissionState = "granted" | "denied" | "unknown" | "unavailable";

export type ConsentState = "unconfirmed" | "granted" | "denied";

export interface MeetingSource {
  id: Uuid;
  kind: SourceKind;
  deviceKey: string;
  sampleRate: number;
  channels: number;
  permissionState: PermissionState;
  offsetMs: number;
}

export interface MeetingSettings {
  summarizationEnabled: boolean;
  retainAudio: boolean;
  audioRetentionDays: number;
}

export interface Meeting {
  id: Uuid;
  title: string;
  language: MeetingLanguage;
  state: MeetingState;
  degraded: string[];
  startedAt: string | null;
  endedAt: string | null;
  timezone: string;
  createdAt: string;
  updatedAt: string;
  elapsedMs: number;
  sources: MeetingSource[];
  consent: { recording: ConsentState; externalProcessing: ConsentState };
  settings: MeetingSettings;
  processing: { sttBacklogMs: number; pendingJobs: number; lastError: string | null };
}

export interface Page<T> {
  items: T[];
  nextCursor: string | null;
}

export interface CreateMeetingRequest {
  title: string;
  language: MeetingLanguage;
  sources: { kind: SourceKind; deviceKey: string }[];
  consent: { recording: boolean; externalProcessing: boolean };
  settings: MeetingSettings;
}

export type ConsentScope = "recording" | "external_processing";

export interface ConsentRequest {
  scope: ConsentScope;
  granted: boolean;
  policyVersion: string;
}

export interface PatchMeetingRequest {
  title?: string;
  settings?: Partial<MeetingSettings>;
}

export type SttState = "not_downloaded" | "loading" | "ready" | "failed" | "unavailable";
export type SummarizerState = "disabled" | "ready" | "cli_not_found" | "cli_auth_required";

export interface Health {
  status: "ok" | "starting" | "degraded";
  apiVersion: string;
  stt: {
    state: SttState;
    model: string;
    device: string;
    computeType: string;
    // Pre-download disclosure (spec §5): size, provider, licence, destination, network use.
    approxSizeMb?: number | null;
    source?: string | null;
    license?: string | null;
    targetDir?: string | null;
    network?: string | null;
    downloaded?: boolean;
    downloading?: boolean | string;
    downloadError?: string | null;
  };
  vad: { state: string; engine?: string };
  summarizer: { state: SummarizerState; cliVersion: string | null };
  queue: { sttBacklogMs: number; pendingJobs: number };
}

export interface Device {
  key: string;
  name: string;
  kind: SourceKind;
  defaultSampleRate: number;
  channels: number;
  available: boolean;
  note: string | null;
}

export interface Segment {
  id: Uuid;
  meetingId: Uuid;
  sourceId: Uuid;
  source: SourceKind;
  startMs: number;
  endMs: number;
  text: string;
  language: string | null;
  isFinal: boolean;
  revision: number;
  speakerLabel: string | null;
  stt: { avgLogprob: number | null; noSpeechProb: number | null } | null;
}

export interface SegmentHistoryItem {
  revision: number;
  text: string;
  speakerLabel: string | null;
  editedAt: string;
  origin: "stt" | "user";
}

export interface SummaryPreview {
  fromMs: number;
  toMs: number;
  segmentCount: number;
  text: string;
}

// ---- Note (note-1.0.schema.json) ------------------------------------------

export type CertaintyLabel = "stated" | "inferred" | "uncertain";

export interface EvidencedText {
  text: string;
  evidenceSegmentIds: Uuid[];
  noEvidenceReason?: string;
}

export interface CornellNote extends EvidencedText {
  certainty?: CertaintyLabel;
}

export interface CornellSection {
  fromMs: number;
  toMs: number;
  summary: string;
}

export interface Bullet extends EvidencedText {
  id: Uuid;
  children: Bullet[];
  certainty?: CertaintyLabel;
}

export type DecisionStatus = "proposed" | "agreed" | "withdrawn" | "needs_review";

export interface Decision extends EvidencedText {
  id: Uuid;
  status: DecisionStatus;
  atMs?: number;
  certainty: number;
}

export type ActionStatus = "open" | "in_progress" | "done" | "canceled";

export interface ActionItem extends EvidencedText {
  id: Uuid;
  assignee: string | null;
  dueDate: string | null;
  status: ActionStatus;
  confirmed: boolean;
  origin?: "ai" | "user";
}

export interface Note {
  schemaVersion: "1.0";
  meetingId: Uuid;
  revisionId: Uuid;
  generatedAt: string;
  coverage: { fromMs: number; toMs: number };
  cornell: {
    cues: EvidencedText[];
    notes: CornellNote[];
    summary: string;
    sections?: CornellSection[];
  };
  bullets: Bullet[];
  decisions: Decision[];
  actions: ActionItem[];
  openQuestions: EvidencedText[];
}

export interface NoteRevision {
  id: Uuid;
  meetingId: Uuid;
  jobId: Uuid | null;
  origin: "ai" | "user";
  schemaVersion: string;
  createdAt: string;
  supersedesId: Uuid | null;
  note: Note;
}

export type NoteRevisionMeta = Omit<NoteRevision, "note">;

export interface NotesState {
  current: NoteRevision | null;
  pendingConflict: NoteRevision | null;
}

// ---- Jobs --------------------------------------------------------------------

export type JobStatus = "queued" | "running" | "retry_wait" | "succeeded" | "failed" | "canceled";
export type JobTrigger = "silence" | "long_speech" | "manual" | "final";

export interface Job {
  id: Uuid;
  meetingId: Uuid;
  trigger: JobTrigger;
  status: JobStatus;
  attempt: number;
  errorCode: string | null;
  retryAfter: string | null;
  createdAt: string;
  startedAt: string | null;
  finishedAt: string | null;
}

export interface ExportResult {
  filename: string;
  contentType: string;
  content: string;
}

export interface DeleteResult {
  deleted: { dbRecords: number; audioFiles: number; tempFiles: number; exports: number };
  failed: { kind: string; path: string; reason: string }[];
  status: "deleted" | "delete_incomplete";
}

export type VadSensitivity = "low" | "normal" | "high";

export interface AppSettings {
  stt: { model: string; computeType: string; device: string };
  vad: { sensitivity: VadSensitivity };
  summarizer: { cliPath?: string | null; timeoutSec: number };
  privacy: { defaultRetainAudio: boolean };
}

export interface PrivacyInfo {
  storage: { dataDir: string; dbPath: string };
  // The contract does not say whether these are counts or id lists; the UI
  // accepts either (see findings: contract gaps).
  audioArchive: { enabledMeetings: number | string[] };
  externalProcessing: { enabledMeetings: number | string[]; destination: string };
}

// ---- Events (event-1.0.schema.json) -------------------------------------------

export type EventName =
  | "meeting.state"
  | "transcript.partial"
  | "transcript.final"
  | "summary.queued"
  | "summary.updated"
  | "job.failed"
  | "audio.gap"
  | "audio.level"
  | "privacy.state"
  | "warning";

export interface EventEnvelope<D = Record<string, unknown>> {
  event: EventName;
  eventId: string;
  meetingId?: string | null;
  seq: number;
  occurredAt: string;
  data: D;
}

export interface MeetingStateData {
  state: MeetingState;
  degraded?: string[];
}

export interface TranscriptEventData {
  segmentId: string;
  startMs: number;
  endMs: number;
  text: string;
  source: SourceKind;
  revision?: number;
  // Not in the event schema but tolerated if the sidecar sends them.
  sourceId?: string;
  speakerLabel?: string | null;
}

export interface SummaryQueuedData {
  jobId: string;
  trigger: JobTrigger;
}

export interface SummaryUpdatedData {
  jobId: string;
  revisionId: string;
  conflict?: boolean;
}

export interface JobFailedData {
  jobId: string;
  errorCode: string;
  retryAfter?: string | null;
  willRetry?: boolean;
}

export interface AudioGapData {
  sourceId: string;
  atMs: number;
  gapMs: number;
  reason: string;
}

/** Events that the server does not persist or replay (api-v1.md "Events"). */
export const EPHEMERAL_EVENTS: ReadonlySet<EventName> = new Set<EventName>([
  "transcript.partial",
  "audio.level",
]);
