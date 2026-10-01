"use client";

import { useEffect, useState } from "react";
import { useConnection } from "@/components/ConnectionProvider";
import { Badge, Dialog, ErrorNotice, Loading } from "@/components/ui";
import { hasPreviewAck, setPreviewAck } from "@/lib/browser";
import { guidanceFor, toApiError, type ApiError } from "@/lib/errors";
import { formatDateTime, formatRange } from "@/lib/format";
import { JOB_STATUS_LABEL, JOB_TRIGGER_LABEL } from "@/lib/labels";
import { jobsNewestFirst, unresolvedFailures, type JobView, type MeetingViewState } from "@/lib/meetingReducer";
import { EXTERNAL_DESTINATION, EXTERNAL_SEND_SCOPE } from "@/lib/policy";
import type { Job, SummaryPreview } from "@/lib/types";

function PreviewDialog({
  meetingId,
  open,
  onClose,
  onConfirm,
  busy,
}: {
  meetingId: string;
  open: boolean;
  onClose: () => void;
  onConfirm: () => void;
  busy: boolean;
}) {
  const { client } = useConnection();
  const [preview, setPreview] = useState<SummaryPreview | null>(null);
  const [error, setError] = useState<ApiError | null>(null);
  useEffect(() => {
    if (!open || !client) return;
    let cancelled = false;
    client.summaryPreview(meetingId).then(
      (p) => {
        if (!cancelled) {
          setPreview(p);
          setError(null);
        }
      },
      (e: unknown) => {
        if (!cancelled) setError(toApiError(e));
      },
    );
    return () => {
      cancelled = true;
      setPreview(null);
    };
  }, [open, client, meetingId]);

  return (
    <Dialog
      open={open}
      onClose={onClose}
      title="送信内容の確認"
      wide
      footer={
        <>
          <button type="button" className="btn" onClick={onClose}>
            キャンセル
          </button>
          <button type="button" className="btn primary" onClick={onConfirm} disabled={busy || !preview || preview.segmentCount === 0}>
            この内容を送信して要約
          </button>
        </>
      }
    >
      <div className="stack">
        <p>
          <strong>送信先:</strong> {EXTERNAL_DESTINATION}
        </p>
        <p className="small">
          送信されるのは下のテキスト（{EXTERNAL_SEND_SCOPE.sent.join("、")}）です。{EXTERNAL_SEND_SCOPE.notSent.join("、")}は送信しません。
        </p>
        <ErrorNotice error={error} title="送信内容を取得できません" />
        {!preview && !error && <Loading />}
        {preview && (
          <>
            <p className="small">
              対象範囲 {formatRange(preview.fromMs, preview.toMs)} / 発話 {preview.segmentCount} 件 / {preview.text.length} 文字
            </p>
            {preview.segmentCount === 0 ? (
              <p className="muted">送信する新しい確定発話はまだありません。</p>
            ) : (
              <pre className="preview-text" tabIndex={0} aria-label="送信されるテキスト">
                {preview.text}
              </pre>
            )}
          </>
        )}
      </div>
    </Dialog>
  );
}

function JobRow({ job, onUpdated }: { job: JobView; onUpdated: (j: Job) => void }) {
  const { client } = useConnection();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<ApiError | null>(null);
  async function act(kind: "retry" | "cancel") {
    if (!client) return;
    setBusy(true);
    setError(null);
    try {
      onUpdated(kind === "retry" ? await client.retryJob(job.id) : await client.cancelJob(job.id));
    } catch (e) {
      setError(toApiError(e));
    } finally {
      setBusy(false);
    }
  }
  const tone = job.status === "failed" ? "danger" : job.status === "succeeded" ? "ok" : job.status === "canceled" ? "neutral" : "info";
  return (
    <li className="card">
      <div className="row small">
        <Badge tone={tone}>{JOB_STATUS_LABEL[job.status]}</Badge>
        <span>{job.trigger ? JOB_TRIGGER_LABEL[job.trigger] : "要約"}</span>
        {job.createdAt && <span className="muted">{formatDateTime(job.createdAt)}</span>}
        {job.attempt !== null && job.attempt > 1 && <span className="muted">試行 {job.attempt} 回目</span>}
        {(job.status === "queued" || job.status === "running" || job.status === "retry_wait") && (
          <button type="button" className="btn small" disabled={busy} onClick={() => act("cancel")}>
            キャンセル
          </button>
        )}
        {job.status === "failed" && (
          <button type="button" className="btn small primary" disabled={busy} onClick={() => act("retry")}>
            再試行
          </button>
        )}
      </div>
      {job.errorCode && (
        <p className="small">
          {guidanceFor(job.errorCode).message} <span className="muted">（診断コード: <code>{job.errorCode}</code>）</span>
          {job.status === "retry_wait" && job.retryAfter && <> 自動再試行予定: {formatDateTime(job.retryAfter)}</>}
        </p>
      )}
      <ErrorNotice error={error} />
    </li>
  );
}

export function SummaryControls({
  state,
  onJob,
  enabled,
}: {
  state: MeetingViewState;
  onJob: (j: Job) => void;
  /** External processing granted and summarization enabled for this meeting. */
  enabled: boolean;
}) {
  const { client } = useConnection();
  const meetingId = state.meetingId;
  const [previewOpen, setPreviewOpen] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<ApiError | null>(null);
  const [message, setMessage] = useState("");

  const jobs = jobsNewestFirst(state).slice(0, 5);
  const failures = unresolvedFailures(state);

  async function send() {
    if (!client) return;
    setBusy(true);
    setError(null);
    try {
      const r = await client.requestSummary(meetingId);
      setPreviewAck(meetingId);
      setPreviewOpen(false);
      setMessage(r.deduplicated ? "同じ内容の要約ジョブに合流しました。" : "要約ジョブを登録しました。");
      client.job(r.jobId).then(onJob, () => undefined);
    } catch (e) {
      setError(toApiError(e));
      setPreviewOpen(false);
    } finally {
      setBusy(false);
    }
  }

  function summarizeNow() {
    setMessage("");
    if (!hasPreviewAck(meetingId)) setPreviewOpen(true);
    else void send();
  }

  return (
    <div className="stack" style={{ marginBottom: 12 }}>
      <div className="row">
        <button type="button" className="btn primary" onClick={summarizeNow} disabled={!enabled || busy || !client}>
          今まとめる
        </button>
        <button type="button" className="btn" onClick={() => setPreviewOpen(true)} disabled={!enabled || !client}>
          送信内容を確認
        </button>
        <span className="small muted" role="status" aria-live="polite">
          {message}
        </span>
      </div>
      {!enabled && (
        <p className="small muted">
          この会議では Claude CLI へのテキスト送信が無効のため、要約は作成されません。下の「プライバシー」欄から有効にできます。
        </p>
      )}
      <ErrorNotice error={error} title="要約を依頼できません" />
      {failures.length > 0 && (
        <div className="notice warn" role="alert">
          <strong>要約に失敗しました。</strong> 逐語記録はそのまま保持されています。原因を解消してから再試行できます。
        </div>
      )}
      {jobs.length > 0 && (
        <details open={failures.length > 0 || jobs.some((j) => j.status !== "succeeded" && j.status !== "canceled")}>
          <summary className="small">要約ジョブ（最新 {jobs.length} 件）</summary>
          <ul style={{ listStyle: "none", padding: 0 }} aria-live="polite">
            {jobs.map((j) => (
              <JobRow key={j.id} job={j} onUpdated={onJob} />
            ))}
          </ul>
        </details>
      )}
      <PreviewDialog meetingId={meetingId} open={previewOpen} onClose={() => setPreviewOpen(false)} onConfirm={send} busy={busy} />
    </div>
  );
}
