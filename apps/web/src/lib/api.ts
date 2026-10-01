// Typed client for the sidecar HTTP API (contracts/api-v1.md).
// Unwraps the {requestId, data | error} envelope and raises ApiError with the
// error code, a Japanese message and the requestId.

import { ApiError } from "./errors";
import type {
  AppSettings,
  ConsentRequest,
  CreateMeetingRequest,
  DeleteResult,
  Device,
  ExportResult,
  Health,
  Job,
  Meeting,
  Note,
  NoteRevision,
  NoteRevisionMeta,
  NotesState,
  Page,
  PatchMeetingRequest,
  PrivacyInfo,
  Segment,
  SegmentHistoryItem,
  SummaryPreview,
} from "./types";

export type FetchLike = (input: string, init?: RequestInit) => Promise<Response>;

export interface ApiClientOptions {
  /** e.g. http://127.0.0.1:53124 — always 127.0.0.1, never "localhost". */
  origin: string;
  token: string;
  fetchImpl?: FetchLike;
  newIdempotencyKey?: () => string;
}

export const API_BASE_PATH = "/api/v1";

export function sidecarOrigin(port: number): string {
  return `http://127.0.0.1:${port}`;
}

type Method = "GET" | "POST" | "PUT" | "PATCH" | "DELETE";

interface Envelope<T> {
  requestId?: string;
  data?: T;
  error?: { code?: string; message?: string; details?: Record<string, unknown> };
}

function qs(params: Record<string, string | number | boolean | null | undefined>): string {
  const sp = new URLSearchParams();
  for (const [k, v] of Object.entries(params)) {
    if (v === undefined || v === null || v === "") continue;
    sp.set(k, String(v));
  }
  const s = sp.toString();
  return s ? `?${s}` : "";
}

const enc = encodeURIComponent;

export class ApiClient {
  readonly origin: string;
  private readonly token: string;
  private readonly fetchImpl: FetchLike;
  private readonly newKey: () => string;

  constructor(opts: ApiClientOptions) {
    this.origin = opts.origin.replace(/\/+$/, "");
    this.token = opts.token;
    this.fetchImpl = opts.fetchImpl ?? ((input, init) => fetch(input, init));
    this.newKey = opts.newIdempotencyKey ?? (() => crypto.randomUUID());
  }

  /** Low-level request: unwraps the envelope or throws ApiError. */
  async request<T>(
    method: Method,
    path: string,
    opts: { body?: unknown; headers?: Record<string, string>; signal?: AbortSignal } = {},
  ): Promise<T> {
    const headers: Record<string, string> = {
      Accept: "application/json",
      Authorization: `Bearer ${this.token}`,
      ...opts.headers,
    };
    let body: string | undefined;
    if (method !== "GET") {
      // Mutating requests must carry application/json (CSRF defence in the
      // contract), even when there is no meaningful body.
      headers["Content-Type"] = "application/json";
      body = JSON.stringify(opts.body ?? {});
      if (method === "DELETE" && opts.body === undefined) body = undefined;
    }

    let res: Response;
    try {
      res = await this.fetchImpl(`${this.origin}${API_BASE_PATH}${path}`, {
        method,
        headers,
        body,
        signal: opts.signal,
        cache: "no-store",
        credentials: "omit",
      });
    } catch (e) {
      if (e instanceof DOMException && e.name === "AbortError") throw e;
      throw new ApiError({
        code: "network_error",
        status: 0,
        requestId: null,
        details: { cause: e instanceof Error ? e.message : String(e) },
      });
    }

    const headerRequestId = res.headers.get("X-Request-Id");
    let env: Envelope<T> | null = null;
    const text = await res.text();
    if (text) {
      try {
        env = JSON.parse(text) as Envelope<T>;
      } catch {
        env = null;
      }
    }
    const requestId = env?.requestId ?? headerRequestId ?? null;

    if (env && env.error) {
      throw new ApiError({
        code: env.error.code ?? (res.status === 401 ? "unauthorized" : "invalid_response"),
        status: res.status,
        requestId,
        serverMessage: env.error.message ?? null,
        details: env.error.details,
      });
    }
    if (!res.ok) {
      throw new ApiError({
        code: res.status === 401 ? "unauthorized" : res.status === 403 ? "origin_forbidden" : "invalid_response",
        status: res.status,
        requestId,
      });
    }
    if (!env || !("data" in env)) {
      // 202 responses without a body (e.g. /shutdown) are fine.
      if (res.status === 202 || res.status === 204) return undefined as T;
      throw new ApiError({ code: "invalid_response", status: res.status, requestId });
    }
    return env.data as T;
  }

  // ---- system ---------------------------------------------------------------
  health() {
    return this.request<Health>("GET", "/health");
  }
  devices() {
    return this.request<{ devices: Device[] }>("GET", "/devices");
  }
  getSettings() {
    return this.request<AppSettings>("GET", "/settings");
  }
  putSettings(settings: AppSettings) {
    return this.request<AppSettings>("PUT", "/settings", { body: settings });
  }
  privacy() {
    return this.request<PrivacyInfo>("GET", "/privacy");
  }
  /** Explicit user action only: fetch the configured STT model (spec §5). */
  downloadSttModel() {
    return this.request<Health["stt"]>("POST", "/stt/model/download");
  }

  // ---- meetings -------------------------------------------------------------
  listMeetings(params: { limit?: number; cursor?: string | null } = {}) {
    return this.request<Page<Meeting>>("GET", `/meetings${qs(params)}`);
  }
  createMeeting(body: CreateMeetingRequest) {
    return this.request<Meeting>("POST", "/meetings", { body });
  }
  getMeeting(id: string) {
    return this.request<Meeting>("GET", `/meetings/${enc(id)}`);
  }
  patchMeeting(id: string, body: PatchMeetingRequest) {
    return this.request<Meeting>("PATCH", `/meetings/${enc(id)}`, { body });
  }
  recordConsent(id: string, body: ConsentRequest) {
    return this.request<Meeting>("POST", `/meetings/${enc(id)}/consent`, { body });
  }
  /** Each call uses a fresh Idempotency-Key unless one is supplied (retries of the same attempt). */
  startMeeting(id: string, idempotencyKey: string = this.newKey()) {
    return this.request<Meeting>("POST", `/meetings/${enc(id)}/start`, {
      headers: { "Idempotency-Key": idempotencyKey },
    });
  }
  stopMeeting(id: string) {
    return this.request<Meeting>("POST", `/meetings/${enc(id)}/stop`);
  }
  recoverMeeting(id: string) {
    return this.request<Meeting>("POST", `/meetings/${enc(id)}/recover`, { body: { action: "finalize" } });
  }
  deleteMeeting(id: string) {
    return this.request<DeleteResult>("DELETE", `/meetings/${enc(id)}`);
  }
  exportMeeting(id: string, format: "markdown" | "json") {
    return this.request<ExportResult>("GET", `/meetings/${enc(id)}/export${qs({ format })}`);
  }

  // ---- transcript -----------------------------------------------------------
  transcriptPage(
    id: string,
    params: { afterMs?: number; limit?: number; includePartial?: boolean; cursor?: string | null } = {},
  ) {
    return this.request<Page<Segment>>("GET", `/meetings/${enc(id)}/transcript${qs(params)}`);
  }
  /** Follows nextCursor until exhausted. */
  async transcriptAll(id: string, includePartial: boolean, maxPages = 200): Promise<Segment[]> {
    const out: Segment[] = [];
    let cursor: string | null = null;
    for (let i = 0; i < maxPages; i++) {
      const page: Page<Segment> = await this.transcriptPage(id, { limit: 500, includePartial, cursor });
      out.push(...page.items);
      if (!page.nextCursor || page.items.length === 0) break;
      cursor = page.nextCursor;
    }
    return out;
  }
  patchSegment(id: string, segmentId: string, body: { text?: string; speakerLabel?: string | null }) {
    return this.request<Segment>("PATCH", `/meetings/${enc(id)}/transcript/${enc(segmentId)}`, { body });
  }
  segmentHistory(id: string, segmentId: string) {
    return this.request<{ items: SegmentHistoryItem[] }>(
      "GET",
      `/meetings/${enc(id)}/transcript/${enc(segmentId)}/history`,
    );
  }

  // ---- summaries / notes ----------------------------------------------------
  summaryPreview(id: string) {
    return this.request<SummaryPreview>("GET", `/meetings/${enc(id)}/summaries/preview`);
  }
  requestSummary(id: string) {
    return this.request<{ jobId: string; deduplicated: boolean }>("POST", `/meetings/${enc(id)}/summaries`, {
      body: {},
    });
  }
  notes(id: string) {
    return this.request<NotesState>("GET", `/meetings/${enc(id)}/notes`);
  }
  noteRevisions(id: string) {
    return this.request<{ items: NoteRevisionMeta[] }>("GET", `/meetings/${enc(id)}/notes/revisions`);
  }
  putNote(id: string, baseRevisionId: string | null, note: Note) {
    return this.request<NoteRevision>("PUT", `/meetings/${enc(id)}/notes`, { body: { baseRevisionId, note } });
  }
  resolveConflict(id: string, action: "accept_ai" | "keep_mine", conflictRevisionId: string) {
    return this.request<NoteRevision>("POST", `/meetings/${enc(id)}/notes/conflict/resolve`, {
      body: { action, conflictRevisionId },
    });
  }

  // ---- jobs -----------------------------------------------------------------
  meetingJobs(id: string) {
    return this.request<{ items: Job[] }>("GET", `/meetings/${enc(id)}/jobs`);
  }
  job(jobId: string) {
    return this.request<Job>("GET", `/jobs/${enc(jobId)}`);
  }
  retryJob(jobId: string) {
    return this.request<Job>("POST", `/jobs/${enc(jobId)}/retry`);
  }
  cancelJob(jobId: string) {
    return this.request<Job>("POST", `/jobs/${enc(jobId)}/cancel`);
  }
}
