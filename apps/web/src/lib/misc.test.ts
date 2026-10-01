import { describe, expect, it } from "vitest";
import { parseConnectionInfo, parseDevInput } from "./connection";
import { formatCertainty, formatMs, formatRange, plainText, splitForHighlight } from "./format";
import { classifyRecording, connectionKey, markStartedHere, wasStartedHere } from "./liveness";
import { filterLines } from "./transcript";
import { meeting, segment } from "./testFixtures";

describe("format", () => {
  it("formats meeting-relative ms as mm:ss / h:mm:ss", () => {
    expect(formatMs(0)).toBe("00:00");
    expect(formatMs(18_200)).toBe("00:18");
    expect(formatMs(65_999)).toBe("01:05");
    expect(formatMs(3_600_000 + 61_000)).toBe("1:01:01");
    expect(formatMs(-5)).toBe("00:00");
    expect(formatRange(0, 420_000)).toBe("00:00–07:00");
  });
  it("presents certainty as a hint, not a probability", () => {
    expect(formatCertainty(0.724)).toBe("目安 72%");
    expect(formatCertainty(null)).toBe("目安 —");
  });
  it("strips control characters but keeps newlines", () => {
    expect(plainText("a\u0000b\nc\u001b")).toBe("ab\nc");
    expect(plainText("<b>x</b> **y**")).toBe("<b>x</b> **y**");
  });
  it("splits text for search highlighting (width/case-insensitive)", () => {
    expect(splitForHighlight("次回までに試作", "試作")).toEqual([
      { text: "次回までに", hit: false },
      { text: "試作", hit: true },
    ]);
    expect(splitForHighlight("Hello hello", "HELLO").filter((p) => p.hit)).toHaveLength(2);
  });
});

describe("transcript search", () => {
  it("matches text and speaker labels", () => {
    const lines = [segment({ id: "a", text: "予算の件" }), segment({ id: "b", text: "別件", speakerLabel: "佐藤" })];
    expect(filterLines(lines, "予算").map((l) => l.id)).toEqual(["a"]);
    expect(filterLines(lines, "佐藤").map((l) => l.id)).toEqual(["b"]);
    expect(filterLines(lines, " ")).toHaveLength(2);
  });
});

describe("connection parsing", () => {
  it("validates sidecar_connection payloads", () => {
    expect(parseConnectionInfo({ state: "ready", port: 53124, token: "t" })).toEqual({ state: "ready", port: 53124, token: "t", error: null });
    expect(parseConnectionInfo({ state: "failed", error: "x" }).state).toBe("failed");
    expect(parseConnectionInfo({ state: "bogus", port: "80", token: "" })).toEqual({ state: "starting", port: null, token: null, error: null });
    expect(parseConnectionInfo(null).state).toBe("starting");
  });
  it("accepts a port + token or the ready line JSON", () => {
    expect(parseDevInput(" 53124 ", " tok ")).toEqual({ port: 53124, token: "tok" });
    expect(parseDevInput('{"type":"ready","apiVersion":"1","port":5,"token":"x","pid":1}', "")).toEqual({ port: 5, token: "x" });
    expect(parseDevInput("99999", "t")).toBeNull();
    expect(parseDevInput("123", "")).toBeNull();
  });
});

class MemStorage implements Storage {
  private m = new Map<string, string>();
  get length() {
    return this.m.size;
  }
  clear() {
    this.m.clear();
  }
  getItem(k: string) {
    return this.m.get(k) ?? null;
  }
  key(i: number) {
    return [...this.m.keys()][i] ?? null;
  }
  removeItem(k: string) {
    this.m.delete(k);
  }
  setItem(k: string, v: string) {
    this.m.set(k, v);
  }
}

describe("liveness", () => {
  it("never stores the token and distinguishes sidecar processes", () => {
    const k1 = connectionKey(53124, "token-A");
    expect(k1).not.toContain("token-A");
    expect(connectionKey(53124, "token-B")).not.toBe(k1);
    const st = new MemStorage();
    markStartedHere("m1", k1, st);
    expect(wasStartedHere("m1", k1, st)).toBe(true);
    expect(wasStartedHere("m1", connectionKey(53124, "token-B"), st)).toBe(false);
  });

  it("classifies recording meetings", () => {
    const now = 100_000;
    const rec = meeting({ state: "recording" });
    expect(classifyRecording(meeting({ state: "completed" }), { startedHere: false, lastLiveSignalAt: null, now })).toBe("not_recording");
    expect(classifyRecording(rec, { startedHere: true, lastLiveSignalAt: null, now })).toBe("live");
    expect(classifyRecording(rec, { startedHere: false, lastLiveSignalAt: now - 2000, now })).toBe("live");
    expect(classifyRecording(rec, { startedHere: false, lastLiveSignalAt: now - 60_000, now })).toBe("unknown");
    expect(classifyRecording(meeting({ state: "recording", degraded: ["interrupted"] }), { startedHere: true, lastLiveSignalAt: now, now })).toBe(
      "interrupted",
    );
  });
});
