"use client";

import Link from "next/link";
import { useId, useState, type FormEvent } from "react";
import { useConnection } from "@/components/ConnectionProvider";
import { HealthBanner } from "@/components/HealthBanner";
import { ErrorNotice, Loading } from "@/components/ui";
import { useResource } from "@/hooks/useResource";
import { toApiError, type ApiError } from "@/lib/errors";
import type { AppSettings, VadSensitivity } from "@/lib/types";

const STT_MODELS = ["tiny", "base", "small", "medium", "large-v3"];
const COMPUTE_TYPES = ["int8", "int8_float16", "float16", "float32"];
const DEVICES = ["cpu", "cuda", "auto"];
const VAD_LABEL: Record<VadSensitivity, string> = { low: "低（騒がしい環境向け）", normal: "標準", high: "高（小さな声も拾う）" };

function SettingsForm({ initial, onSaved }: { initial: AppSettings; onSaved: (s: AppSettings) => void }) {
  const { client } = useConnection();
  const [draft, setDraft] = useState<AppSettings>(initial);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<ApiError | null>(null);
  const [message, setMessage] = useState("");
  const ids = { model: useId(), compute: useId(), device: useId(), vad: useId(), cli: useId(), timeout: useId() };

  async function onSubmit(e: FormEvent) {
    e.preventDefault();
    if (!client) return;
    setBusy(true);
    setError(null);
    setMessage("");
    try {
      const cliPath = draft.summarizer.cliPath?.trim() ? draft.summarizer.cliPath.trim() : null;
      const saved = await client.putSettings({ ...draft, summarizer: { ...draft.summarizer, cliPath } });
      onSaved(saved);
      setDraft(saved);
      setMessage("設定を保存しました。");
    } catch (err) {
      setError(toApiError(err));
    } finally {
      setBusy(false);
    }
  }

  const set = <K extends keyof AppSettings>(k: K, v: AppSettings[K]) => setDraft((d) => ({ ...d, [k]: v }));

  return (
    <form className="panel stack" onSubmit={onSubmit} aria-labelledby="settings-title">
      <h1 id="settings-title">設定</h1>
      <fieldset className="stack">
        <legend>ローカル音声認識（faster-whisper）</legend>
        <div className="grid-2">
          <div className="field">
            <label htmlFor={ids.model}>モデル</label>
            <input
              id={ids.model}
              type="text"
              list={`${ids.model}-list`}
              value={draft.stt.model}
              onChange={(e) => set("stt", { ...draft.stt, model: e.target.value })}
            />
            <datalist id={`${ids.model}-list`}>
              {STT_MODELS.map((m) => (
                <option key={m} value={m} />
              ))}
            </datalist>
            <span className="hint">大きいモデルほど精度は上がりますが、メモリと処理時間が増えます。未取得のモデルは初回にダウンロードされます。</span>
          </div>
          <div className="field">
            <label htmlFor={ids.compute}>計算方式（量子化）</label>
            <select id={ids.compute} value={draft.stt.computeType} onChange={(e) => set("stt", { ...draft.stt, computeType: e.target.value })}>
              {[...new Set([...COMPUTE_TYPES, draft.stt.computeType])].map((c) => (
                <option key={c} value={c}>
                  {c}
                </option>
              ))}
            </select>
          </div>
          <div className="field">
            <label htmlFor={ids.device}>デバイス</label>
            <select id={ids.device} value={draft.stt.device} onChange={(e) => set("stt", { ...draft.stt, device: e.target.value })}>
              {[...new Set([...DEVICES, draft.stt.device])].map((c) => (
                <option key={c} value={c}>
                  {c === "cpu" ? "CPU" : c === "cuda" ? "GPU（CUDA）" : c === "auto" ? "自動" : c}
                </option>
              ))}
            </select>
          </div>
          <div className="field">
            <label htmlFor={ids.vad}>発話検出（VAD）の感度</label>
            <select
              id={ids.vad}
              value={draft.vad.sensitivity}
              onChange={(e) => set("vad", { sensitivity: e.target.value as VadSensitivity })}
            >
              {(Object.keys(VAD_LABEL) as VadSensitivity[]).map((v) => (
                <option key={v} value={v}>
                  {VAD_LABEL[v]}
                </option>
              ))}
            </select>
          </div>
        </div>
      </fieldset>

      <fieldset className="stack">
        <legend>要約（Claude Code CLI）</legend>
        <div className="grid-2">
          <div className="field">
            <label htmlFor={ids.cli}>CLI のパス（任意）</label>
            <input
              id={ids.cli}
              type="text"
              value={draft.summarizer.cliPath ?? ""}
              placeholder="空欄の場合は PATH から claude を探します"
              onChange={(e) => set("summarizer", { ...draft.summarizer, cliPath: e.target.value })}
              autoComplete="off"
            />
          </div>
          <div className="field">
            <label htmlFor={ids.timeout}>タイムアウト（秒）</label>
            <input
              id={ids.timeout}
              type="number"
              min={10}
              max={1800}
              value={draft.summarizer.timeoutSec}
              onChange={(e) => set("summarizer", { ...draft.summarizer, timeoutSec: Number(e.target.value) || 120 })}
            />
          </div>
        </div>
        <p className="small muted">Sekretär は CLI の認証情報を収集・保存しません。認証はターミナルで claude を起動して行ってください。</p>
      </fieldset>

      <fieldset className="stack">
        <legend>プライバシーの既定値</legend>
        <label className="check">
          <input
            type="checkbox"
            checked={draft.privacy.defaultRetainAudio}
            onChange={(e) => set("privacy", { ...draft.privacy, defaultRetainAudio: e.target.checked })}
          />
          新しい会議で音声を保持する（既定: 保持しない）
        </label>
        <p className="small muted">
          会議ごとの新規作成画面でも変更できます。Claude CLI への送信は常に既定で無効で、会議ごとに有効化します。
          <Link href="/settings/privacy/">プライバシーの詳細</Link>
        </p>
      </fieldset>

      <ErrorNotice error={error} title="設定を保存できません" />
      <div className="row">
        <button type="submit" className="btn primary" disabled={busy || !client}>
          {busy ? "保存中…" : "保存"}
        </button>
        <span role="status" aria-live="polite" className="small muted">
          {message}
        </span>
      </div>
    </form>
  );
}

export default function SettingsPage() {
  const settings = useResource("settings", (c) => c.getSettings());
  return (
    <div className="stack">
      {settings.loading && <Loading />}
      <ErrorNotice error={settings.error} title="設定を取得できません" />
      {settings.data && <SettingsForm initial={settings.data} onSaved={(s) => settings.setData(() => s)} />}
      <HealthBanner />
    </div>
  );
}
