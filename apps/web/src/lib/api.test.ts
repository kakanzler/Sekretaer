import { describe, expect, it } from "vitest";
import { ApiClient, sidecarOrigin, type FetchLike } from "./api";
import { ApiError } from "./errors";

interface Captured {
  url: string;
  init: RequestInit;
}

function fakeFetch(status: number, body: unknown, headers: Record<string, string> = {}) {
  const calls: Captured[] = [];
  const impl: FetchLike = async (url, init) => {
    calls.push({ url, init: init ?? {} });
    const text = body === undefined ? "" : typeof body === "string" ? body : JSON.stringify(body);
    return new Response(text || null, { status, headers });
  };
  return { impl, calls };
}

const client = (impl: FetchLike) =>
  new ApiClient({ origin: sidecarOrigin(53124), token: "tok", fetchImpl: impl, newIdempotencyKey: () => "idem-1" });

describe("ApiClient envelope", () => {
  it("builds 127.0.0.1 URLs with the /api/v1 base and bearer header", async () => {
    const f = fakeFetch(200, { requestId: "r1", data: { status: "ok" } });
    const data = await client(f.impl).health();
    expect(data).toEqual({ status: "ok" });
    expect(f.calls[0]!.url).toBe("http://127.0.0.1:53124/api/v1/health");
    const h = f.calls[0]!.init.headers as Record<string, string>;
    expect(h.Authorization).toBe("Bearer tok");
    expect(h["Content-Type"]).toBeUndefined();
    expect(f.calls[0]!.init.method).toBe("GET");
  });

  it("maps error envelopes to ApiError with code, server message and requestId", async () => {
    const f = fakeFetch(409, {
      requestId: "req-9",
      error: { code: "permission_denied", message: "マイクの権限がありません。", details: { source: "microphone" } },
    });
    const err = await client(f.impl).startMeeting("m1").catch((e) => e);
    expect(err).toBeInstanceOf(ApiError);
    expect(err).toMatchObject({
      code: "permission_denied",
      status: 409,
      requestId: "req-9",
      userMessage: "マイクの権限がありません。",
      details: { source: "microphone" },
    });
    expect(err.settingsHint).toBe("os_privacy");
  });

  it("falls back to a Japanese message when the server omits one and to X-Request-Id", async () => {
    const f = fakeFetch(409, { error: { code: "consent_required" } }, { "X-Request-Id": "hdr-1" });
    const err = (await client(f.impl).requestSummary("m1").catch((e) => e)) as ApiError;
    expect(err.code).toBe("consent_required");
    expect(err.requestId).toBe("hdr-1");
    expect(err.userMessage).toMatch(/同意/);
  });

  it("classifies non-envelope failures", async () => {
    const e401 = (await client(fakeFetch(401, "nope").impl).health().catch((e) => e)) as ApiError;
    expect(e401.code).toBe("unauthorized");
    const e500 = (await client(fakeFetch(500, "<html>").impl).health().catch((e) => e)) as ApiError;
    expect(e500.code).toBe("invalid_response");
    const ok = (await client(fakeFetch(200, "not json").impl).health().catch((e) => e)) as ApiError;
    expect(ok.code).toBe("invalid_response");
  });

  it("maps network failures to network_error", async () => {
    const impl: FetchLike = async () => {
      throw new TypeError("Failed to fetch");
    };
    const err = (await client(impl).health().catch((e) => e)) as ApiError;
    expect(err).toMatchObject({ code: "network_error", status: 0, requestId: null });
  });

  it("sends JSON content type and Idempotency-Key on start", async () => {
    const f = fakeFetch(200, { requestId: "r", data: { id: "m1", state: "recording" } });
    await client(f.impl).startMeeting("m 1");
    const c = f.calls[0]!;
    expect(c.url).toBe("http://127.0.0.1:53124/api/v1/meetings/m%201/start");
    expect(c.init.method).toBe("POST");
    const h = c.init.headers as Record<string, string>;
    expect(h["Idempotency-Key"]).toBe("idem-1");
    expect(h["Content-Type"]).toBe("application/json");
    expect(c.init.body).toBe("{}");
  });

  it("puts notes with baseRevisionId and builds query strings", async () => {
    const f = fakeFetch(200, { requestId: "r", data: {} });
    const c = client(f.impl);
    await c.putNote("m1", "rev-1", { schemaVersion: "1.0" } as never);
    expect(JSON.parse(f.calls[0]!.init.body as string)).toEqual({ baseRevisionId: "rev-1", note: { schemaVersion: "1.0" } });
    await c.transcriptPage("m1", { limit: 10, includePartial: true, cursor: null });
    expect(f.calls[1]!.url).toBe("http://127.0.0.1:53124/api/v1/meetings/m1/transcript?limit=10&includePartial=true");
    await c.exportMeeting("m1", "json");
    expect(f.calls[2]!.url).toMatch(/\/export\?format=json$/);
  });

  it("follows nextCursor when loading the whole transcript", async () => {
    const pages = [
      { requestId: "r", data: { items: [{ id: "a" }], nextCursor: "c2" } },
      { requestId: "r", data: { items: [{ id: "b" }], nextCursor: null } },
    ];
    const urls: string[] = [];
    const impl: FetchLike = async (url) => {
      urls.push(url);
      return new Response(JSON.stringify(pages[urls.length - 1]), { status: 200 });
    };
    const items = await client(impl).transcriptAll("m1", false);
    expect(items.map((i) => i.id)).toEqual(["a", "b"]);
    expect(urls[1]).toMatch(/cursor=c2/);
  });

  it("DELETE sends no body but keeps the JSON content type", async () => {
    const f = fakeFetch(200, { requestId: "r", data: { status: "deleted", deleted: {}, failed: [] } });
    await client(f.impl).deleteMeeting("m1");
    expect(f.calls[0]!.init.method).toBe("DELETE");
    expect(f.calls[0]!.init.body).toBeUndefined();
  });
});
