"use client";

import { useState } from "react";
import { useConnection } from "@/components/ConnectionProvider";
import { Badge, ErrorNotice } from "@/components/ui";
import { useNow } from "@/hooks/useResource";
import { toApiError, type ApiError } from "@/lib/errors";
import { formatMs, formatMsSpoken, formatSeconds } from "@/lib/format";
import { MEETING_STATE_LABEL, PERMISSION_LABEL, SOURCE_LABEL, warningLabel } from "@/lib/labels";
import { computeElapsedMs, type MeetingViewState } from "@/lib/meetingReducer";
import type { Meeting, MeetingSource } from "@/lib/types";

const LEVEL_STALE_MS = 3000;
const BACKLOG_WARN_MS = 5000;

function levelFor(state: MeetingViewState, src: MeetingSource, now: number): number {
  const r = state.levels[src.id] ?? state.levels[src.kind] ?? state.levels.default;
  if (!r || now - r.at > LEVEL_STALE_MS) return 0;
  return r.level;
}

function LevelMeter({ label, level }: { label: string; level: number }) {
  const pct = Math.round(level * 100);
  return (
    <span className="meter">
      <span
        className="meter-track"
        role="meter"
        aria-label={`${label}の入力レベル`}
        aria-valuemin={0}
        aria-valuemax={100}
        aria-valuenow={pct}
      >
        <span className="meter-fill" style={{ width: `${pct}%` }} />
      </span>
    </span>
  );
}

export function StatusBar({
  state,
  live,
  onMeeting,
}: {
  state: MeetingViewState;
  /** Capture is believed to be running (shows level meters and the stop button). */
  live: boolean;
  onMeeting: (m: Meeting) => void;
}) {
  const { client } = useConnection();
  const meeting = state.meeting!;
  const recording = meeting.state === "recording";
  const now = useNow(recording ? 500 : 60_000, true);
  const elapsed = computeElapsedMs(meeting, now, state.snapshotAt);
  const [stopping, setStopping] = useState(false);
  const [error, setError] = useState<ApiError | null>(null);

  const recText = recording && live ? "録音中" : meeting.state === "finalizing" ? "確定処理中（録音は停止済み）" : recording ? "録音中（応答なし）" : "停止中";
  const recClass = recording && live ? "recording" : "stopped";

  const backlogMs = meeting.processing.sttBacklogMs;
  const warnings = new Set<string>(meeting.degraded);
  if (backlogMs >= BACKLOG_WARN_MS) warnings.add("stt_queue_backlog");
  for (const w of state.warnings.slice(-5)) warnings.add(w.code);

  async function stop() {
    if (!client) return;
    setStopping(true);
    setError(null);
    try {
      onMeeting(await client.stopMeeting(meeting.id));
    } catch (e) {
      setError(toApiError(e));
    } finally {
      setStopping(false);
    }
  }

  return (
    <div className="statusbar" aria-label="会議の状態">
      <h1>{meeting.title || "（無題）"}</h1>
      <span className={`rec-state ${recClass}`} role="status" aria-live="polite">
        <span className="rec-dot" aria-hidden="true" />
        {recText}
        {!recording && meeting.state !== "finalizing" && (
          <span className="muted small">（{MEETING_STATE_LABEL[meeting.state] ?? meeting.state}）</span>
        )}
      </span>
      <span>
        <span className="muted small">経過 </span>
        <time className="elapsed" aria-label={`経過時間 ${formatMsSpoken(elapsed)}`}>
          {formatMs(elapsed)}
        </time>
      </span>
      <span className="row" aria-label="入力ソース">
        {meeting.sources.map((s) => (
          <span key={s.id} className="row">
            <Badge tone={s.permissionState === "granted" ? "neutral" : "warn"}>
              {SOURCE_LABEL[s.kind]}
              {s.permissionState !== "granted" ? `（${PERMISSION_LABEL[s.permissionState]}）` : ""}
            </Badge>
            {recording && live && <LevelMeter label={SOURCE_LABEL[s.kind]} level={levelFor(state, s, now)} />}
          </span>
        ))}
      </span>
      {backlogMs > 0 && (
        <span className="small muted" aria-label="認識待ち">
          認識待ち {formatSeconds(backlogMs)}
        </span>
      )}
      {warnings.size > 0 && (
        <span className="row" role="alert">
          {[...warnings].map((w) => (
            <Badge key={w} tone="warn">
              ⚠ {warningLabel(w)}
            </Badge>
          ))}
        </span>
      )}
      {recording && live && (
        <button type="button" className="btn stop" onClick={stop} disabled={stopping} style={{ marginLeft: "auto" }}>
          {stopping ? "停止しています…" : "■ 録音を停止"}
        </button>
      )}
      {error && (
        <div style={{ flexBasis: "100%" }}>
          <ErrorNotice error={error} title="録音を停止できません" />
        </div>
      )}
    </div>
  );
}
