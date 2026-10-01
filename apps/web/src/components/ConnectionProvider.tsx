"use client";

import { createContext, useCallback, useContext, useEffect, useRef, useState, type ReactNode } from "react";
import { ApiClient, sidecarOrigin, type FetchLike } from "@/lib/api";
import {
  clearDevConnection,
  getTauriConnection,
  isTauri,
  listenTauriConnection,
  loadDevConnection,
  parseDevInput,
  saveDevConnection,
  type SidecarConnectionInfo,
  type SidecarPhase,
} from "@/lib/connection";
import { connectionKey } from "@/lib/liveness";
import type { EventEnvelope } from "@/lib/types";
import { EventStream, type StreamStatus, type WebSocketFactory } from "@/lib/ws";

type Mode = "tauri" | "dev" | "mock";
type Phase = "init" | "dev-setup" | SidecarPhase;

interface ConnectionContextValue {
  mode: Mode | null;
  phase: Phase;
  error: string | null;
  client: ApiClient | null;
  events: EventStream | null;
  streamStatus: StreamStatus;
  /** Identifies the current sidecar process without exposing the token. */
  connKey: string | null;
  connectDev: (portText: string, tokenText: string) => boolean;
  disconnectDev: () => void;
}

const ConnectionContext = createContext<ConnectionContextValue | null>(null);

export function useConnection(): ConnectionContextValue {
  const ctx = useContext(ConnectionContext);
  if (!ctx) throw new Error("useConnection must be used inside ConnectionProvider");
  return ctx;
}

/** Subscribe to sidecar events for the lifetime of the component. */
export function useSidecarEvents(handler: (ev: EventEnvelope) => void): void {
  const { events } = useConnection();
  const ref = useRef(handler);
  useEffect(() => {
    ref.current = handler;
  });
  useEffect(() => {
    if (!events) return;
    return events.subscribe((ev) => ref.current(ev));
  }, [events]);
}

interface Transport {
  fetchImpl?: FetchLike;
  wsFactory?: WebSocketFactory;
}

export function ConnectionProvider({ children }: { children: ReactNode }) {
  const [mode, setMode] = useState<Mode | null>(null);
  const [phase, setPhase] = useState<Phase>("init");
  const [error, setError] = useState<string | null>(null);
  const [client, setClient] = useState<ApiClient | null>(null);
  const [events, setEvents] = useState<EventStream | null>(null);
  const [streamStatus, setStreamStatus] = useState<StreamStatus>("idle");
  const [connKey, setConnKey] = useState<string | null>(null);
  const streamRef = useRef<EventStream | null>(null);
  const transportRef = useRef<Transport>({});
  const currentRef = useRef<{ port: number; token: string } | null>(null);

  const applyInfo = useCallback((info: SidecarConnectionInfo) => {
    setPhase(info.state);
    setError(info.error);
    if (info.state !== "ready" || !info.port || !info.token) return;
    const { port, token } = info;
    const cur = currentRef.current;
    if (cur && cur.port === port && cur.token === token) return;
    currentRef.current = { port, token };
    setClient(new ApiClient({ origin: sidecarOrigin(port), token, fetchImpl: transportRef.current.fetchImpl }));
    setConnKey(connectionKey(port, token));
    let es = streamRef.current;
    if (!es) {
      es = new EventStream({ port, token, wsFactory: transportRef.current.wsFactory });
      es.onStatus((s) => setStreamStatus(s));
      streamRef.current = es;
      es.start();
    } else {
      es.updateConnection(port, token);
    }
    setEvents(es);
  }, []);

  useEffect(() => {
    let disposed = false;
    let unlisten: (() => void) | null = null;
    (async () => {
      // Compared inline (not via a helper) so the bundler drops the mock
      // import entirely when the flag is not "1" at build time.
      if (process.env.NEXT_PUBLIC_SEKRETAER_MOCK === "1") {
        const mock = await import("@/lib/mock");
        if (disposed) return;
        transportRef.current = { fetchImpl: mock.mockFetch, wsFactory: mock.mockWsFactory };
        setMode("mock");
        applyInfo({ state: "ready", port: mock.MOCK_PORT, token: mock.MOCK_TOKEN, error: null });
        return;
      }
      if (isTauri()) {
        setMode("tauri");
        try {
          const un = await listenTauriConnection((info) => {
            if (!disposed) applyInfo(info);
          });
          if (disposed) un();
          else unlisten = un;
          const info = await getTauriConnection();
          if (!disposed) applyInfo(info);
        } catch (e) {
          if (!disposed) {
            setPhase("crashed");
            setError(e instanceof Error ? e.message : String(e));
          }
        }
        return;
      }
      await Promise.resolve();
      if (disposed) return;
      setMode("dev");
      const saved = loadDevConnection();
      if (saved) applyInfo({ state: "ready", port: saved.port, token: saved.token, error: null });
      else setPhase("dev-setup");
    })();
    return () => {
      disposed = true;
      unlisten?.();
      streamRef.current?.stop();
      streamRef.current = null;
      currentRef.current = null;
    };
  }, [applyInfo]);

  const connectDev = useCallback(
    (portText: string, tokenText: string) => {
      const parsed = parseDevInput(portText, tokenText);
      if (!parsed) return false;
      saveDevConnection(parsed);
      applyInfo({ state: "ready", port: parsed.port, token: parsed.token, error: null });
      return true;
    },
    [applyInfo],
  );

  const disconnectDev = useCallback(() => {
    clearDevConnection();
    streamRef.current?.stop();
    streamRef.current = null;
    currentRef.current = null;
    setClient(null);
    setEvents(null);
    setConnKey(null);
    setStreamStatus("idle");
    setPhase("dev-setup");
  }, []);

  return (
    <ConnectionContext.Provider
      value={{ mode, phase, error, client, events, streamStatus, connKey, connectDev, disconnectDev }}
    >
      {children}
    </ConnectionContext.Provider>
  );
}
