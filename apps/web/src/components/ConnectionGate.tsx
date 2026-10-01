"use client";

import { useId, useState, type FormEvent, type ReactNode } from "react";
import { restartSidecar } from "@/lib/connection";
import { useConnection } from "./ConnectionProvider";

const PHASE_TEXT: Record<string, { title: string; body: string }> = {
  init: { title: "接続を準備しています…", body: "" },
  starting: {
    title: "サイドカーを起動しています…",
    body: "録音・文字起こしを担当するローカル処理（サイドカー）の起動を待っています。",
  },
  restarting: {
    title: "サイドカーを再起動しています…",
    body: "ローカル処理が停止したため再起動しています。録音中だった会議は自動では再開されません。再接続後に会議画面で状態を確認してください。",
  },
  failed: {
    title: "サイドカーを起動できませんでした",
    body: "ローカル処理（サイドカー）の再起動を繰り返しても起動できませんでした。録音中だった会議は自動では再開されません。「再起動」を押すか、アプリを再起動してください。",
  },
  crashed: {
    title: "サイドカーが停止しました",
    body: "ローカル処理が異常終了しました。録音中だった会議は自動では再開されません。アプリを再起動し、会議画面から「録音を終了して確定」を選んでください。",
  },
};

function RestartButton() {
  const [state, setState] = useState<"idle" | "busy" | "failed">("idle");
  return (
    <p className="row">
      <button
        type="button"
        className="btn"
        disabled={state === "busy"}
        onClick={async () => {
          setState("busy");
          setState((await restartSidecar()) ? "idle" : "failed");
        }}
      >
        再起動
      </button>
      {state === "failed" && <span className="error-text">再起動を要求できませんでした。アプリを再起動してください。</span>}
    </p>
  );
}

function DevConnectForm() {
  const { connectDev } = useConnection();
  const [port, setPort] = useState("");
  const [token, setToken] = useState("");
  const [invalid, setInvalid] = useState(false);
  const portId = useId();
  const tokenId = useId();
  const errId = useId();

  function onSubmit(e: FormEvent) {
    e.preventDefault();
    setInvalid(!connectDev(port, token));
  }

  return (
    <section className="panel narrow" aria-labelledby="dev-connect-title">
      <h1 id="dev-connect-title">開発用: サイドカーへの接続</h1>
      <p>
        ブラウザー単独の開発モードです。<code>python -m sekretaer --dev</code> が標準エラーに出力するポートとトークンを入力してください。
        トークンはこのタブの sessionStorage にだけ保存され、URL には含めません。
      </p>
      <form onSubmit={onSubmit} className="stack">
        <div className="field">
          <label htmlFor={portId}>ポート（または ready 行の JSON）</label>
          <input
            id={portId}
            value={port}
            onChange={(e) => setPort(e.target.value)}
            inputMode="numeric"
            autoComplete="off"
            required
            aria-describedby={invalid ? errId : undefined}
          />
        </div>
        <div className="field">
          <label htmlFor={tokenId}>トークン</label>
          <input
            id={tokenId}
            type="password"
            value={token}
            onChange={(e) => setToken(e.target.value)}
            autoComplete="off"
          />
        </div>
        {invalid && (
          <p id={errId} role="alert" className="error-text">
            ポートとトークンを確認してください（ポートは 1〜65535 の整数）。
          </p>
        )}
        <div className="row">
          <button type="submit" className="btn primary">
            接続
          </button>
        </div>
      </form>
    </section>
  );
}

/**
 * Renders children once a sidecar client exists. Before the first connection a
 * status screen is shown; afterwards a banner reports restarts/crashes while
 * keeping the current screen mounted.
 */
export function ConnectionGate({ children }: { children: ReactNode }) {
  const { phase, error, client, mode, streamStatus } = useConnection();

  if (phase === "dev-setup") return <DevConnectForm />;

  if (!client) {
    const t = PHASE_TEXT[phase] ?? PHASE_TEXT.init!;
    return (
      <section className="panel narrow" role="status" aria-live="polite">
        <h1>{t.title}</h1>
        {t.body && <p>{t.body}</p>}
        {error && <p className="muted">詳細: {error}</p>}
        {phase === "failed" && mode === "tauri" && <RestartButton />}
      </section>
    );
  }

  const t = phase !== "ready" ? PHASE_TEXT[phase] : null;
  return (
    <>
      {t && (
        <div className={`banner ${phase === "crashed" || phase === "failed" ? "danger" : "warn"}`} role="alert">
          <strong>{t.title}</strong> {t.body}
          {error && <span className="muted"> 詳細: {error}</span>}
          {phase === "failed" && mode === "tauri" && <RestartButton />}
        </div>
      )}
      {phase === "ready" && streamStatus === "reconnecting" && (
        <div className="banner warn" role="status" aria-live="polite">
          リアルタイム更新の接続が切れました。再接続しています…（見逃したイベントは再接続後に補完されます）
        </div>
      )}
      {mode === "mock" && (
        <div className="banner info" role="note">
          モックモードで表示しています（NEXT_PUBLIC_SEKRETAER_MOCK=1）。データは実在しません。
        </div>
      )}
      {children}
    </>
  );
}
