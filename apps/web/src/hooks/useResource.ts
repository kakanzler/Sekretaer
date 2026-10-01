"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { useConnection } from "@/components/ConnectionProvider";
import type { ApiClient } from "@/lib/api";
import { toApiError, type ApiError } from "@/lib/errors";

export interface Resource<T> {
  data: T | undefined;
  error: ApiError | null;
  loading: boolean;
  reload: () => void;
  setData: (updater: (prev: T | undefined) => T | undefined) => void;
}

/**
 * Fetch a sidecar resource when the client is available and whenever `key`
 * changes (pass null to skip). Re-fetches on reload() and on client change.
 */
export function useResource<T>(key: string | null, fetcher: (client: ApiClient) => Promise<T>): Resource<T> {
  const { client } = useConnection();
  const [state, setState] = useState<{ key: string | null; data: T | undefined; error: ApiError | null }>({
    key: null,
    data: undefined,
    error: null,
  });
  const [tick, setTick] = useState(0);
  const fetcherRef = useRef(fetcher);
  useEffect(() => {
    fetcherRef.current = fetcher;
  });

  useEffect(() => {
    if (!client || key === null) return;
    let cancelled = false;
    fetcherRef.current(client).then(
      (data) => {
        if (!cancelled) setState({ key, data, error: null });
      },
      (e: unknown) => {
        if (!cancelled) setState((s) => ({ key, data: s.key === key ? s.data : undefined, error: toApiError(e) }));
      },
    );
    return () => {
      cancelled = true;
    };
  }, [client, key, tick]);

  const reload = useCallback(() => setTick((t) => t + 1), []);
  const setData = useCallback(
    (updater: (prev: T | undefined) => T | undefined) => setState((s) => ({ ...s, data: updater(s.data) })),
    [],
  );
  const current = state.key === key;
  return {
    data: current ? state.data : undefined,
    error: current ? state.error : null,
    loading: key !== null && (!current || (state.data === undefined && state.error === null)),
    reload,
    setData,
  };
}

/** Periodically call reload() while `active` is true. */
export function useInterval(fn: () => void, ms: number, active = true): void {
  const ref = useRef(fn);
  useEffect(() => {
    ref.current = fn;
  });
  useEffect(() => {
    if (!active) return;
    const h = setInterval(() => ref.current(), ms);
    return () => clearInterval(h);
  }, [ms, active]);
}

/** Re-render every `ms` while active and return Date.now() (for clocks). */
export function useNow(ms: number, active = true): number {
  const [now, setNow] = useState(() => Date.now());
  useInterval(() => setNow(Date.now()), ms, active);
  return now;
}
