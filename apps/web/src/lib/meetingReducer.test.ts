import { describe, expect, it } from "vitest";
import {
  computeElapsedMs,
  initialMeetingState,
  isProvisional,
  meetingReducer,
  normalizeLevel,
  parseLevels,
  unresolvedFailures,
  type MeetingAction,
  type MeetingViewState,
} from "./meetingReducer";
import { linesInOrder } from "./transcript";
import { MEETING_ID, ev, meeting, segment } from "./testFixtures";

function run(actions: MeetingAction[], start: MeetingViewState = initialMeetingState(MEETING_ID)): MeetingViewState {
  return actions.reduce(meetingReducer, start);
}

const withMeeting = (): MeetingViewState =>
  run([{ type: "snapshot/meeting", meeting: meeting(), at: 0 }]);

describe("meetingReducer: transcript", () => {
  it("shows a partial and replaces it with the final for the same segment", () => {
    let s = withMeeting();
    s = meetingReducer(s, { type: "event", now: 1, event: ev("transcript.partial", 1, { segmentId: "s1", startMs: 0, endMs: 500, text: "次回", source: "microphone" }) });
    expect(linesInOrder(s.transcript)).toMatchObject([{ id: "s1", isFinal: false, text: "次回" }]);
    s = meetingReducer(s, { type: "event", now: 2, event: ev("transcript.partial", 2, { segmentId: "s1", startMs: 0, endMs: 900, text: "次回まで", source: "microphone" }) });
    expect(linesInOrder(s.transcript)[0]!.text).toBe("次回まで");
    s = meetingReducer(s, { type: "event", now: 3, event: ev("transcript.final", 3, { segmentId: "s1", startMs: 0, endMs: 1200, text: "次回までに試作します。", source: "microphone", revision: 1 }) });
    const lines = linesInOrder(s.transcript);
    expect(lines).toHaveLength(1);
    expect(lines[0]).toMatchObject({ id: "s1", isFinal: true, text: "次回までに試作します。", sourceId: "src-mic" });
  });

  it("never lets a late partial overwrite a final", () => {
    let s = withMeeting();
    s = meetingReducer(s, { type: "event", now: 1, event: ev("transcript.final", 5, { segmentId: "s1", startMs: 0, endMs: 1000, text: "確定", source: "microphone" }) });
    s = meetingReducer(s, { type: "event", now: 2, event: ev("transcript.partial", 6, { segmentId: "s1", startMs: 0, endMs: 800, text: "暫定", source: "microphone" }) });
    expect(linesInOrder(s.transcript)[0]).toMatchObject({ isFinal: true, text: "確定" });
  });

  it("drops a stale partial with a different id once an overlapping final arrives", () => {
    let s = withMeeting();
    s = meetingReducer(s, { type: "event", now: 1, event: ev("transcript.partial", 1, { segmentId: "p-1", startMs: 100, endMs: 800, text: "暫", source: "microphone" }) });
    s = meetingReducer(s, { type: "event", now: 1, event: ev("transcript.partial", 2, { segmentId: "p-sys", startMs: 100, endMs: 800, text: "別ソース", source: "system" }) });
    s = meetingReducer(s, { type: "event", now: 2, event: ev("transcript.final", 3, { segmentId: "f-1", startMs: 0, endMs: 1000, text: "確定", source: "microphone" }) });
    expect(linesInOrder(s.transcript).map((l) => l.id)).toEqual(["f-1", "p-sys"]);
  });

  it("keeps segments ordered by start time and applies higher revisions only", () => {
    let s = run([{ type: "snapshot/transcript", segments: [segment({ id: "b", startMs: 5000, endMs: 6000 }), segment({ id: "a", startMs: 1000, endMs: 2000 })] }], withMeeting());
    expect(linesInOrder(s.transcript).map((l) => l.id)).toEqual(["a", "b"]);
    s = meetingReducer(s, { type: "segment/updated", segment: segment({ id: "a", startMs: 1000, endMs: 2000, text: "修正後", revision: 2, speakerLabel: "佐藤" }) });
    // A replayed STT final with the old revision must not undo the user edit.
    s = meetingReducer(s, { type: "event", now: 1, event: ev("transcript.final", 10, { segmentId: "a", startMs: 1000, endMs: 2000, text: "元", source: "microphone", revision: 1 }) });
    expect(s.transcript.byId.a).toMatchObject({ text: "修正後", revision: 2, speakerLabel: "佐藤" });
  });
});

describe("meetingReducer: cursor tracking", () => {
  it("tracks the highest persisted seq and ignores replayed duplicates", () => {
    let s = withMeeting();
    s = meetingReducer(s, { type: "event", now: 1, event: ev("transcript.final", 7, { segmentId: "s1", startMs: 0, endMs: 1, text: "一", source: "microphone" }) });
    expect(s.lastSeq).toBe(7);
    const before = s;
    s = meetingReducer(s, { type: "event", now: 2, event: ev("transcript.final", 7, { segmentId: "s1", startMs: 0, endMs: 1, text: "一（重複）", source: "microphone" }) });
    expect(s).toBe(before);
    s = meetingReducer(s, { type: "event", now: 3, event: ev("meeting.state", 6, { state: "completed" }) });
    expect(s.meeting!.state).toBe("recording");
  });

  it("does not advance the cursor on ephemeral events but records a live signal", () => {
    let s = withMeeting();
    s = meetingReducer(s, { type: "event", now: 1, event: ev("meeting.state", 3, { state: "recording", degraded: ["stt_queue_backlog"] }) });
    s = meetingReducer(s, { type: "event", now: 99, event: ev("audio.level", 50, { sourceId: "src-mic", level: 0.5 }) });
    expect(s.lastSeq).toBe(3);
    expect(s.lastLiveSignalAt).toBe(99);
    expect(s.levels["src-mic"]).toEqual({ level: 0.5, at: 99 });
    expect(s.meeting!.degraded).toEqual(["stt_queue_backlog"]);
  });

  it("ignores events for other meetings", () => {
    const s = withMeeting();
    const next = meetingReducer(s, { type: "event", now: 1, event: ev("meeting.state", 9, { state: "completed" }, "other") });
    expect(next).toBe(s);
  });
});

describe("meetingReducer: jobs and notes", () => {
  it("marks notes provisional while a job is queued and bumps notesVersion on update", () => {
    let s = withMeeting();
    s = meetingReducer(s, { type: "event", now: 1, event: ev("summary.queued", 1, { jobId: "j1", trigger: "silence" }) });
    expect(isProvisional(s)).toBe(true);
    s = meetingReducer(s, { type: "event", now: 2, event: ev("summary.updated", 2, { jobId: "j1", revisionId: "r1", conflict: true }) });
    expect(isProvisional(s)).toBe(false);
    expect(s.notesVersion).toBe(1);
    expect(s.conflictHint).toBe(true);
    s = meetingReducer(s, { type: "notes/fetched" });
    expect(s.conflictHint).toBe(false);
  });

  it("records failures with willRetry as retry_wait and others as failed", () => {
    let s = withMeeting();
    s = meetingReducer(s, { type: "event", now: 1, event: ev("summary.queued", 1, { jobId: "j1", trigger: "manual" }) });
    s = meetingReducer(s, { type: "event", now: 2, event: ev("job.failed", 2, { jobId: "j1", errorCode: "cli_timeout", willRetry: true, retryAfter: "2026-09-29T10:05:00+09:00" }) });
    expect(s.jobs.j1).toMatchObject({ status: "retry_wait", errorCode: "cli_timeout", trigger: "manual" });
    expect(isProvisional(s)).toBe(true);
    s = meetingReducer(s, { type: "event", now: 3, event: ev("job.failed", 3, { jobId: "j1", errorCode: "cli_not_found", willRetry: false }) });
    expect(s.jobs.j1!.status).toBe("failed");
    expect(unresolvedFailures(s).map((j) => j.id)).toEqual(["j1"]);
  });

  it("bumps privacyVersion on global privacy.state events", () => {
    const s = meetingReducer(withMeeting(), { type: "event", now: 1, event: ev("privacy.state", 4, {}, null) });
    expect(s.privacyVersion).toBe(1);
  });
});

describe("level parsing", () => {
  it("normalises dBFS, 0..1 and percent readings", () => {
    expect(normalizeLevel({ db: -60 })).toBe(0);
    expect(normalizeLevel({ db: 0 })).toBe(1);
    expect(normalizeLevel({ rms: 0.25 })).toBe(0.25);
    expect(normalizeLevel({ level: 50 })).toBe(0.5);
    expect(normalizeLevel({})).toBeNull();
  });
  it("accepts single and list payloads", () => {
    expect(parseLevels({ source: "system", peak: 0.3 })).toEqual([{ key: "system", level: 0.3 }]);
    expect(parseLevels({ levels: [{ sourceId: "a", level: 0.1 }, { sourceId: "b", db: -30 }] })).toEqual([
      { key: "a", level: 0.1 },
      { key: "b", level: 0.5 },
    ]);
  });
});

describe("computeElapsedMs", () => {
  it("uses wall clock from startedAt while recording and ended-started afterwards", () => {
    const start = Date.parse("2026-09-29T10:00:00+09:00");
    const m = meeting({ startedAt: "2026-09-29T10:00:00+09:00" });
    expect(computeElapsedMs(m, start + 65_000, null)).toBe(65_000);
    const done = meeting({ state: "completed", startedAt: "2026-09-29T10:00:00+09:00", endedAt: "2026-09-29T10:30:00+09:00" });
    expect(computeElapsedMs(done, start + 999_999_999, null)).toBe(30 * 60_000);
  });
  it("falls back to elapsedMs + local delta", () => {
    expect(computeElapsedMs(meeting({ elapsedMs: 10_000 }), 5_000, 2_000)).toBe(13_000);
  });
});
