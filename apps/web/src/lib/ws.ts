// WebSocket client for WS /api/v1/events (contracts/api-v1.md "Events").
// - Auth via subprotocols ["sekretaer.v1", "bearer.<token>"]; the token never
//   appears in the URL.
// - Reconnects with ?cursor=<last persisted seq> and exponential backoff.
// - De-duplicates replayed events by seq; ephemeral events (partial, level) are
//   never replayed and do not advance the cursor.

import { EPHEMERAL_EVENTS, type EventEnvelope } from "./types";

export const WS_PROTOCOL = "sekretaer.v1";

export function buildSubprotocols(token: string): string[] {
  return [WS_PROTOCOL, `bearer.${token}`];
}

export function buildEventsUrl(port: number, cursor: number | null): string {
  const base = `ws://127.0.0.1:${port}/api/v1/events`;
  return cursor !== null && cursor > 0 ? `${base}?cursor=${cursor}` : base;
}

export interface BackoffOptions {
  baseMs?: number;
  maxMs?: number;
  /** 0..1 random source; jitter adds up to 20 %. Pass () => 0 for determinism. */
  random?: () => number;
}

export function backoffDelay(attempt: number, opts: BackoffOptions = {}): number {
  const base = opts.baseMs ?? 1000;
  const max = opts.maxMs ?? 30_000;
  const rnd = opts.random ?? Math.random;
  const exp = Math.min(max, base * 2 ** Math.max(0, attempt));
  return Math.min(max, Math.round(exp * (1 + 0.2 * rnd())));
}

/** Minimal WebSocket surface so tests and the mock can inject a fake. */
export interface WebSocketLike {
  readonly protocol: string;
  onopen: ((ev: unknown) => void) | null;
  onmessage: ((ev: { data: unknown }) => void) | null;
  onclose: ((ev: { code: number; reason?: string }) => void) | null;
  onerror: ((ev: unknown) => void) | null;
  close(code?: number, reason?: string): void;
}

export type WebSocketFactory = (url: string, protocols: string[]) => WebSocketLike;

export type StreamStatus = "idle" | "connecting" | "open" | "reconnecting" | "closed";

export interface EventStreamOptions {
  port: number;
  token: string;
  initialCursor?: number | null;
  wsFactory?: WebSocketFactory;
  backoff?: BackoffOptions;
  setTimer?: (fn: () => void, ms: number) => unknown;
  clearTimer?: (handle: unknown) => void;
}

type Listener = (ev: EventEnvelope) => void;
type StatusListener = (status: StreamStatus, info: { attempt: number; nextDelayMs: number | null }) => void;

function isEnvelope(v: unknown): v is EventEnvelope {
  if (!v || typeof v !== "object") return false;
  const o = v as Record<string, unknown>;
  return typeof o.event === "string" && typeof o.seq === "number" && typeof o.data === "object" && o.data !== null;
}

export class EventStream {
  private opts: EventStreamOptions;
  private ws: WebSocketLike | null = null;
  private listeners = new Set<Listener>();
  private statusListeners = new Set<StatusListener>();
  private attempt = 0;
  private timer: unknown = null;
  private stopped = true;
  private _cursor: number;
  private _status: StreamStatus = "idle";

  constructor(opts: EventStreamOptions) {
    this.opts = opts;
    this._cursor = opts.initialCursor ?? 0;
  }

  /** Highest persisted (replayable) seq seen so far. */
  get cursor(): number {
    return this._cursor;
  }

  get status(): StreamStatus {
    return this._status;
  }

  subscribe(fn: Listener): () => void {
    this.listeners.add(fn);
    return () => this.listeners.delete(fn);
  }

  onStatus(fn: StatusListener): () => void {
    this.statusListeners.add(fn);
    return () => this.statusListeners.delete(fn);
  }

  start(): void {
    if (!this.stopped) return;
    this.stopped = false;
    this.connect();
  }

  stop(): void {
    this.stopped = true;
    this.clearTimer();
    const ws = this.ws;
    this.ws = null;
    if (ws) {
      ws.onclose = null;
      ws.onmessage = null;
      ws.onerror = null;
      ws.onopen = null;
      ws.close(1000, "client stop");
    }
    this.setStatus("closed", null);
  }

  /** Switch to a new sidecar port/token (e.g. after a sidecar restart); keeps the cursor. */
  updateConnection(port: number, token: string): void {
    if (port === this.opts.port && token === this.opts.token) return;
    this.opts = { ...this.opts, port, token };
    if (!this.stopped) {
      this.stop();
      this.stopped = false;
      this.attempt = 0;
      this.connect();
    }
  }

  /** Feed a raw message (exposed for tests). Returns the parsed event if it was delivered. */
  handleMessage(raw: unknown): EventEnvelope | null {
    let parsed: unknown = raw;
    if (typeof raw === "string") {
      try {
        parsed = JSON.parse(raw);
      } catch {
        return null;
      }
    }
    if (!isEnvelope(parsed)) return null;
    const ev = parsed;
    if (!EPHEMERAL_EVENTS.has(ev.event)) {
      if (ev.seq <= this._cursor) return null; // replay duplicate
      this._cursor = ev.seq;
    }
    for (const l of this.listeners) {
      try {
        l(ev);
      } catch (e) {
        console.error("event listener failed", e);
      }
    }
    return ev;
  }

  private factory(): WebSocketFactory {
    return this.opts.wsFactory ?? ((url, protocols) => new WebSocket(url, protocols) as unknown as WebSocketLike);
  }

  private connect(): void {
    if (this.stopped) return;
    this.clearTimer();
    this.setStatus(this.attempt === 0 ? "connecting" : "reconnecting", null);
    const url = buildEventsUrl(this.opts.port, this._cursor > 0 ? this._cursor : null);
    let ws: WebSocketLike;
    try {
      ws = this.factory()(url, buildSubprotocols(this.opts.token));
    } catch {
      this.scheduleReconnect();
      return;
    }
    this.ws = ws;
    ws.onopen = () => {
      this.attempt = 0;
      this.setStatus("open", null);
    };
    ws.onmessage = (ev) => {
      this.handleMessage(ev.data);
    };
    ws.onerror = () => {
      /* onclose follows */
    };
    ws.onclose = () => {
      if (this.ws === ws) this.ws = null;
      this.scheduleReconnect();
    };
  }

  private scheduleReconnect(): void {
    if (this.stopped) return;
    const delay = backoffDelay(this.attempt, this.opts.backoff);
    this.attempt += 1;
    this.setStatus("reconnecting", delay);
    const set = this.opts.setTimer ?? ((fn: () => void, ms: number) => setTimeout(fn, ms));
    this.timer = set(() => {
      this.timer = null;
      this.connect();
    }, delay);
  }

  private clearTimer(): void {
    if (this.timer !== null) {
      const clear = this.opts.clearTimer ?? ((h: unknown) => clearTimeout(h as ReturnType<typeof setTimeout>));
      clear(this.timer);
      this.timer = null;
    }
  }

  private setStatus(status: StreamStatus, nextDelayMs: number | null): void {
    this._status = status;
    for (const l of this.statusListeners) l(status, { attempt: this.attempt, nextDelayMs });
  }
}
