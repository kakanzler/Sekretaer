"use client";

import Link from "next/link";
import { useState } from "react";
import { useConnection } from "@/components/ConnectionProvider";
import { Badge, Dialog, ErrorNotice } from "@/components/ui";
import { downloadText } from "@/lib/browser";
import { toApiError, type ApiError } from "@/lib/errors";
import type { DeleteResult, Meeting } from "@/lib/types";

export function ExportButtons({ meeting, disabled }: { meeting: Meeting; disabled?: boolean }) {
  const { client } = useConnection();
  const [busy, setBusy] = useState<"markdown" | "json" | null>(null);
  const [error, setError] = useState<ApiError | null>(null);
  const [message, setMessage] = useState("");

  async function doExport(format: "markdown" | "json") {
    if (!client) return;
    setBusy(format);
    setError(null);
    setMessage("");
    try {
      const r = await client.exportMeeting(meeting.id, format);
      downloadText(r.filename, r.contentType, r.content);
      setMessage(`${r.filename} を書き出しました。`);
    } catch (e) {
      setError(toApiError(e));
    } finally {
      setBusy(null);
    }
  }

  return (
    <div className="stack">
      <div className="row">
        <button type="button" className="btn" disabled={disabled || busy !== null} onClick={() => doExport("markdown")}>
          {busy === "markdown" ? "書き出し中…" : "Markdown で書き出し"}
        </button>
        <button type="button" className="btn" disabled={disabled || busy !== null} onClick={() => doExport("json")}>
          {busy === "json" ? "書き出し中…" : "JSON で書き出し（スキーマ版付き）"}
        </button>
        <span className="small muted" role="status" aria-live="polite">
          {message}
        </span>
      </div>
      <ErrorNotice error={error} title="書き出せません" />
    </div>
  );
}

const DELETE_KIND_LABEL: Record<string, string> = {
  dbRecords: "DB レコード",
  audioFiles: "音声ファイル",
  tempFiles: "一時ファイル・キャッシュ",
  exports: "アプリ管理のエクスポート",
};

/** AC-07: the success/failure breakdown returned by DELETE /meetings/{id}. */
export function DeleteResultView({ result, title }: { result: DeleteResult; title: string }) {
  return (
    <section className="panel stack" role="status" aria-live="polite" aria-labelledby="delete-result-title">
      <h1 id="delete-result-title">削除結果: {title || "（無題）"}</h1>
      {result.status === "deleted" ? (
        <p className="notice ok">削除が完了しました。</p>
      ) : (
        <p className="notice warn">一部のデータを削除できませんでした（delete_incomplete）。下の一覧を確認してください。</p>
      )}
      <h2>削除できたもの</h2>
      <dl className="kv">
        {Object.entries(result.deleted).map(([k, v]) => (
          <div key={k} style={{ display: "contents" }}>
            <dt>{DELETE_KIND_LABEL[k] ?? k}</dt>
            <dd>{v} 件</dd>
          </div>
        ))}
      </dl>
      <h2>
        削除できなかったもの <Badge tone={result.failed.length ? "danger" : "ok"}>{result.failed.length} 件</Badge>
      </h2>
      {result.failed.length === 0 ? (
        <p className="small muted">ありません。</p>
      ) : (
        <ul>
          {result.failed.map((f, i) => (
            <li key={i}>
              {DELETE_KIND_LABEL[f.kind] ?? f.kind}: <code>{f.path}</code> — {f.reason}
            </li>
          ))}
        </ul>
      )}
      <p className="small muted">OS のバックアップや、ご自身で保存したコピーはアプリからは削除できません。</p>
      <p>
        <Link href="/" className="btn primary">
          会議一覧へ
        </Link>
      </p>
    </section>
  );
}

/** AC-07: confirmation listing what will be deleted. The result is shown by DeleteResultView. */
export function DeleteMeetingButton({ meeting, onDeleted }: { meeting: Meeting; onDeleted: (r: DeleteResult) => void }) {
  const { client } = useConnection();
  const [open, setOpen] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<ApiError | null>(null);
  const blocked = meeting.state === "recording" || meeting.state === "finalizing";

  async function doDelete() {
    if (!client) return;
    setBusy(true);
    setError(null);
    try {
      const r = await client.deleteMeeting(meeting.id);
      setOpen(false);
      onDeleted(r);
    } catch (e) {
      setError(toApiError(e));
    } finally {
      setBusy(false);
    }
  }

  return (
    <>
      <button type="button" className="btn danger" onClick={() => setOpen(true)} disabled={blocked}>
        この会議を削除…
      </button>
      {blocked && <span className="small muted">録音を停止して確定した後に削除できます。</span>}
      <Dialog
        open={open}
        onClose={() => setOpen(false)}
        title="会議を削除しますか？"
        footer={
          <>
            <button type="button" className="btn" onClick={() => setOpen(false)} disabled={busy}>
              キャンセル
            </button>
            <button type="button" className="btn danger" onClick={doDelete} disabled={busy}>
              {busy ? "削除しています…" : "削除する"}
            </button>
          </>
        }
      >
        <div className="stack">
          <p>
            「{meeting.title || "（無題）"}」について、アプリが管理する次のデータを削除します。この操作は取り消せません。
          </p>
          <ul>
            <li>会議情報、逐語記録（訂正履歴を含む）、ノートの全版、要約ジョブ、同意記録などの DB レコード</li>
            <li>音声ファイル{meeting.settings.retainAudio ? "（この会議は音声を保持しています）" : "（この会議は音声を保持していません）"}</li>
            <li>派生キャッシュと一時ファイル</li>
            <li>アプリが管理するエクスポート済みファイル</li>
          </ul>
          <p className="small muted">
            OS のバックアップや、書き出してご自身で保存したファイルなどのコピーはアプリからは削除できません。必要に応じてご自身で削除してください。
          </p>
          <ErrorNotice error={error} title="削除できませんでした" />
        </div>
      </Dialog>
    </>
  );
}

/** Crash recovery (spec §9): never auto-resume capture; offer finalize only. */
export function RecoveryPrompt({
  meeting,
  uncertain,
  onMeeting,
  onShowLive,
}: {
  meeting: Meeting;
  /** true when we could not tell whether capture is still running. */
  uncertain: boolean;
  onMeeting: (m: Meeting) => void;
  onShowLive: () => void;
}) {
  const { client } = useConnection();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<ApiError | null>(null);

  async function finalize() {
    if (!client) return;
    setBusy(true);
    setError(null);
    try {
      onMeeting(await client.recoverMeeting(meeting.id));
    } catch (e) {
      setError(toApiError(e));
    } finally {
      setBusy(false);
    }
  }

  return (
    <section className="notice warn stack" role="alert" aria-labelledby="recover-title">
      <h2 id="recover-title">この会議は「録音中」のまま中断されています</h2>
      <p>
        アプリまたはローカル処理が異常終了した可能性があります。録音は自動では再開しません。
        ここまでの逐語記録は保持されています。「録音を終了して確定」を選ぶと、残りの処理（確定・最終要約）を行って会議を完了します。
      </p>
      {uncertain && (
        <p className="small">
          別のウィンドウで録音が続いている可能性もあります。その場合は「録音中として表示」を選んでください。
        </p>
      )}
      <ErrorNotice error={error} title="確定できません" />
      <div className="row">
        <button type="button" className="btn primary" onClick={finalize} disabled={busy}>
          {busy ? "確定しています…" : "録音を終了して確定"}
        </button>
        {uncertain && (
          <button type="button" className="btn" onClick={onShowLive} disabled={busy}>
            録音中として表示
          </button>
        )}
      </div>
    </section>
  );
}
