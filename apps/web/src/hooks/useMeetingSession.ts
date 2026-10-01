"use client";

import { useCallback, useEffect, useReducer, useState } from "react";
import { useConnection, useSidecarEvents } from "@/components/ConnectionProvider";
import { toApiError, type ApiError } from "@/lib/errors";
import {
  activeJobs,
  initialMeetingState,
  meetingReducer,
  type MeetingAction,
  type MeetingViewState,
} from "@/lib/meetingReducer";
import type { EventEnvelope, NotesState } from "@/lib/types";
import { useInterval } from "./useResource";

export interface MeetingSession {
  state: MeetingViewState;
  dispatch: (a: MeetingAction) => void;
  loadError: ApiError | null;
  notes: NotesState | null;
  notesError: ApiError | null;
  setNotes: (n: NotesState) => void;
  reloadNotes: () => void;
  reloadMeeting: () => void;
  reloadJobs: () => void;
}

/**
 * Loads a meeting's REST snapshot (meeting, transcript, jobs, notes) and keeps
 * it current with WS events through the pure meetingReducer.
 */
export function useMeetingSession(meetingId: string): MeetingSession {
  const { client } = useConnection();
  const [state, dispatch] = useReducer(meetingReducer, meetingId, initialMeetingState);
  const [loadError, setLoadError] = useState<ApiError | null>(null);
  const [notes, setNotes] = useState<NotesState | null>(null);
  const [notesError, setNotesError] = useState<ApiError | null>(null);
  const [meetingTick, setMeetingTick] = useState(0);
  const [notesTick, setNotesTick] = useState(0);
  const [jobsTick, setJobsTick] = useState(0);
  // Events first, so nothing that arrives while the snapshot loads is lost.
  useSidecarEvents(
    useCallback(
      (ev: EventEnvelope) => {
        dispatch({ type: "event", event: ev, now: Date.now() });
        const mine = ev.meetingId === meetingId;
        if ((mine && ev.event === "meeting.state") || (ev.event === "privacy.state" && (mine || !ev.meetingId))) {
          setMeetingTick((t) => t + 1);
        }
        if (ev.meetingId === meetingId && (ev.event === "summary.queued" || ev.event === "job.failed")) {
          setJobsTick((t) => t + 1);
        }
      },
      [meetingId],
    ),
  );

  // Full snapshot on mount / reconnect.
  useEffect(() => {
    if (!client) return;
    let cancelled = false;
    (async () => {
      try {
        const meeting = await client.getMeeting(meetingId);
        if (cancelled) return;
        dispatch({ type: "snapshot/meeting", meeting, at: Date.now() });
        setLoadError(null);
        const live = meeting.state === "recording" || meeting.state === "preparing";
        const [segments, jobs] = await Promise.all([
          client.transcriptAll(meetingId, live),
          client.meetingJobs(meetingId).catch(() => ({ items: [] })),
        ]);
        if (cancelled) return;
        dispatch({ type: "snapshot/transcript", segments });
        dispatch({ type: "snapshot/jobs", jobs: jobs.items, at: Date.now() });
      } catch (e) {
        if (!cancelled) setLoadError(toApiError(e));
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [client, meetingId]);

  // Meeting refresh (state changes, privacy changes, periodic while recording).
  useEffect(() => {
    if (!client || meetingTick === 0) return;
    let cancelled = false;
    client.getMeeting(meetingId).then(
      (meeting) => {
        if (!cancelled) dispatch({ type: "snapshot/meeting", meeting, at: Date.now() });
      },
      (e: unknown) => {
        if (!cancelled) setLoadError(toApiError(e));
      },
    );
    return () => {
      cancelled = true;
    };
  }, [client, meetingId, meetingTick]);

  // Notes: on mount, when summary.updated arrives, or on demand.
  useEffect(() => {
    if (!client) return;
    let cancelled = false;
    client.notes(meetingId).then(
      (n) => {
        if (cancelled) return;
        setNotes(n);
        setNotesError(null);
        dispatch({ type: "notes/fetched" });
      },
      (e: unknown) => {
        if (!cancelled) setNotesError(toApiError(e));
      },
    );
    return () => {
      cancelled = true;
    };
  }, [client, meetingId, state.notesVersion, notesTick]);

  // Jobs: refresh on queue/failure events and poll while any job is active.
  useEffect(() => {
    if (!client || jobsTick === 0) return;
    let cancelled = false;
    client.meetingJobs(meetingId).then(
      (r) => {
        if (!cancelled) dispatch({ type: "snapshot/jobs", jobs: r.items, at: Date.now() });
      },
      () => {
        /* job list is advisory; keep event-derived state */
      },
    );
    return () => {
      cancelled = true;
    };
  }, [client, meetingId, jobsTick]);

  const hasActive = activeJobs(state).length > 0;
  useInterval(() => setJobsTick((t) => t + 1), 4000, hasActive);
  useInterval(() => setMeetingTick((t) => t + 1), 5000, state.meeting?.state === "recording" || state.meeting?.state === "finalizing");

  return {
    state,
    dispatch,
    loadError,
    notes,
    notesError,
    setNotes,
    reloadNotes: useCallback(() => setNotesTick((t) => t + 1), []),
    reloadMeeting: useCallback(() => setMeetingTick((t) => t + 1), []),
    reloadJobs: useCallback(() => setJobsTick((t) => t + 1), []),
  };
}
