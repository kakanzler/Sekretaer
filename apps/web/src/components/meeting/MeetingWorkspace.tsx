"use client";

import Link from "next/link";
import { useCallback, useMemo, useState } from "react";
import { useConnection } from "@/components/ConnectionProvider";
import { StartBlockedHelp } from "@/components/PermissionHelp";
import { Dialog, ErrorNotice, Loading } from "@/components/ui";
import { useMeetingSession } from "@/hooks/useMeetingSession";
import { useNow } from "@/hooks/useResource";
import { toApiError, type ApiError } from "@/lib/errors";
import { formatMs, formatSeconds } from "@/lib/format";
import { classifyRecording, markStartedHere, wasStartedHere } from "@/lib/liveness";
import { isProvisional } from "@/lib/meetingReducer";
import { diffNotes } from "@/lib/noteDiff";
import { prepareForSave } from "@/lib/noteEdit";
import type { DeleteResult, Meeting, Note, NotesState } from "@/lib/types";
import { ConflictBanner } from "./ConflictBanner";
import { DiffView } from "./DiffView";
import { EvidenceContext } from "./Evidence";
import { DeleteMeetingButton, DeleteResultView, ExportButtons, RecoveryPrompt } from "./MeetingActions";
import { NotePane } from "./NotePane";
import { PrivacyPanel } from "./PrivacyPanel";
import { StatusBar } from "./StatusBar";
import { SummaryControls } from "./SummaryControls";
import { TranscriptPane, type HighlightRequest } from "./TranscriptPane";

const LIVENESS_GRACE_MS = 6000;

function StartPrompt({ meeting, onMeeting }: { meeting: Meeting; onMeeting: (m: Meeting) => void }) {
  const { client, connKey } = useConnection();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<ApiError | null>(null);
  async function start() {
    if (!client) return;
    setBusy(true);
    setError(null);
    try {
      const m = await client.startMeeting(meeting.id);
      if (connKey) markStartedHere(m.id, connKey);
      onMeeting(m);
    } catch (e) {
      setError(toApiError(e));
    } finally {
      setBusy(false);
    }
  }
  return (
    <section className="notice info stack" aria-labelledby="start-title">
      <h2 id="start-title">この会議はまだ録音を開始していません</h2>
      <p>権限や入力デバイスを確認したうえで開始してください。同意の内容を変える場合は、新しい会議を作成してください。</p>
      <ErrorNotice error={error} title="録音を開始できません">
        <StartBlockedHelp error={error!} />
        <p className="small">
          <Link href="/settings/">アプリの設定を開く</Link> / <Link href="/meetings/new/">新しい会議を作成</Link>
        </p>
      </ErrorNotice>
      <div className="row">
        <button type="button" className="btn primary" onClick={start} disabled={busy || meeting.consent.recording !== "granted"}>
          {busy ? "開始しています…" : "録音を開始"}
        </button>
        {meeting.consent.recording !== "granted" && (
          <span className="small error-text">録音同意が記録されていないため開始できません。新しい会議を作成してください。</span>
        )}
      </div>
    </section>
  );
}

export function MeetingWorkspace({ meetingId }: { meetingId: string }) {
  const { client, connKey } = useConnection();
  const session = useMeetingSession(meetingId);
  const { state, dispatch, notes, setNotes } = session;
  const meeting = state.meeting;
  const recording = meeting?.state === "recording";
  const now = useNow(1000, recording);
  const [mountedAt] = useState(() => Date.now());
  const [highlight, setHighlight] = useState<HighlightRequest | null>(null);
  const [deleteResult, setDeleteResult] = useState<DeleteResult | null>(null);
  const [showAsLive, setShowAsLive] = useState(false);
  const [saveConflict, setSaveConflict] = useState<{ mine: Note; latest: NotesState } | null>(null);
  const [overwriteBusy, setOverwriteBusy] = useState(false);
  const [overwriteError, setOverwriteError] = useState<ApiError | null>(null);

  const onMeeting = useCallback((m: Meeting) => dispatch({ type: "snapshot/meeting", meeting: m, at: Date.now() }), [dispatch]);
  const jumpTo = useCallback((segmentId: string) => setHighlight((h) => ({ id: segmentId, n: (h?.n ?? 0) + 1 })), []);
  const evidence = useMemo(() => ({ transcript: state.transcript, jumpTo }), [state.transcript, jumpTo]);

  if (deleteResult) return <DeleteResultView result={deleteResult} title={meeting?.title ?? ""} />;
  if (!meeting) {
    if (session.loadError) {
      return (
        <ErrorNotice error={session.loadError} title="会議を読み込めません">
          <p>
            <Link href="/">会議一覧へ戻る</Link>
          </p>
        </ErrorNotice>
      );
    }
    return <Loading label="会議を読み込んでいます…" />;
  }
  if (meeting.state === "deleted" || meeting.state === "deleting") {
    return (
      <section className="panel" role="status">
        <h1>{meeting.state === "deleted" ? "この会議は削除されました" : "この会議は削除処理中です"}</h1>
        <Link href="/">会議一覧へ</Link>
      </section>
    );
  }

  const startedHere = connKey ? wasStartedHere(meeting.id, connKey) : false;
  const liveness = classifyRecording(meeting, { startedHere, lastLiveSignalAt: state.lastLiveSignalAt, now });
  const checking = liveness === "unknown" && now - mountedAt < LIVENESS_GRACE_MS && !showAsLive;
  const live = liveness === "live" || (liveness === "unknown" && showAsLive);
  const needsRecovery = liveness === "interrupted" || (liveness === "unknown" && !checking && !showAsLive);
  const summarizeEnabled = meeting.settings.summarizationEnabled && meeting.consent.externalProcessing === "granted";
  const editable = true;

  async function saveNote(note: Note): Promise<ApiError | null> {
    if (!client) return null;
    const base = notes?.current?.id ?? null;
    try {
      const rev = await client.putNote(meetingId, base, prepareForSave(note, new Date().toISOString()));
      setNotes({ current: rev, pendingConflict: notes?.pendingConflict ?? null });
      return null;
    } catch (e) {
      const err = toApiError(e);
      if (err.code === "conflict") {
        try {
          const latest = await client.notes(meetingId);
          setNotes(latest);
          setSaveConflict({ mine: note, latest });
        } catch {
          /* the error below is still shown */
        }
      }
      return err;
    }
  }

  async function overwriteWithMine() {
    if (!client || !saveConflict) return;
    setOverwriteBusy(true);
    setOverwriteError(null);
    try {
      const rev = await client.putNote(
        meetingId,
        saveConflict.latest.current?.id ?? null,
        prepareForSave(saveConflict.mine, new Date().toISOString()),
      );
      setNotes({ current: rev, pendingConflict: saveConflict.latest.pendingConflict });
      setSaveConflict(null);
    } catch (e) {
      setOverwriteError(toApiError(e));
    } finally {
      setOverwriteBusy(false);
    }
  }

  return (
    <EvidenceContext.Provider value={evidence}>
      <StatusBar state={state} live={live} onMeeting={onMeeting} />

      {meeting.state === "preparing" && <StartPrompt meeting={meeting} onMeeting={onMeeting} />}
      {checking && (
        <p className="notice info" role="status">
          録音の状態を確認しています…
        </p>
      )}
      {needsRecovery && (
        <RecoveryPrompt
          meeting={meeting}
          uncertain={liveness === "unknown"}
          onMeeting={onMeeting}
          onShowLive={() => setShowAsLive(true)}
        />
      )}
      {meeting.state === "error" && (
        <div className="notice danger" role="alert">
          この会議は保存障害のためエラー状態です。{meeting.processing.lastError ? `（${meeting.processing.lastError}）` : ""}
          ここまでに保存された逐語記録とノートは表示・書き出しできます。
        </div>
      )}
      {state.gaps.length > 0 && (
        <details className="notice warn">
          <summary>音声の欠落を {state.gaps.length} 件検出しました（欠落範囲は記録されています）</summary>
          <ul className="small">
            {state.gaps.map((g, i) => (
              <li key={i}>
                {formatMs(g.atMs)} から {formatSeconds(g.gapMs)}（{g.reason}）
              </li>
            ))}
          </ul>
        </details>
      )}
      {notes?.pendingConflict && (
        <ConflictBanner
          meetingId={meetingId}
          current={notes.current}
          conflict={notes.pendingConflict}
          onResolved={(rev) => setNotes({ current: rev, pendingConflict: null })}
        />
      )}

      <div className="workspace">
        <TranscriptPane
          meetingId={meetingId}
          transcript={state.transcript}
          live={live && recording}
          editable={editable}
          highlight={highlight}
          onSegmentUpdated={(segment) => dispatch({ type: "segment/updated", segment })}
        />
        <NotePane
          meetingId={meetingId}
          revision={notes?.current ?? null}
          provisional={isProvisional(state)}
          editable={editable && notes !== null}
          onSave={saveNote}
          error={session.notesError}
          controls={
            <SummaryControls
              state={state}
              enabled={summarizeEnabled}
              onJob={(job) => dispatch({ type: "job/updated", job, at: Date.now() })}
            />
          }
        />
      </div>

      {meeting.state !== "preparing" && (
        <section className="panel stack" aria-labelledby="export-title" style={{ marginTop: 16 }}>
          <h2 id="export-title">書き出し</h2>
          <p className="small muted">手修正を反映したノートと逐語記録を、Markdown またはスキーマ版付き JSON で保存します。</p>
          <ExportButtons meeting={meeting} />
        </section>
      )}

      <PrivacyPanel
        meeting={meeting}
        onMeeting={onMeeting}
        deleteSlot={<DeleteMeetingButton meeting={meeting} onDeleted={setDeleteResult} />}
      />

      <Dialog
        open={saveConflict !== null}
        onClose={() => setSaveConflict(null)}
        title="保存中にノートが更新されていました"
        wide
        footer={
          <>
            <button type="button" className="btn" onClick={() => setSaveConflict(null)} disabled={overwriteBusy}>
              最新の版を使う（自分の編集を破棄）
            </button>
            <button type="button" className="btn primary" onClick={overwriteWithMine} disabled={overwriteBusy}>
              自分の編集で上書き保存
            </button>
          </>
        }
      >
        {saveConflict && (
          <div className="stack">
            <p>編集を始めた後に、別の版（AI の要約または別の編集）が保存されていました。違いを確認してください。</p>
            <ErrorNotice error={overwriteError} title="保存できません" />
            {saveConflict.latest.current ? (
              <DiffView entries={diffNotes(saveConflict.latest.current.note, saveConflict.mine)} leftLabel="最新の版" rightLabel="自分の編集" />
            ) : (
              <p className="muted">最新の版はありません。</p>
            )}
          </div>
        )}
      </Dialog>
    </EvidenceContext.Provider>
  );
}
