"use client";

import Link from "next/link";
import { useState } from "react";
import { type ApiError, toApiError } from "@/lib/errors";
import type { Health } from "@/lib/types";
import { useConnection } from "./ConnectionProvider";
import { useInterval, useResource } from "@/hooks/useResource";
import { formatSeconds } from "@/lib/format";
import { STT_STATE_TEXT, SUMMARIZER_STATE_TEXT } from "@/lib/labels";
import { Badge, ErrorNotice } from "./ui";

/** Sidecar health summary: STT model state and summarizer (Claude CLI) state, in plain language. */
export function HealthBanner() {
  const health = useResource("health", (c) => c.health());
  useInterval(health.reload, 15_000);
  const h = health.data;

  if (health.error && !h) {
    return <ErrorNotice error={health.error} title="サイドカーの状態を取得できません" />;
  }
  if (!h) {
    return (
      <p className="muted" role="status">
        サイドカーの状態を確認しています…
      </p>
    );
  }
  const stt = STT_STATE_TEXT[h.stt.state] ?? { label: h.stt.state, explain: "", ok: false };
  const sum = SUMMARIZER_STATE_TEXT[h.summarizer.state] ?? { label: h.summarizer.state, explain: "", ok: false };
  const allOk = h.status === "ok" && stt.ok && sum.ok;

  return (
    <section
      className={`panel ${allOk ? "" : "attention"}`}
      aria-labelledby="health-title"
      aria-live="polite"
    >
      <div className="row spread">
        <h2 id="health-title">
          ローカル処理の状態{" "}
          <Badge tone={h.status === "ok" ? "ok" : h.status === "starting" ? "info" : "warn"}>
            {h.status === "ok" ? "正常" : h.status === "starting" ? "起動中" : "一部制限あり"}
          </Badge>
        </h2>
        <span className="muted small">API v{h.apiVersion}</span>
      </div>
      <div className="health-grid">
        <div className="health-item">
          <h3>
            音声認識（STT） <Badge tone={stt.ok ? "ok" : "warn"}>{stt.label}</Badge>
          </h3>
          <p className="small muted">
            モデル {h.stt.model || "—"} / {h.stt.computeType || "—"} / {h.stt.device || "—"}
          </p>
          {!stt.ok && <p>{stt.explain}</p>}
          {h.stt.state === "not_downloaded" && <ModelDownload stt={h.stt} onStarted={health.reload} />}
          {!stt.ok && (
            <p>
              <Link href="/settings/">設定を開く</Link>
            </p>
          )}
        </div>
        <div className="health-item">
          <h3>
            要約（Claude Code CLI） <Badge tone={sum.ok ? (h.summarizer.state === "ready" ? "ok" : "neutral") : "warn"}>{sum.label}</Badge>
          </h3>
          {h.summarizer.cliVersion && <p className="small muted">CLI {h.summarizer.cliVersion}</p>}
          <p className={sum.ok ? "small muted" : undefined}>{sum.explain}</p>
          {h.summarizer.state === "cli_not_found" && (
            <p>
              <Link href="/settings/">CLI のパスを設定する</Link>
            </p>
          )}
        </div>
        <div className="health-item">
          <h3>処理キュー</h3>
          <p className="small">
            認識待ち {formatSeconds(h.queue.sttBacklogMs)} / 要約ジョブ {h.queue.pendingJobs} 件
          </p>
          {h.vad?.state && <p className="small muted">VAD: {h.vad.state}</p>}
        </div>
      </div>
    </section>
  );
}

type SttHealth = Health["stt"];

/**
 * Spec §5: before the first model fetch, state size, provider, licence, destination and
 * network use, and download only on an explicit user action.
 */
function ModelDownload({ stt, onStarted }: { stt: SttHealth; onStarted: () => void }) {
  const { client } = useConnection();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<ApiError | null>(null);
  const downloading = Boolean(stt.downloading) || busy;

  async function start() {
    if (!client) return;
    setBusy(true);
    setError(null);
    try {
      await client.downloadSttModel();
      onStarted();
    } catch (e) {
      setError(toApiError(e));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="stack small">
      <p>認識モデルは未取得です。取得すると次の内容で保存されます。</p>
      <dl className="small">
        <dt>モデル</dt>
        <dd>{stt.model}（約 {stt.approxSizeMb ?? "?"} MB）</dd>
        <dt>提供元</dt>
        <dd>{stt.source ?? "—"}</dd>
        <dt>ライセンス</dt>
        <dd>{stt.license ?? "—"}</dd>
        <dt>保存先</dt>
        <dd className="mono">{stt.targetDir ?? "—"}</dd>
        <dt>ネットワーク</dt>
        <dd>{stt.network ?? "インターネットから取得します（初回のみ）。"}</dd>
      </dl>
      {stt.downloadError && <p role="alert">前回の取得に失敗しました: {stt.downloadError}</p>}
      {error && <ErrorNotice error={error} title="モデルを取得できません" />}
      <p>
        <button type="button" onClick={start} disabled={downloading || !client}>
          {downloading ? "取得中…" : "この内容でモデルを取得する"}
        </button>
      </p>
    </div>
  );
}
