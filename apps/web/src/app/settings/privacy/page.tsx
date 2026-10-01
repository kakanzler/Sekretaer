"use client";

import { ErrorNotice, Loading } from "@/components/ui";
import { useResource } from "@/hooks/useResource";
import { CONSENT_POLICY_VERSION, EXTERNAL_SEND_SCOPE } from "@/lib/policy";

function countOf(v: number | string[] | undefined): string {
  if (v === undefined) return "—";
  return `${Array.isArray(v) ? v.length : v} 件`;
}

export default function PrivacyPage() {
  const privacy = useResource("privacy", (c) => c.privacy());
  const p = privacy.data;
  return (
    <div className="stack">
      <section className="panel stack" aria-labelledby="privacy-title">
        <h1 id="privacy-title">プライバシー</h1>
        {privacy.loading && <Loading />}
        <ErrorNotice error={privacy.error} title="プライバシー情報を取得できません" />
        {p && (
          <dl className="kv">
            <dt>データの保存先</dt>
            <dd>
              <code>{p.storage.dataDir}</code>
            </dd>
            <dt>データベース</dt>
            <dd>
              <code>{p.storage.dbPath}</code>
            </dd>
            <dt>音声を保持している会議</dt>
            <dd>{countOf(p.audioArchive.enabledMeetings)}</dd>
            <dt>外部処理を有効にした会議</dt>
            <dd>{countOf(p.externalProcessing.enabledMeetings)}</dd>
            <dt>外部処理の送信先</dt>
            <dd>{p.externalProcessing.destination}</dd>
          </dl>
        )}
      </section>

      <section className="panel stack" aria-labelledby="defaults-title">
        <h2 id="defaults-title">初期設定と取り扱い</h2>
        <ul>
          <li>音声はこの PC 上で取得し、文字起こしもこの PC 上で行います。</li>
          <li>音声ファイルは既定で保持しません。会議ごとに保持を選んだ場合のみ、暗号化して保持期間まで保存します。</li>
          <li>
            Claude Code CLI への送信は既定で無効です。会議ごとに有効にした場合のみ、次のものを送信します: {EXTERNAL_SEND_SCOPE.sent.join("、")}。
            {EXTERNAL_SEND_SCOPE.notSent.join("、")}は送信しません。
          </li>
          <li>ログには会議の本文、音声、CLI の入出力を含めません。</li>
        </ul>
      </section>

      <section className="panel stack" aria-labelledby="consent-title">
        <h2 id="consent-title">録音の同意について</h2>
        <p>
          録音を始める前に、参加者へ録音することを通知し、同意を得てください。Sekretär はその確認操作とポリシー版（現在:{" "}
          <code>{CONSENT_POLICY_VERSION}</code>）を会議ごとに記録します。各会議の同意記録は、会議画面の「プライバシー」欄で確認できます。
        </p>
        <p>このアプリの確認操作は法的な同意手続きの代わりにはなりません。地域の法令や組織の規則への適合をアプリが保証することはありません。</p>
        <p>
          組織で利用する場合は、Claude Code CLI の利用契約・組織設定・データ処理条件が会議テキストに適用されるかを、組織の管理者に確認してください。
        </p>
      </section>

      <section className="panel stack" aria-labelledby="delete-title">
        <h2 id="delete-title">削除と保持</h2>
        <p>
          会議を削除すると、アプリが管理する DB レコード、音声、派生キャッシュ、一時ファイル、アプリ管理のエクスポート済みファイルを削除し、
          削除できたもの・できなかったものを表示します。OS のバックアップや、ご自身で保存したコピーはアプリからは削除できません。
        </p>
      </section>
    </div>
  );
}
