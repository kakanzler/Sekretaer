"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useId, useRef, useState, type FormEvent } from "react";
import { useConnection } from "@/components/ConnectionProvider";
import { StartBlockedHelp } from "@/components/PermissionHelp";
import { Badge, ErrorNotice, Loading } from "@/components/ui";
import { useResource } from "@/hooks/useResource";
import { toApiError, type ApiError } from "@/lib/errors";
import { formatDateTime } from "@/lib/format";
import { LANGUAGE_LABEL, SUMMARIZER_STATE_TEXT } from "@/lib/labels";
import { markStartedHere } from "@/lib/liveness";
import { CONSENT_POLICY_VERSION, EXTERNAL_DESTINATION, EXTERNAL_SEND_SCOPE } from "@/lib/policy";
import type { CreateMeetingRequest, Device, Meeting, MeetingLanguage } from "@/lib/types";

const RETENTION_CHOICES = [1, 7, 30, 90];

function pickDefault(devices: Device[], kind: Device["kind"]): string {
  return devices.find((d) => d.kind === kind && d.available)?.key ?? "";
}

export default function NewMeetingPage() {
  const router = useRouter();
  const { client, connKey } = useConnection();
  const devicesRes = useResource("devices", (c) => c.devices());
  const settingsRes = useResource("settings", (c) => c.getSettings());
  const healthRes = useResource("health", (c) => c.health());

  const [title, setTitle] = useState("");
  const [language, setLanguage] = useState<MeetingLanguage>("auto");
  const [useMic, setUseMic] = useState(true);
  const [micKeyChoice, setMicKey] = useState<string | null>(null);
  const [useSystem, setUseSystem] = useState(false);
  const [systemKeyChoice, setSystemKey] = useState<string | null>(null);
  const [consentRecording, setConsentRecording] = useState(false);
  const [retainAudioChoice, setRetainAudio] = useState<boolean | null>(null);
  const [retentionDays, setRetentionDays] = useState(7);
  const [summarize, setSummarize] = useState(false);
  const [externalConsent, setExternalConsent] = useState(false);

  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<ApiError | null>(null);
  const [status, setStatus] = useState("");
  const created = useRef<{ meeting: Meeting; signature: string } | null>(null);
  const consentRef = useRef<HTMLInputElement>(null);
  const ids = { title: useId(), consentHint: useId(), blocked: useId(), mic: useId(), sys: useId(), days: useId() };

  const devices = devicesRes.data?.devices ?? [];
  const mics = devices.filter((d) => d.kind === "microphone");
  const systems = devices.filter((d) => d.kind === "system");
  const systemAvailable = systems.some((d) => d.available);
  const micKey = micKeyChoice ?? pickDefault(devices, "microphone");
  const systemKey = systemKeyChoice ?? pickDefault(devices, "system");
  const retainAudio = retainAudioChoice ?? settingsRes.data?.privacy.defaultRetainAudio ?? false;
  const summarizerState = healthRes.data?.summarizer.state;

  const sources: CreateMeetingRequest["sources"] = [];
  if (useMic && micKey) sources.push({ kind: "microphone", deviceKey: micKey });
  if (useSystem && systemKey && systemAvailable) sources.push({ kind: "system", deviceKey: systemKey });

  const blockers: string[] = [];
  if (!consentRecording) blockers.push("録音同意の確認が必要です。");
  if (sources.length === 0) blockers.push("音声ソースを 1 つ以上選んでください。");
  if (summarize && !externalConsent) blockers.push("Claude CLI への送信を有効にする場合は、送信内容への同意が必要です。");

  function buildRequest(): CreateMeetingRequest {
    return {
      title: title.trim() || `会議 ${formatDateTime(new Date().toISOString())}`,
      language,
      sources,
      consent: { recording: consentRecording, externalProcessing: summarize && externalConsent },
      settings: { summarizationEnabled: summarize, retainAudio, audioRetentionDays: retentionDays },
    };
  }

  async function onSubmit(e: FormEvent) {
    e.preventDefault();
    if (!client || blockers.length > 0 || busy) return;
    setBusy(true);
    setError(null);
    const req = buildRequest();
    const signature = JSON.stringify({ sources: req.sources, language: req.language });
    try {
      let meeting: Meeting;
      const prev = created.current;
      if (prev && prev.signature === signature && prev.meeting.state === "preparing") {
        // Retry after fixing permissions etc.: reuse the prepared meeting and sync editable fields.
        setStatus("会議の設定を更新しています…");
        meeting = await client.patchMeeting(prev.meeting.id, { title: req.title, settings: req.settings });
        const wantExternal = req.consent.externalProcessing;
        if ((meeting.consent.externalProcessing === "granted") !== wantExternal) {
          meeting = await client.recordConsent(meeting.id, {
            scope: "external_processing",
            granted: wantExternal,
            policyVersion: CONSENT_POLICY_VERSION,
          });
        }
      } else {
        setStatus("会議を作成しています…");
        meeting = await client.createMeeting(req);
      }
      created.current = { meeting, signature };
      setStatus("録音を開始しています…");
      const started = await client.startMeeting(meeting.id);
      if (connKey) markStartedHere(started.id, connKey);
      setStatus("録音を開始しました。");
      router.push(`/meetings/view/?id=${encodeURIComponent(started.id)}`);
    } catch (err) {
      setError(toApiError(err));
      setStatus("録音を開始できませんでした。");
    } finally {
      setBusy(false);
    }
  }

  if (devicesRes.loading || settingsRes.loading) return <Loading />;

  return (
    <form className="panel stack" onSubmit={onSubmit} aria-labelledby="new-title" noValidate>
      <h1 id="new-title">新規会議</h1>
      <ErrorNotice error={devicesRes.error} title="入力デバイスを取得できません" />

      <div className="field">
        <label htmlFor={ids.title}>タイトル</label>
        <input
          id={ids.title}
          type="text"
          value={title}
          onChange={(e) => setTitle(e.target.value)}
          placeholder="例: 週次定例（空欄の場合は日時から作成）"
        />
      </div>

      <fieldset>
        <legend>言語</legend>
        <div className="radio-row">
          {(Object.keys(LANGUAGE_LABEL) as MeetingLanguage[]).map((l) => (
            <label key={l} className="check">
              <input type="radio" name="language" value={l} checked={language === l} onChange={() => setLanguage(l)} />
              {LANGUAGE_LABEL[l]}
            </label>
          ))}
        </div>
      </fieldset>

      <fieldset>
        <legend>音声ソース</legend>
        <p className="small muted">
          マイクとシステム音声は別々のソースとして記録されます。OS の権限は録音開始時にソースごとに確認します。
        </p>
        <div className="stack">
          <div className="field">
            <label className="check">
              <input type="checkbox" checked={useMic} onChange={(e) => setUseMic(e.target.checked)} />
              マイク
            </label>
            {useMic && (
              <>
                <label htmlFor={ids.mic} className="visually-hidden">
                  マイクのデバイス
                </label>
                <select id={ids.mic} value={micKey} onChange={(e) => setMicKey(e.target.value)}>
                  {mics.length === 0 && <option value="">利用できるマイクがありません</option>}
                  {mics.map((d) => (
                    <option key={d.key} value={d.key} disabled={!d.available}>
                      {d.name}
                      {!d.available ? "（利用不可）" : ""}
                    </option>
                  ))}
                </select>
              </>
            )}
          </div>
          <div className="field">
            <label className="check">
              <input
                type="checkbox"
                checked={useSystem && systemAvailable}
                disabled={!systemAvailable}
                onChange={(e) => setUseSystem(e.target.checked)}
                aria-describedby={!systemAvailable ? ids.sys : undefined}
              />
              システム音声（PC で再生中の音声）
            </label>
            {!systemAvailable && (
              <p id={ids.sys} className="notice info small">
                この環境ではシステム音声を取得できません。
                {systems.find((d) => d.note)?.note ? ` ${systems.find((d) => d.note)?.note}` : ""}
                マイクのみで会議を開始できます。
              </p>
            )}
            {useSystem && systemAvailable && (
              <select aria-label="システム音声のデバイス" value={systemKey} onChange={(e) => setSystemKey(e.target.value)}>
                {systems.map((d) => (
                  <option key={d.key} value={d.key} disabled={!d.available}>
                    {d.name}
                    {!d.available ? "（利用不可）" : ""}
                  </option>
                ))}
              </select>
            )}
          </div>
          {useMic && useSystem && systemAvailable && (
            <p className="notice warn small" role="note">
              マイクとシステム音声を両方使う場合、スピーカーから出た音をマイクが拾い、同じ発言が二重に記録されることがあります。
              ヘッドセットの使用をおすすめします。
            </p>
          )}
        </div>
      </fieldset>

      <fieldset>
        <legend>ローカル保存</legend>
        <p className="small muted">文字起こしとノートはこの PC 内に保存されます。音声ファイルの保持は会議ごとに選べます（既定は保持しない）。</p>
        <label className="check">
          <input type="checkbox" checked={retainAudio} onChange={(e) => setRetainAudio(e.target.checked)} />
          <span>
            音声を保持する <Badge tone={retainAudio ? "warn" : "neutral"}>{retainAudio ? "保持する" : "保持しない"}</Badge>
          </span>
        </label>
        {retainAudio && (
          <div className="field">
            <label htmlFor={ids.days}>保持期間</label>
            <select id={ids.days} value={retentionDays} onChange={(e) => setRetentionDays(Number(e.target.value))}>
              {RETENTION_CHOICES.map((d) => (
                <option key={d} value={d}>
                  会議終了後 {d} 日
                </option>
              ))}
            </select>
            <span className="hint">音声は暗号化して保存され、期間が過ぎると削除されます。</span>
          </div>
        )}
      </fieldset>

      <fieldset>
        <legend>Claude CLI へのテキスト送信（要約）</legend>
        <label className="check">
          <input
            type="checkbox"
            checked={summarize}
            onChange={(e) => {
              setSummarize(e.target.checked);
              if (!e.target.checked) setExternalConsent(false);
            }}
          />
          <span>
            この会議で要約を有効にする <Badge tone={summarize ? "warn" : "neutral"}>{summarize ? "送信する" : "送信しない"}</Badge>
          </span>
        </label>
        {!summarize && <p className="small muted">無効のままでも、録音とローカル文字起こしはすべて利用できます。</p>}
        {summarize && (
          <div className="notice info stack">
            <p>
              <strong>送信先:</strong> {EXTERNAL_DESTINATION}。この PC にインストールされた CLI を通じて、CLI の契約・組織設定に従って外部で処理されます。
            </p>
            <div>
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
            </div>
            <p className="small">初めて「今まとめる」を押す前に、送信される正確なテキストを確認できます。会議中いつでも無効にできます。</p>
            {summarizerState && summarizerState !== "ready" && (
              <p className="small">
                現在の要約の状態: {SUMMARIZER_STATE_TEXT[summarizerState]?.label ?? summarizerState}。
                {SUMMARIZER_STATE_TEXT[summarizerState]?.explain}
              </p>
            )}
            <label className="check">
              <input type="checkbox" checked={externalConsent} onChange={(e) => setExternalConsent(e.target.checked)} />
              上記の内容を理解し、この会議のテキストを {EXTERNAL_DESTINATION} に送信することに同意します。
            </label>
          </div>
        )}
      </fieldset>

      <fieldset>
        <legend>録音の同意</legend>
        <label className="check">
          <input
            ref={consentRef}
            type="checkbox"
            checked={consentRecording}
            onChange={(e) => setConsentRecording(e.target.checked)}
            aria-describedby={ids.consentHint}
          />
          参加者に録音することを伝え、同意を確認しました。
        </label>
        <p id={ids.consentHint} className="small muted">
          録音前に参加者へ通知し同意を得てください。この確認とポリシー版（{CONSENT_POLICY_VERSION}）は会議に記録されます。
          このチェックは法的な同意手続きの代わりにはならず、地域や組織の法令・規則への適合をアプリが保証するものではありません。
        </p>
      </fieldset>

      {blockers.length > 0 && (
        <ul id={ids.blocked} className="small error-text" aria-live="polite">
          {blockers.map((b) => (
            <li key={b}>{b}</li>
          ))}
        </ul>
      )}

      <ErrorNotice error={error} title="録音を開始できません">
        <StartBlockedHelp
          error={error!}
          onMicOnly={() => {
            setUseSystem(false);
            setUseMic(true);
          }}
          onFocusConsent={() => consentRef.current?.focus()}
        />
        <p className="small">
          <Link href="/settings/">アプリの設定を開く</Link>
        </p>
      </ErrorNotice>

      <div className="row">
        <button
          type="submit"
          className="btn primary"
          disabled={busy || blockers.length > 0 || !client}
          aria-describedby={blockers.length > 0 ? ids.blocked : undefined}
        >
          {busy ? "開始しています…" : "録音を開始"}
        </button>
        <Link href="/" className="btn">
          キャンセル
        </Link>
        <span role="status" aria-live="polite" className="muted small">
          {status}
        </span>
      </div>
    </form>
  );
}
