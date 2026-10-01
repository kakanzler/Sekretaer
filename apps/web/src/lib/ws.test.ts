import { describe, expect, it } from "vitest";
import { ev } from "./testFixtures";
import { backoffDelay, buildEventsUrl, buildSubprotocols, EventStream, type WebSocketLike } from "./ws";

class FakeSocket implements WebSocketLike {
  protocol = "sekretaer.v1";
  onopen: ((ev: unknown) => void) | null = null;
  onmessage: ((ev: { data: unknown }) => void) | null = null;
  onclose: ((ev: { code: number; reason?: string }) => void) | null = null;
  onerror: ((ev: unknown) => void) | null = null;
  closed = false;
  constructor(
    public url: string,
    public protocols: string[],
  ) {}
  close() {
    this.closed = true;
  }
  send(e: unknown) {
    this.onmessage?.({ data: JSON.stringify(e) });
  }
}

function harness() {
  const sockets: FakeSocket[] = [];
  const timers: { fn: () => void; ms: number }[] = [];
  const stream = new EventStream({
    port: 53124,
    token: "secret-token",
    wsFactory: (url, protocols) => {
      const s = new FakeSocket(url, protocols);
      sockets.push(s);
      return s;
    },
    backoff: { baseMs: 1000, maxMs: 8000, random: () => 0 },
    setTimer: (fn, ms) => {
      timers.push({ fn, ms });
      return timers.length;
    },
    clearTimer: () => undefined,
  });
  return { stream, sockets, timers };
}

describe("ws helpers", () => {
  it("puts the token only in subprotocols, never in the URL", () => {
    expect(buildSubprotocols("abc")).toEqual(["sekretaer.v1", "bearer.abc"]);
    expect(buildEventsUrl(53124, null)).toBe("ws://127.0.0.1:53124/api/v1/events");
    expect(buildEventsUrl(53124, 42)).toBe("ws://127.0.0.1:53124/api/v1/events?cursor=42");
  });

  it("backs off exponentially with a cap", () => {
    const d = (n: number) => backoffDelay(n, { baseMs: 1000, maxMs: 30000, random: () => 0 });
    expect([0, 1, 2, 3, 4, 5, 6].map(d)).toEqual([1000, 2000, 4000, 8000, 16000, 30000, 30000]);
    expect(backoffDelay(0, { baseMs: 1000, random: () => 1 })).toBe(1200);
  });
});

describe("EventStream", () => {
  it("connects with subprotocols, tracks cursor and reconnects with it", () => {
    const { stream, sockets, timers } = harness();
    const got: number[] = [];
    stream.subscribe((e) => got.push(e.seq));
    stream.start();
    expect(sockets).toHaveLength(1);
    expect(sockets[0]!.url).toBe("ws://127.0.0.1:53124/api/v1/events");
    expect(sockets[0]!.url).not.toContain("secret-token");
    expect(sockets[0]!.protocols).toEqual(["sekretaer.v1", "bearer.secret-token"]);
    sockets[0]!.onopen?.({});
    expect(stream.status).toBe("open");

    sockets[0]!.send(ev("transcript.final", 5, { segmentId: "a", startMs: 0, endMs: 1, text: "x", source: "microphone" }));
    sockets[0]!.send(ev("audio.level", 9, { level: 0.2 })); // ephemeral: delivered, cursor unchanged
    sockets[0]!.send(ev("summary.queued", 6, { jobId: "j", trigger: "manual" }));
    expect(stream.cursor).toBe(6);
    expect(got).toEqual([5, 9, 6]);

    sockets[0]!.onclose?.({ code: 1006 });
    expect(stream.status).toBe("reconnecting");
    expect(timers[0]!.ms).toBe(1000);
    timers[0]!.fn();
    expect(sockets[1]!.url).toBe("ws://127.0.0.1:53124/api/v1/events?cursor=6");

    // Replayed duplicates are dropped; newer events pass.
    sockets[1]!.send(ev("transcript.final", 5, { segmentId: "a", startMs: 0, endMs: 1, text: "x", source: "microphone" }));
    sockets[1]!.send(ev("meeting.state", 7, { state: "completed" }));
    expect(got).toEqual([5, 9, 6, 7]);
  });

  it("increases the delay across consecutive failures and resets after open", () => {
    const { stream, sockets, timers } = harness();
    stream.start();
    sockets[0]!.onclose?.({ code: 1006 });
    timers[0]!.fn();
    sockets[1]!.onclose?.({ code: 1006 });
    timers[1]!.fn();
    sockets[2]!.onclose?.({ code: 1006 });
    expect(timers.map((t) => t.ms)).toEqual([1000, 2000, 4000]);
    timers[2]!.fn();
    sockets[3]!.onopen?.({});
    sockets[3]!.onclose?.({ code: 1006 });
    expect(timers[3]!.ms).toBe(1000);
  });

  it("switches port/token immediately and keeps the cursor", () => {
    const { stream, sockets } = harness();
    stream.start();
    sockets[0]!.onopen?.({});
    sockets[0]!.send(ev("meeting.state", 12, { state: "recording" }));
    stream.updateConnection(60000, "new-token");
    expect(sockets[0]!.closed).toBe(true);
    expect(sockets[1]!.url).toBe("ws://127.0.0.1:60000/api/v1/events?cursor=12");
    expect(sockets[1]!.protocols).toEqual(["sekretaer.v1", "bearer.new-token"]);
  });

  it("ignores malformed messages and stops cleanly", () => {
    const { stream, sockets, timers } = harness();
    const got: unknown[] = [];
    stream.subscribe((e) => got.push(e));
    stream.start();
    expect(stream.handleMessage("{not json")).toBeNull();
    expect(stream.handleMessage(JSON.stringify({ foo: 1 }))).toBeNull();
    expect(got).toHaveLength(0);
    stream.stop();
    expect(sockets[0]!.closed).toBe(true);
    expect(stream.status).toBe("closed");
    expect(timers).toHaveLength(0);
  });
});
