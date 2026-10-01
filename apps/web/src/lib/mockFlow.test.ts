// Integration: ApiClient + EventStream + meetingReducer against the opt-in mock sidecar.
import { describe, expect, it } from "vitest";
import { ApiClient, sidecarOrigin } from "./api";
import { ApiError } from "./errors";
import { initialMeetingState, meetingReducer, type MeetingViewState } from "./meetingReducer";
import { mockFetch, mockWsFactory, MOCK_PORT, MOCK_TOKEN } from "./mock";
import { linesInOrder } from "./transcript";
import { EventStream } from "./ws";

const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));

describe("mock sidecar flow", () => {
  it("refuses start without consent (AC-01), records live transcript, stops and deletes (AC-07)", async () => {
    const client = new ApiClient({ origin: sidecarOrigin(MOCK_PORT), token: MOCK_TOKEN, fetchImpl: mockFetch });
    const noConsent = await client.createMeeting({
      title: "同意なし",
      language: "ja",
      sources: [{ kind: "microphone", deviceKey: "mic-default" }],
      consent: { recording: false, externalProcessing: false },
      settings: { summarizationEnabled: false, retainAudio: false, audioRetentionDays: 7 },
    });
    const err = (await client.startMeeting(noConsent.id).catch((e) => e)) as ApiError;
    expect(err).toBeInstanceOf(ApiError);
    expect(err.code).toBe("consent_required");
    expect(err.status).toBe(409);

    const m = await client.createMeeting({
      title: "同意あり",
      language: "ja",
      sources: [{ kind: "microphone", deviceKey: "mic-default" }],
      consent: { recording: true, externalProcessing: true },
      settings: { summarizationEnabled: true, retainAudio: false, audioRetentionDays: 7 },
    });
    let state: MeetingViewState = meetingReducer(initialMeetingState(m.id), { type: "snapshot/meeting", meeting: m, at: Date.now() });
    const stream = new EventStream({ port: MOCK_PORT, token: MOCK_TOKEN, wsFactory: mockWsFactory });
    stream.subscribe((event) => {
      state = meetingReducer(state, { type: "event", event, now: Date.now() });
    });
    stream.start();
    await sleep(100);
    const started = await client.startMeeting(m.id, "key-1");
    expect(started.state).toBe("recording");
    expect((await client.startMeeting(m.id, "key-1")).id).toBe(m.id); // idempotent
    await sleep(5600);
    expect(state.meeting!.state).toBe("recording");
    const lines = linesInOrder(state.transcript);
    expect(lines.some((l) => l.isFinal)).toBe(true);
    expect(Object.keys(state.levels).length).toBeGreaterThan(0);

    const summary = await client.requestSummary(m.id);
    expect(summary.jobId).toBeTruthy();
    await client.stopMeeting(m.id);
    await sleep(3200);
    expect(state.meeting!.state).toBe("completed");
    expect(state.notesVersion).toBeGreaterThan(0);
    const notes = await client.notes(m.id);
    expect(notes.current?.note.schemaVersion).toBe("1.0");

    const del = await client.deleteMeeting(m.id);
    expect(del.status).toBe("deleted");
    stream.stop();
  }, 20_000);
});
