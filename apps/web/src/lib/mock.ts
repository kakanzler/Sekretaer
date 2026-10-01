// In-memory fake sidecar for visual checking. OPT-IN ONLY: it is loaded via a
// dynamic import guarded by NEXT_PUBLIC_SEKRETAER_MOCK === "1", so production
// builds without that variable do not include it.

import type { FetchLike } from "./api";
import type {
  AppSettings,
  CreateMeetingRequest,
  Device,
  EventEnvelope,
  EventName,
  Health,
  Job,
  Meeting,
  Note,
  NoteRevision,
  Segment,
  SegmentHistoryItem,
} from "./types";
import type { WebSocketFactory, WebSocketLike } from "./ws";

const uuid = () => crypto.randomUUID();

/** Stable ids for the seeded sample meetings (handy for opening them directly). */
export const MOCK_COMPLETED_ID = "00000000-0000-4000-8000-000000000001";
export const MOCK_CRASHED_ID = "00000000-0000-4000-8000-000000000002";
const nowIso = () => new Date().toISOString();

interface Store {
  meetings: Map<string, Meeting>;
  segments: Map<string, Segment[]>;
  history: Map<string, SegmentHistoryItem[]>;
  notes: Map<string, { current: NoteRevision | null; pendingConflict: NoteRevision | null; revisions: NoteRevision[] }>;
  jobs: Map<string, Job>;
  idem: Map<string, Meeting>;
  settings: AppSettings;
  seq: number;
  log: EventEnvelope[];
  sockets: Set<MockSocket>;
}

const devices: Device[] = [
  { key: "mic-default", name: "既定のマイク（内蔵）", kind: "microphone", defaultSampleRate: 48000, channels: 1, available: true, note: null },
  { key: "mic-usb", name: "USB ヘッドセット", kind: "microphone", defaultSampleRate: 44100, channels: 1, available: true, note: null },
  { key: "mic-denied", name: "権限なしテスト用マイク（開始時に permission_denied）", kind: "microphone", defaultSampleRate: 48000, channels: 1, available: true, note: null },
  { key: "sys-loopback", name: "システム音声（ループバック）", kind: "system", defaultSampleRate: 48000, channels: 2, available: false, note: "この OS 版ではシステム音声の取得に対応していません。" },
];

function makeMeeting(p: Partial<Meeting> & { title: string }): Meeting {
  const created = p.createdAt ?? nowIso();
  return {
    id: p.id ?? uuid(),
    title: p.title,
    language: p.language ?? "ja",
    state: p.state ?? "preparing",
    degraded: p.degraded ?? [],
    startedAt: p.startedAt ?? null,
    endedAt: p.endedAt ?? null,
    timezone: "Asia/Tokyo",
    createdAt: created,
    updatedAt: created,
    elapsedMs: p.elapsedMs ?? 0,
    sources: p.sources ?? [
      { id: uuid(), kind: "microphone", deviceKey: "mic-default", sampleRate: 48000, channels: 1, permissionState: "granted", offsetMs: 0 },
    ],
    consent: p.consent ?? { recording: "granted", externalProcessing: "unconfirmed" },
    settings: p.settings ?? { summarizationEnabled: false, retainAudio: false, audioRetentionDays: 7 },
    processing: p.processing ?? { sttBacklogMs: 0, pendingJobs: 0, lastError: null },
  };
}

function seg(meeting: Meeting, startMs: number, text: string, source: "microphone" | "system" = "microphone"): Segment {
  return {
    id: uuid(),
    meetingId: meeting.id,
    sourceId: meeting.sources[0]!.id,
    source,
    startMs,
    endMs: startMs + 4000,
    text,
    language: "ja",
    isFinal: true,
    revision: 1,
    speakerLabel: null,
    stt: { avgLogprob: -0.3, noSpeechProb: 0.01 },
  };
}

function sampleNote(meeting: Meeting, segs: Segment[], variant: "base" | "ai2"): Note {
  const s = (i: number) => segs[Math.min(i, segs.length - 1)]!.id;
  const note: Note = {
    schemaVersion: "1.0",
    meetingId: meeting.id,
    revisionId: uuid(),
    generatedAt: nowIso(),
    coverage: { fromMs: 0, toMs: segs[segs.length - 1]!.endMs },
    cornell: {
      cues: [
        { text: "試作の範囲", evidenceSegmentIds: [s(0)] },
        { text: "次回レビューの日程", evidenceSegmentIds: [s(3)] },
      ],
      notes: [
        { text: "試作は録音と文字起こしに限定する", evidenceSegmentIds: [s(1)], certainty: "stated" },
        { text: "要約は後続で検討する", evidenceSegmentIds: [s(2)], certainty: "inferred" },
      ],
      summary: "試作範囲を録音・文字起こしに絞り、次回レビューで要約機能を再検討する。",
      sections: [{ fromMs: 0, toMs: 20000, summary: "試作範囲の確認" }],
    },
    bullets: [
      {
        id: uuid(),
        text: "試作範囲",
        evidenceSegmentIds: [s(0)],
        children: [
          { id: uuid(), text: "録音と文字起こしのみ", evidenceSegmentIds: [s(1)], children: [], certainty: "stated" },
          { id: uuid(), text: "要約は保留", evidenceSegmentIds: [], noEvidenceReason: "複数の発言からの推定", children: [], certainty: "uncertain" },
        ],
      },
    ],
    decisions: [
      { id: uuid(), text: "試作は録音と文字起こしに限定する", status: "needs_review", atMs: 5000, certainty: 0.72, evidenceSegmentIds: [s(1)] },
    ],
    actions: [
      { id: uuid(), text: "試作版を用意する", assignee: "佐藤", dueDate: "来週金曜", status: "open", confirmed: false, origin: "ai", evidenceSegmentIds: [s(2)] },
      { id: uuid(), text: "レビュー日程を調整する", assignee: null, dueDate: null, status: "open", confirmed: false, origin: "ai", evidenceSegmentIds: [s(3)] },
    ],
    openQuestions: [{ text: "システム音声を MVP に含めるか", evidenceSegmentIds: [s(3)] }],
  };
  if (variant === "ai2") {
    note.cornell.summary = "試作範囲を録音・文字起こしに絞る。要約機能は次回レビューで判断する。";
    note.decisions = [{ ...note.decisions[0]!, status: "agreed", certainty: 0.81 }];
    note.actions = [...note.actions, { id: uuid(), text: "CLI 認証手順を確認する", assignee: "田中", dueDate: null, status: "open", confirmed: false, origin: "ai", evidenceSegmentIds: [s(2)] }];
  }
  return note;
}

function revision(meetingId: string, note: Note, origin: "ai" | "user", supersedesId: string | null): NoteRevision {
  return { id: note.revisionId, meetingId, jobId: origin === "ai" ? uuid() : null, origin, schemaVersion: "1.0", createdAt: nowIso(), supersedesId, note };
}

function seed(): Store {
  const store: Store = {
    meetings: new Map(),
    segments: new Map(),
    history: new Map(),
    notes: new Map(),
    jobs: new Map(),
    idem: new Map(),
    settings: {
      stt: { model: "small", computeType: "int8", device: "cpu" },
      vad: { sensitivity: "normal" },
      summarizer: { cliPath: null, timeoutSec: 120 },
      privacy: { defaultRetainAudio: false },
    },
    seq: 100,
    log: [],
    sockets: new Set(),
  };
  const day = 24 * 3600 * 1000;
  const done = makeMeeting({
    id: MOCK_COMPLETED_ID,
    title: "週次定例（サンプル）",
    state: "completed",
    startedAt: new Date(Date.now() - day).toISOString(),
    endedAt: new Date(Date.now() - day + 25 * 60000).toISOString(),
    createdAt: new Date(Date.now() - day).toISOString(),
    elapsedMs: 25 * 60000,
    consent: { recording: "granted", externalProcessing: "granted" },
    settings: { summarizationEnabled: true, retainAudio: false, audioRetentionDays: 7 },
  });
  const segs = [
    seg(done, 1000, "それでは試作の範囲について確認します。"),
    seg(done, 6000, "試作は録音と文字起こしに限定しましょう。"),
    seg(done, 12000, "要約は後で検討で、佐藤さんが来週金曜までに試作版を用意します。"),
    seg(done, 18000, "システム音声を入れるかは次回のレビューで決めましょう。", "microphone"),
  ];
  store.meetings.set(done.id, done);
  store.segments.set(done.id, segs);
  const base = revision(done.id, sampleNote(done, segs, "base"), "user", null);
  const ai2 = revision(done.id, sampleNote(done, segs, "ai2"), "ai", base.id);
  store.notes.set(done.id, { current: base, pendingConflict: ai2, revisions: [base, ai2] });
  const failed: Job = { id: uuid(), meetingId: done.id, trigger: "final", status: "failed", attempt: 3, errorCode: "cli_not_found", retryAfter: null, createdAt: nowIso(), startedAt: nowIso(), finishedAt: nowIso() };
  store.jobs.set(failed.id, failed);

  const crashed = makeMeeting({
    id: MOCK_CRASHED_ID,
    title: "中断された会議（サンプル）",
    state: "recording",
    startedAt: new Date(Date.now() - 2 * 3600 * 1000).toISOString(),
    createdAt: new Date(Date.now() - 2 * 3600 * 1000).toISOString(),
    elapsedMs: 600000,
  });
  store.meetings.set(crashed.id, crashed);
  store.segments.set(crashed.id, [seg(crashed, 2000, "録音途中でアプリが終了した会議です。")]);
  return store;
}

let STORE: Store | null = null;
function store(): Store {
  if (!STORE) STORE = seed();
  return STORE;
}

// ---- events ------------------------------------------------------------------

class MockSocket implements WebSocketLike {
  readonly protocol = "sekretaer.v1";
  onopen: ((ev: unknown) => void) | null = null;
  onmessage: ((ev: { data: unknown }) => void) | null = null;
  onclose: ((ev: { code: number; reason?: string }) => void) | null = null;
  onerror: ((ev: unknown) => void) | null = null;
  constructor(cursor: number) {
    setTimeout(() => {
      this.onopen?.({});
      for (const e of store().log) if (e.seq > cursor) this.onmessage?.({ data: JSON.stringify(e) });
      store().sockets.add(this);
    }, 50);
  }
  close() {
    store().sockets.delete(this);
  }
}

function emit(event: EventName, meetingId: string | null, data: Record<string, unknown>): void {
  const s = store();
  s.seq += 1;
  const env: EventEnvelope = { event, eventId: uuid(), meetingId, seq: s.seq, occurredAt: nowIso(), data };
  if (event !== "transcript.partial" && event !== "audio.level") s.log.push(env);
  const raw = JSON.stringify(env);
  for (const sock of s.sockets) sock.onmessage?.({ data: raw });
}

export const mockWsFactory: WebSocketFactory = (url) => {
  const m = /cursor=(\d+)/.exec(url);
  return new MockSocket(m ? Number(m[1]) : Number.MAX_SAFE_INTEGER);
};

const PHRASES = [
  "本日の議題は三点あります。",
  "まず前回のアクションの進捗を確認します。",
  "デザインのレビューは今週中に終わる予定です。",
  "リリース日は来月の第二週で合意でよいでしょうか。",
  "担当は山田さんでお願いします。",
  "未解決の点は次回に持ち越します。",
];

const timers = new Map<string, ReturnType<typeof setInterval>>();

function startSimulation(m: Meeting): void {
  let t = 0;
  let phrase = 0;
  let partialId = uuid();
  let partialStart = 0;
  const handle = setInterval(() => {
    const cur = store().meetings.get(m.id);
    if (!cur || cur.state !== "recording") {
      clearInterval(handle);
      timers.delete(m.id);
      return;
    }
    t += 1000;
    for (const src of cur.sources) {
      emit("audio.level", m.id, { sourceId: src.id, level: 0.2 + 0.6 * Math.abs(Math.sin(t / 700 + src.offsetMs)) });
    }
    const text = PHRASES[phrase % PHRASES.length]!;
    const step = (t / 1000) % 5;
    if (step !== 0) {
      emit("transcript.partial", m.id, { segmentId: partialId, startMs: partialStart, endMs: t, text: text.slice(0, Math.ceil((text.length * step) / 5)), source: "microphone" });
    } else {
      const final = { ...seg(cur, partialStart, text), id: partialId, endMs: t };
      store().segments.get(m.id)!.push(final);
      emit("transcript.final", m.id, { segmentId: partialId, startMs: partialStart, endMs: t, text, source: "microphone", revision: 1 });
      phrase += 1;
      partialId = uuid();
      partialStart = t + 200;
    }
  }, 1000);
  timers.set(m.id, handle);
}

// ---- HTTP --------------------------------------------------------------------

function ok(data: unknown, status = 200): Response {
  return new Response(JSON.stringify({ requestId: uuid(), data }), { status, headers: { "Content-Type": "application/json" } });
}
function err(status: number, code: string, message: string): Response {
  return new Response(JSON.stringify({ requestId: uuid(), error: { code, message, details: {} } }), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

function runSummary(meetingId: string, trigger: Job["trigger"]): Job {
  const s = store();
  const job: Job = { id: uuid(), meetingId, trigger, status: "queued", attempt: 1, errorCode: null, retryAfter: null, createdAt: nowIso(), startedAt: null, finishedAt: null };
  s.jobs.set(job.id, job);
  emit("summary.queued", meetingId, { jobId: job.id, trigger });
  setTimeout(() => {
    job.status = "running";
    job.startedAt = nowIso();
  }, 800);
  setTimeout(() => {
    const m = s.meetings.get(meetingId);
    const segs = s.segments.get(meetingId) ?? [];
    if (!m || segs.length === 0) {
      job.status = "failed";
      job.errorCode = "summary_invalid_schema";
      emit("job.failed", meetingId, { jobId: job.id, errorCode: job.errorCode, willRetry: false, retryAfter: null });
      return;
    }
    const n = s.notes.get(meetingId) ?? { current: null, pendingConflict: null, revisions: [] };
    const rev = revision(meetingId, sampleNote(m, segs, "base"), "ai", n.current?.id ?? null);
    rev.jobId = job.id;
    n.current = rev;
    n.revisions.push(rev);
    s.notes.set(meetingId, n);
    job.status = "succeeded";
    job.finishedAt = nowIso();
    emit("summary.updated", meetingId, { jobId: job.id, revisionId: rev.id, conflict: false });
  }, 2500);
  return job;
}

async function route(method: string, path: string, query: URLSearchParams, body: unknown, headers: Headers): Promise<Response> {
  const s = store();
  await new Promise((r) => setTimeout(r, 120));
  if (!headers.get("Authorization")?.startsWith("Bearer ")) return err(401, "unauthorized", "認証が必要です。");
  const parts = path.split("/").filter(Boolean);
  const [a, id, b, c, d] = parts;

  if (method === "GET" && a === "health") {
    const health: Health = {
      status: "degraded",
      apiVersion: "1",
      stt: { state: "ready", model: s.settings.stt.model, device: s.settings.stt.device, computeType: s.settings.stt.computeType },
      vad: { state: "ready" },
      summarizer: { state: "cli_not_found", cliVersion: null },
      queue: { sttBacklogMs: 0, pendingJobs: 0 },
    };
    return ok(health);
  }
  if (method === "GET" && a === "devices") return ok({ devices });
  if (a === "settings") {
    if (method === "PUT") s.settings = body as AppSettings;
    return ok(s.settings);
  }
  if (method === "GET" && a === "privacy") {
    const list = [...s.meetings.values()];
    return ok({
      storage: { dataDir: "C:\\Users\\user\\AppData\\Roaming\\Sekretaer", dbPath: "C:\\Users\\user\\AppData\\Roaming\\Sekretaer\\sekretaer.db" },
      audioArchive: { enabledMeetings: list.filter((m) => m.settings.retainAudio).length },
      externalProcessing: { enabledMeetings: list.filter((m) => m.settings.summarizationEnabled).length, destination: "Claude Code CLI (claude -p)" },
    });
  }
  if (a === "jobs" && id) {
    const job = s.jobs.get(id);
    if (!job) return err(404, "not_found", "ジョブが見つかりません。");
    if (method === "POST" && b === "retry") {
      s.jobs.delete(id);
      return ok(runSummary(job.meetingId, job.trigger));
    }
    if (method === "POST" && b === "cancel") {
      job.status = "canceled";
      return ok(job);
    }
    return ok(job);
  }
  if (a !== "meetings") return err(404, "not_found", "見つかりません。");

  if (!id) {
    if (method === "GET") {
      const items = [...s.meetings.values()].filter((m) => m.state !== "deleted").sort((x, y) => y.createdAt.localeCompare(x.createdAt));
      return ok({ items, nextCursor: null });
    }
    const req = body as CreateMeetingRequest;
    const m = makeMeeting({
      title: req.title || "無題の会議",
      language: req.language,
      sources: req.sources.map((src) => ({ id: uuid(), kind: src.kind, deviceKey: src.deviceKey, sampleRate: 48000, channels: 1, permissionState: src.deviceKey === "mic-denied" ? "denied" : "granted", offsetMs: 0 })),
      consent: { recording: req.consent.recording ? "granted" : "unconfirmed", externalProcessing: req.consent.externalProcessing ? "granted" : "unconfirmed" },
      settings: req.settings,
    });
    s.meetings.set(m.id, m);
    s.segments.set(m.id, []);
    return ok(m);
  }
  const m = s.meetings.get(id);
  if (!m || m.state === "deleted") return err(404, "not_found", "会議が見つかりません。");

  if (!b) {
    if (method === "GET") return ok(m);
    if (method === "PATCH") {
      const p = body as { title?: string; settings?: Partial<Meeting["settings"]> };
      if (p.title !== undefined) m.title = p.title;
      if (p.settings) m.settings = { ...m.settings, ...p.settings };
      emit("privacy.state", m.id, {});
      return ok(m);
    }
    if (method === "DELETE") {
      m.state = "deleted";
      emit("meeting.state", m.id, { state: "deleted" });
      return ok({ deleted: { dbRecords: 42, audioFiles: 0, tempFiles: 3, exports: 1 }, failed: [], status: "deleted" });
    }
  }
  if (method === "POST" && b === "consent") {
    const p = body as { scope: string; granted: boolean };
    const v = p.granted ? "granted" : "denied";
    if (p.scope === "recording") m.consent.recording = v;
    else m.consent.externalProcessing = v;
    emit("privacy.state", m.id, {});
    return ok(m);
  }
  if (method === "POST" && b === "start") {
    const key = headers.get("Idempotency-Key") ?? "";
    const prev = s.idem.get(key);
    if (prev) return ok(prev);
    if (m.consent.recording !== "granted") return err(409, "consent_required", "録音の同意が確認されていません。参加者への通知と同意取得を確認してから開始してください。");
    if (m.sources.some((x) => x.permissionState === "denied")) return err(409, "permission_denied", "マイクへのアクセスが OS により拒否されています。OS のプライバシー設定でアクセスを許可してください。");
    if (m.sources.some((x) => x.deviceKey === "sys-loopback")) return err(409, "source_unavailable", "システム音声はこの OS 版では取得できません。マイクのみで開始できます。");
    m.state = "recording";
    m.startedAt = nowIso();
    s.idem.set(key, m);
    emit("meeting.state", m.id, { state: "recording", degraded: [] });
    startSimulation(m);
    return ok(m);
  }
  if (method === "POST" && (b === "stop" || b === "recover")) {
    m.state = "finalizing";
    emit("meeting.state", m.id, { state: "finalizing" });
    setTimeout(() => {
      m.state = "completed";
      m.endedAt = nowIso();
      emit("meeting.state", m.id, { state: "completed" });
      if (m.settings.summarizationEnabled && m.consent.externalProcessing === "granted") runSummary(m.id, "final");
    }, 1500);
    return ok(m, 202);
  }
  if (b === "transcript") {
    const segs = s.segments.get(m.id) ?? [];
    if (!c) return ok({ items: segs, nextCursor: null });
    const target = segs.find((x) => x.id === c);
    if (!target) return err(404, "not_found", "発話が見つかりません。");
    const hist = s.history.get(c) ?? [{ revision: 1, text: target.text, speakerLabel: target.speakerLabel, editedAt: nowIso(), origin: "stt" as const }];
    if (d === "history") return ok({ items: hist });
    const p = body as { text?: string; speakerLabel?: string | null };
    if (p.text !== undefined) target.text = p.text;
    if (p.speakerLabel !== undefined) target.speakerLabel = p.speakerLabel;
    target.revision += 1;
    hist.push({ revision: target.revision, text: target.text, speakerLabel: target.speakerLabel, editedAt: nowIso(), origin: "user" });
    s.history.set(c, hist);
    return ok(target);
  }
  if (b === "summaries") {
    if (c === "preview") {
      const segs = s.segments.get(m.id) ?? [];
      return ok({ fromMs: 0, toMs: segs.at(-1)?.endMs ?? 0, segmentCount: segs.length, text: segs.map((x) => `[${x.startMs}] ${x.text}`).join("\n") });
    }
    if (!m.settings.summarizationEnabled || m.consent.externalProcessing !== "granted") {
      return err(409, "consent_required", "この会議では Claude CLI へのテキスト送信が許可されていません。");
    }
    const job = runSummary(m.id, "manual");
    return ok({ jobId: job.id, deduplicated: false }, 202);
  }
  if (b === "notes") {
    const n = s.notes.get(m.id) ?? { current: null, pendingConflict: null, revisions: [] };
    if (method === "GET" && !c) return ok({ current: n.current, pendingConflict: n.pendingConflict });
    if (method === "GET" && c === "revisions") return ok({
        items: n.revisions.map((r) => ({ id: r.id, meetingId: r.meetingId, jobId: r.jobId, origin: r.origin, schemaVersion: r.schemaVersion, createdAt: r.createdAt, supersedesId: r.supersedesId })),
      });
    if (method === "PUT") {
      const p = body as { baseRevisionId: string | null; note: Note };
      if ((n.current?.id ?? null) !== p.baseRevisionId) return err(409, "conflict", "ノートがほかの更新で変更されています。");
      const rev = revision(m.id, p.note, "user", n.current?.id ?? null);
      n.current = rev;
      n.revisions.push(rev);
      s.notes.set(m.id, n);
      return ok(rev);
    }
    if (method === "POST" && c === "conflict") {
      const p = body as { action: string };
      if (p.action === "accept_ai" && n.pendingConflict) n.current = n.pendingConflict;
      n.pendingConflict = null;
      return ok(n.current);
    }
  }
  if (b === "jobs") return ok({ items: [...s.jobs.values()].filter((j) => j.meetingId === m.id) });
  if (b === "export") {
    const fmt = query.get("format");
    const n = s.notes.get(m.id)?.current?.note ?? null;
    if (fmt === "json") return ok({ filename: `${m.title}.json`, contentType: "application/json", content: JSON.stringify({ meeting: m, note: n }, null, 2) });
    return ok({ filename: `${m.title}.md`, contentType: "text/markdown", content: `# ${m.title}\n\n${n?.cornell.summary ?? ""}\n` });
  }
  return err(404, "not_found", "見つかりません。");
}

export const mockFetch: FetchLike = async (input, init) => {
  const url = new URL(input);
  const path = url.pathname.replace(/^\/api\/v1/, "");
  const body = typeof init?.body === "string" && init.body ? JSON.parse(init.body) : undefined;
  return route(init?.method ?? "GET", path, url.searchParams, body, new Headers(init?.headers));
};

export const MOCK_PORT = 45999;
export const MOCK_TOKEN = "mock-token";
