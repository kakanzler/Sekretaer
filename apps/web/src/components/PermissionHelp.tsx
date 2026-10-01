"use client";

import Link from "next/link";
import { useState } from "react";
import { isTauri, openOsPrivacySettings } from "@/lib/connection";
import type { ApiError } from "@/lib/errors";

/**
 * Explains why recording could not start and where to fix it (AC-01).
 * Renders only guidance; the error itself is shown by ErrorNotice.
 */
export function StartBlockedHelp({
  error,
  onMicOnly,
  onFocusConsent,
}: {
  error: ApiError;
  onMicOnly?: () => void;
  onFocusConsent?: () => void;
}) {
  const [opened, setOpened] = useState<boolean | null>(null);
  const inTauri = typeof window !== "undefined" && isTauri();

  if (error.code === "permission_denied") {
    return (
      <div className="stack small">
        <p>
          <strong>設定経路:</strong> Windows は「設定 → プライバシーとセキュリティ → マイク」で「アプリがマイクにアクセスできるようにする」を
          オンにします。macOS は「システム設定 → プライバシーとセキュリティ → マイク」（システム音声は「画面収録とシステムオーディオ録音」）で
          Sekretär を許可します。変更後にもう一度「録音を開始」を押してください。
        </p>
        {inTauri && (
          <div className="row">
            <button
              type="button"
              className="btn"
              onClick={async () => setOpened(await openOsPrivacySettings("microphone"))}
            >
              OS のプライバシー設定を開く
            </button>
            {opened === false && <span className="error-text">設定画面を開けませんでした。上記の手順で開いてください。</span>}
          </div>
        )}
      </div>
    );
  }
  if (error.code === "source_unavailable") {
    return (
      <div className="stack small">
        <p>
          選択した入力ソースを利用できません。システム音声の取得は OS によって対応していない場合があります。デバイスの接続を確認するか、
          マイクのみで会議を開始できます。
        </p>
        {onMicOnly && (
          <button type="button" className="btn" onClick={onMicOnly}>
            マイクのみにする
          </button>
        )}
      </div>
    );
  }
  if (error.code === "consent_required") {
    return (
      <div className="stack small">
        <p>録音を始める前に、参加者へ録音することを伝え、同意を確認したうえで「録音同意」にチェックしてください。</p>
        {onFocusConsent && (
          <button type="button" className="btn" onClick={onFocusConsent}>
            同意の項目へ移動
          </button>
        )}
      </div>
    );
  }
  if (error.code === "stt_model_unavailable") {
    return (
      <p className="small">
        <Link href="/settings/">設定画面</Link>で音声認識モデルを確認してください。
      </p>
    );
  }
  return null;
}
