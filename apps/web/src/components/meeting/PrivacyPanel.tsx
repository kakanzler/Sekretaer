"use client";

import Link from "next/link";
import { useId, useState } from "react";
import { useConnection } from "@/components/ConnectionProvider";
import { Badge, Dialog, ErrorNotice } from "@/components/ui";
import { useResource } from "@/hooks/useResource";
import { toApiError, type ApiError } from "@/lib/errors";
import { CONSENT_LABEL, PERMISSION_LABEL, SOURCE_LABEL } from "@/lib/labels";
import { CONSENT_POLICY_VERSION, EXTERNAL_DESTINATION, EXTERNAL_SEND_SCOPE } from "@/lib/policy";
import type { Meeting } from "@/lib/types";

const RETENTION_CHOICES = [1, 7, 30, 90];

/** Per-meeting privacy state: capture targets, storage, external processing, consent record. */
export function PrivacyPanel({
  meeting,
  onMeeting,
  deleteSlot,
}: {
  meeting: Meeting;
  onMeeting: (m: Meeting) => void;
  deleteSlot: React.ReactNode;
}) {
  const { client } = useConnection();
  const privacy = useResource("privacy", (c) => c.privacy());
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<ApiError | null>(null);
  const [confirmOpen, setConfirmOpen] = useState(false);
  const daysId = useId();

  const externalOn = meeting.settings.summarizationEnabled && meeting.consent.externalProcessing === "granted";

  async function run(fn: () => Promise<Meeting>) {
    setBusy(true);
    setError(null);
    try {
      onMeeting(await fn());
    } catch (e) {
      setError(toApiError(e));
    } finally {
      setBusy(false);
    }
  }

  async function setExternal(on: boolean) {
    if (!client) return;
    await run(async () => {
      // Withdrawing consent first cancels queued jobs server-side before disabling.
      let m = await client.recordConsent(meeting.id, {
        scope: "external_processing",
        granted: on,
        policyVersion: CONSENT_POLICY_VERSION,
      });
      if (m.settings.summarizationEnabled !== on) m = await client.patchMeeting(meeting.id, { settings: { summarizationEnabled: on } });
      return m;
    });
    setConfirmOpen(false);
  }

  return (
    <section className="panel stack" aria-labelledby="privacy-title">
      <h2 id="privacy-title">プライバシー</h2>
      <ErrorNotice error={error} title="設定を変更できません" />
      <div className="health-grid">
        <div className="health-item">
          <h3>録音対象</h3>
          <ul className="note-list small">
            {meeting.sources.map((s) => (
              <li key={s.id}>
                {SOURCE_LABEL[s.kind]}（{s.deviceKey}）: 権限 {PERMISSION_LABEL[s.permissionState]}
                <span className="muted">
                  {" "}
                  / {s.sampleRate} Hz / {s.channels}ch
                </span>
              </li>
            ))}
          </ul>
        </div>
        <div className="health-item">
          <h3>保存先</h3>
          {privacy.data ? (
            <dl className="kv small">
              <dt>データ</dt>
              <dd>
                <code>{privacy.data.storage.dataDir}</code>
              </dd>
              <dt>DB</dt>
              <dd>
                <code>{privacy.data.storage.dbPath}</code>
              </dd>
            </dl>
          ) : (
            <p className="small muted">{privacy.error ? "取得できませんでした" : "確認中…"}</p>
          )}
          <p className="small">
            音声の保持:{" "}
            <Badge tone={meeting.settings.retainAudio ? "warn" : "neutral"}>
              {meeting.settings.retainAudio ? `保持する（${meeting.settings.audioRetentionDays} 日）` : "保持しない"}
            </Badge>
          </p>
          <label className="check small">
            <input
              type="checkbox"
              checked={meeting.settings.retainAudio}
              disabled={busy || !client}
              onChange={(e) => client && run(() => client.patchMeeting(meeting.id, { settings: { retainAudio: e.target.checked } }))}
            />
            この会議の音声を保持する
          </label>
          {meeting.settings.retainAudio && (
            <div className="row small">
              <label htmlFor={daysId}>保持期間</label>
              <select
                id={daysId}
                value={meeting.settings.audioRetentionDays}
                disabled={busy || !client}
                onChange={(e) =>
                  client && run(() => client.patchMeeting(meeting.id, { settings: { audioRetentionDays: Number(e.target.value) } }))
                }
              >
                {[...new Set([...RETENTION_CHOICES, meeting.settings.audioRetentionDays])].sort((a, b) => a - b).map((d) => (
                  <option key={d} value={d}>
                    {d} 日
                  </option>
                ))}
              </select>
            </div>
          )}
        </div>
        <div className="health-item">
          <h3>外部処理（要約）</h3>
          <p className="small">
            <Badge tone={externalOn ? "warn" : "neutral"}>{externalOn ? "送信する" : "送信しない"}</Badge> 送信先: {EXTERNAL_DESTINATION}
          </p>
          <p className="small muted">音声は送信しません。送信するのは逐語記録のテキストと最小限の直近ノートだけです。</p>
          {externalOn ? (
            <button type="button" className="btn small" disabled={busy} onClick={() => setExternal(false)}>
              送信を停止（同意を撤回）
            </button>
          ) : (
            <button type="button" className="btn small" disabled={busy} onClick={() => setConfirmOpen(true)}>
              この会議で送信を有効にする…
            </button>
          )}
        </div>
        <div className="health-item">
          <h3>同意記録</h3>
          <dl className="kv small">
            <dt>録音</dt>
            <dd>{CONSENT_LABEL[meeting.consent.recording]}</dd>
            <dt>外部処理</dt>
            <dd>{CONSENT_LABEL[meeting.consent.externalProcessing]}</dd>
          </dl>
          <p className="small">
            <Link href="/settings/privacy/">プライバシーと同意について</Link>
          </p>
        </div>
      </div>
      <div className="row">{deleteSlot}</div>

      <Dialog
        open={confirmOpen}
        onClose={() => setConfirmOpen(false)}
        title="Claude CLI への送信を有効にする"
        footer={
          <>
            <button type="button" className="btn" onClick={() => setConfirmOpen(false)}>
              キャンセル
            </button>
            <button type="button" className="btn primary" disabled={busy} onClick={() => setExternal(true)}>
              同意して有効にする
            </button>
          </>
        }
      >
        <div className="stack">
          <p>
            送信先: <strong>{EXTERNAL_DESTINATION}</strong>
          </p>
          <strong>送信されるもの</strong>
          <ul>
            {EXTERNAL_SEND_SCOPE.sent.map((s) => (
              <li key={s}>{s}</li>
            ))}
          </ul>
          <strong>送信されないもの</strong>
          <ul>
            {EXTERNAL_SEND_SCOPE.notSent.map((s) => (
              <li key={s}>{s}</li>
            ))}
          </ul>
          <p className="small muted">
            組織で利用する場合、CLI の利用契約・組織設定・データ処理条件が会議テキストに適用されるかを管理者に確認してください。
            同意はポリシー版 {CONSENT_POLICY_VERSION} とともに記録されます。
          </p>
        </div>
      </Dialog>
    </section>
  );
}
