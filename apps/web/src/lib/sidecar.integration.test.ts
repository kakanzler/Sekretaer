/**
 * UI ⇄ real sidecar integration (opt-in: SEKRETAER_INTEGRATION=1, run via `pnpm test:integration`).
 *
 * Spawns tests/integration/run_sidecar_stack.py — the production sidecar server with only the
 * hardware/model/network engines replaced — and drives it through this app's own ApiClient,
 * EventStream and meeting reducer: consent gate (AC-01), live partial/final transcript (AC-02),
 * persistent summary jobs (AC-03), schema-valid notes with evidence (AC-05), export (AC-10),
 * delete (AC-07) and token rejection (AC-08).
 */
import { spawn, type ChildProcessWithoutNullStreams } from "node:child_process";
import { mkdtempSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";
import { createInterface } from "node:readline";
import { fileURLToPath } from "node:url";
import { afterAll, beforeAll, describe, expect, it } from "vitest";
import { ApiClient } from "./api";
import { ApiError } from "./errors";
import type { EventEnvelope } from "./types";
import { EventStream } from "./ws";

const enabled =
  process.env.SEKRETAER_INTEGRATION === "1" || process.env.npm_lifecycle_event === "test:integration";
const repo = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../../../..");

interface Ready {
  port: number;
  token: string;
}

function launch(): Promise<{ child: ChildProcessWithoutNullStreams; ready: Ready }> {
  const dataDir = mkdtempSync(path.join(tmpdir(), "sekretaer-it-"));
  const child = spawn(
    "uv",
    ["run", "--project", path.join(repo, "sidecar"), "python", path.join(repo, "tests/integration/run_sidecar_stack.py"), "--data-dir", dataDir],
    { cwd: repo, stdio: ["pipe", "pipe", "pipe"] },
  );
  child.stderr.resume();
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => reject(new Error("sidecar did not become ready")), 60_000);
    createInterface({ input: child.stdout }).once("line", (line) => {
      clearTimeout(timer);
      const r = JSON.parse(line) as { type: string } & Ready;
      if (r.type !== "ready") reject(new Error("unexpected handshake"));
      else resolve({ child, ready: { port: r.port, token: r.token } });
    });
    child.once("exit", (code) => reject(new Error(`sidecar exited early (${code})`)));
  });
}

async function until<T>(fn: () => Promise<T | undefined> | T | undefined, what: string, timeoutMs = 60_000): Promise<T> {
  const end = Date.now() + timeoutMs;
  for (;;) {
    const v = await fn();
    if (v !== undefined && v !== null && v !== false) return v as T;
    if (Date.now() > end) throw new Error(`timed out waiting for ${what}`);
    await new Promise((r) => setTimeout(r, 200));
  }
}

describe.skipIf(!enabled)("UI client against the real sidecar", () => {
  let child: ChildProcessWithoutNullStreams;
  let client: ApiClient;
  let stream: EventStream;
  let ready: Ready;
  const events: EventEnvelope[] = [];

  beforeAll(async () => {
    ({ child, ready } = await launch());
    client = new ApiClient({ origin: `http://127.0.0.1:${ready.port}`, token: ready.token });
    stream = new EventStream({ port: ready.port, token: ready.token });
    stream.subscribe((ev) => events.push(ev));
    stream.start();
    await until(() => stream.status === "open", "websocket open", 10_000);
  }, 90_000);

  afterAll(async () => {
    stream?.stop();
    if (child && child.exitCode === null) {
      const exited = new Promise((r) => child.once("exit", r));
      child.stdin.end(); // contract: stdin EOF → sidecar exits
      await Promise.race([exited, new Promise((r) => setTimeout(r, 10_000))]);
      if (child.exitCode === null) child.kill();
    }
  });

  it("rejects requests without the token (AC-08)", async () => {
    const bad = new ApiClient({ origin: `http://127.0.0.1:${ready.port}`, token: "wrong" });
    await expect(bad.health()).rejects.toMatchObject({ code: "unauthorized", status: 401 });
  });

  it("runs a meeting end to end", async () => {
    const health = await client.health();
    expect(health.apiVersion).toBe("1");
    const { devices } = await client.devices();
    expect(devices.map((d) => d.key)).toContain("synthetic:it-mic");

    // AC-01: no recording consent → start refused with a reason.
    const blocked = await client.createMeeting({
      title: "同意なし",
      language: "ja",
      sources: [{ kind: "microphone", deviceKey: "synthetic:it-mic" }],
      consent: { recording: false, externalProcessing: false },
      settings: { summarizationEnabled: false, retainAudio: false, audioRetentionDays: 7 },
    });
    const err = await client.startMeeting(blocked.id).catch((e: unknown) => e);
    expect(err).toBeInstanceOf(ApiError);
    expect((err as ApiError).code).toBe("consent_required");
    await client.deleteMeeting(blocked.id);

    const m = await client.createMeeting({
      title: "結合テスト会議",
      language: "ja",
      sources: [{ kind: "microphone", deviceKey: "synthetic:it-mic" }],
      consent: { recording: true, externalProcessing: true },
      settings: { summarizationEnabled: true, retainAudio: false, audioRetentionDays: 7 },
    });
    const key = crypto.randomUUID();
    const started = await client.startMeeting(m.id, key);
    expect(started.state).toBe("recording");
    expect((await client.startMeeting(m.id, key)).state).toBe("recording"); // idempotent

    // AC-02: partial and final transcript events arrive over the socket while recording.
    await until(() => events.some((e) => e.event === "transcript.final" && e.meetingId === m.id), "final segment");
    await until(() => events.some((e) => e.event === "transcript.partial" && e.meetingId === m.id), "partial segment", 5_000);

    // AC-03: a manual summary request creates a persistent job while capture continues.
    const { jobId } = await client.requestSummary(m.id);
    const job = await until(async () => {
      const j = await client.job(jobId);
      return j.status === "succeeded" ? j : undefined;
    }, "manual summary job");
    expect(job.meetingId).toBe(m.id);
    expect((await client.getMeeting(m.id)).state).toBe("recording");

    const stopped = await client.stopMeeting(m.id);
    expect(["finalizing", "completed"]).toContain(stopped.state);
    await until(() => events.some((e) => e.event === "meeting.state" && e.meetingId === m.id && e.data.state === "completed"), "completed");
    await until(async () => {
      const jobs = (await client.meetingJobs(m.id)).items;
      return jobs.length > 0 && jobs.every((j) => ["succeeded", "failed", "canceled"].includes(j.status)) ? jobs : undefined;
    }, "all jobs settled");

    // AC-05: note evidence ids point at real segments.
    const notes = await client.notes(m.id);
    expect(notes.current).not.toBeNull();
    const segIds = new Set((await client.transcriptAll(m.id, false)).map((s) => s.id));
    const note = notes.current!.note;
    const evidenced = [...note.cornell.notes, ...note.decisions, ...note.actions, ...note.bullets];
    expect(evidenced.length).toBeGreaterThan(0);
    for (const item of evidenced) for (const id of item.evidenceSegmentIds) expect(segIds.has(id)).toBe(true);
    expect(events.some((e) => e.event === "summary.updated" && e.meetingId === m.id)).toBe(true);

    // AC-10: exports.
    const md = await client.exportMeeting(m.id, "markdown");
    expect(md.content).toContain("結合テスト会議");
    const json = JSON.parse((await client.exportMeeting(m.id, "json")).content) as { schemaVersion?: string };
    expect(json.schemaVersion).toBeDefined();

    // AC-07: deletion reports its result and the meeting is gone.
    const del = await client.deleteMeeting(m.id);
    expect(del.status).toBe("deleted");
    const gone = await client.getMeeting(m.id).catch((e: unknown) => e);
    expect(gone).toBeInstanceOf(ApiError);
  }, 180_000);
});
